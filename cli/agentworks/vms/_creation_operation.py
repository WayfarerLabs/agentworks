"""Independent creation through native bootstrap and canonical RunContext handoff."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.capabilities.base import RunContext, TargetAbsence
from agentworks.capabilities.vm_platform.base import VMPlatform, provider_locator_remaining
from agentworks.db import Database, OperationResourceKind, OperationScope, ProvisioningStatus, VMStatus
from agentworks.errors import AlreadyExistsError, ConfigError, StateError, ValidationError
from agentworks.execution._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from agentworks.execution._file_paths import normalized_root
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh.connection import admit_connection
from agentworks.execution.target import ExecutionRoute
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name
from agentworks.operations import OperationOwner
from agentworks.secrets.line_safety import LineOrientedSecretUse, require_line_safe_secret
from agentworks.vms._creation_access import VMCreationResult, creation_metadata_snapshot
from agentworks.vms._creation_guest import attest_created_guest, bootstrap_created_guest
from agentworks.vms._creation_trust import CreationSSHTrust
from agentworks.vms._native_operation import VMOperationControlFact, _prepare_route, _Workflow
from agentworks.vms._ssh_native_binding import ssh_execution_binding, vm_ssh_connection
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.db import VMRow
    from agentworks.execution.carriers.ssh import SSHSettings
    from agentworks.vms._creation_access import OwnedVMCreationAccess


class _BootstrapPhase(Enum):
    PREPARING = auto()
    ADDRESS_PUBLICATION = auto()
    COMPLETE = auto()
    TRANSFERRED = auto()


@dataclass(kw_only=True)
class _CreationWorkflow(_Workflow):
    db: Database
    vm_name: str
    site: str
    marker: str
    admin_username: str
    hostname: str
    creation: OwnedVMCreationAccess | None = None
    trust: CreationSSHTrust | None = None
    row_attempted: bool = False
    phase: _BootstrapPhase = _BootstrapPhase.PREPARING
    address: str | None = None

    @contextmanager
    def row_transaction(self) -> Iterator[VMRow]:
        """Own the commit boundary and fence the selected row before writes.

        The VM read establishes the SQLite snapshot before the claim read on
        the separate operation connection. A concurrent takeover cannot upgrade
        that stale snapshot into a committed VM write.
        """
        _require_standalone_database(self.db)
        with self.db.transaction():
            vm = self.db.get_vm(self.vm_name)
            self.owner.list_pending_lifecycle_obligations()
            if vm is None or (vm.site, vm.instance_marker, vm.admin_username, vm.hostname) != (
                self.site,
                self.marker,
                self.admin_username,
                self.hostname,
            ):
                raise StateError("Created VM row no longer matches this workflow")
            yield vm

    def persist_metadata(self) -> None:
        if self.creation is None:
            return
        metadata = creation_metadata_snapshot(self.creation.platform_metadata)
        if metadata:
            with self.row_transaction() as vm:
                if vm.platform_metadata == metadata:
                    return
                if vm.platform_metadata:
                    raise StateError("Created VM platform metadata changed")
                self.db.update_vm_platform_metadata(self.vm_name, metadata)

    def publish_address(self, address: str) -> None:
        # Arm reconciliation before the write: commit may succeed without a reply.
        self.address = address
        self.phase = _BootstrapPhase.ADDRESS_PUBLICATION
        with self.row_transaction() as vm:
            if vm.tailscale_host is not None and vm.tailscale_host != address:
                raise StateError("Created VM address changed before publication")
            self.db.update_vm_tailscale(self.vm_name, address)
        self.phase = _BootstrapPhase.COMPLETE

    def _reconcile_address_publication(self) -> None:
        if self.phase is not _BootstrapPhase.ADDRESS_PUBLICATION:
            return
        with self.row_transaction() as vm:
            address = vm.tailscale_host
            if address is not None and address != self.address:
                raise StateError("Created VM address publication remains unproved")
        self.phase = _BootstrapPhase.PREPARING if address is None else _BootstrapPhase.COMPLETE

    def transfer_bootstrap(self, deadline: Deadline) -> None:
        if self.phase is _BootstrapPhase.COMPLETE:
            assert self.creation is not None
            self.creation.complete_bootstrap(deadline)
            self.phase = _BootstrapPhase.TRANSFERRED

    def _settle_access(self, deadline: Deadline) -> bool:
        # Common cleanup has already drained actual guest and local delivery.
        # Checkpoint failures retain authority without keeping local processes alive.
        if self.trust is not None and not self.trust.close(deadline):
            raise StateError("Created VM retains SSH trust maintenance custody")
        if self.row_attempted:
            self._reconcile_address_publication()
        if self.creation is None:
            return True
        self.persist_metadata()
        if self.phase is _BootstrapPhase.PREPARING:
            self.creation.request_rollback()
        else:
            self.transfer_bootstrap(deadline)
        try:
            settled = self.creation.settle(deadline)
        except BaseException as control:
            # Settlement can establish a previously unknown provider association.
            # Preserve that addressability while the original claim is retained.
            try:
                self.persist_metadata()
            except BaseException:
                control.add_note("Created VM metadata publication remains unsettled")
            raise
        self.persist_metadata()
        return settled


def _require_standalone_database(db: Database) -> None:
    # Creation checkpoints must commit before a provider transfer or SSH handoff.
    # Database.transaction deliberately joins nested transactions elsewhere.
    if db._tx_depth or db._conn.in_transaction:  # noqa: SLF001
        raise StateError("VM creation requires its own database commit boundaries")


def _insert_row(
    workflow: _CreationWorkflow, request: ProvisionRequest, template: str | None, admin_template: str | None
) -> VMRow:
    with workflow.db.transaction():
        existing = workflow.db.get_vm(workflow.vm_name)
        workflow.owner.list_pending_lifecycle_obligations()
        if existing is not None:
            raise AlreadyExistsError("VM already exists", entity_kind="vm", entity_name=workflow.vm_name)
        workflow.row_attempted = True
        vm = workflow.db.insert_vm(
            workflow.vm_name,
            workflow.site,
            workflow.hostname,
            template=template,
            admin_template=admin_template,
            cpus=request.cpus,
            memory_gib=request.memory_gib,
            disk_gib=request.disk_gib,
            swap_gib=request.swap_gib,
            admin_username=workflow.admin_username,
            instance_marker=workflow.marker,
        )
        workflow.db.update_vm_provisioning_status(workflow.vm_name, ProvisioningStatus.IN_PROGRESS)
    return vm


def _prepare_created_context(
    workflow: _CreationWorkflow,
    request: ProvisionRequest,
    platform: VMPlatform,
    ctx: RunContext,
    settings: SSHSettings,
    trusted_root: PurePosixPath,
    template: str | None,
    admin_template: str | None,
) -> RunContext:
    db, deadline = workflow.db, workflow.deadline
    # Admit endpoint-independent local policy before provider effects. This
    # passive unspecified-address connection is never used for delivery; the
    # observed guest IPv4 and current policy are admitted again after bootstrap.
    provider_locator_remaining(deadline, vm_name=workflow.vm_name)
    workflow.owner.list_pending_lifecycle_obligations()
    admit_connection(vm_ssh_connection(workflow.vm_name, workflow.admin_username, "0.0.0.0", settings))
    provider_locator_remaining(deadline, vm_name=workflow.vm_name)
    vm = _insert_row(workflow, request, template, admin_template)
    creation = platform.build_creation_access(request, ctx, owner=workflow.owner, custody=workflow.local_delivery)
    workflow.creation = creation
    workflow.access = creation
    result = creation.provision(deadline)
    if type(result) is not VMCreationResult:
        raise StateError("VM platform did not return independent creation evidence")
    workflow.persist_metadata()
    if dict(result.platform_metadata) != creation_metadata_snapshot(creation.platform_metadata):
        raise StateError("VM creation result conflicts with retained platform addressability")
    with workflow.row_transaction() as current:
        vm = current
    native = _prepare_route(
        workflow,
        db,
        vm,
        platform,
        creation,
        result.locator,
        result.binding,
        trusted_root=trusted_root,
        agent=None,
        route=ExecutionRoute.NATIVE,
        route_check=creation.route_check,
    )
    native_route = workflow.routes[-1]
    workflow.publish_address(bootstrap_created_guest(native, request, deadline))
    workflow.transfer_bootstrap(deadline)
    release = attest_created_guest(native, request.debian_release, deadline)
    with workflow.row_transaction():
        db.update_vm_debian_release(workflow.vm_name, release)
    # Revoke public native body admission while retaining all native cleanup.
    native_route.body_admission_closed = True
    with workflow.row_transaction() as current:
        vm = current
    assert workflow.address is not None
    workflow.trust = CreationSSHTrust(workflow.owner, vm, workflow.address, settings, deadline=deadline)
    workflow.trust.prepare()
    availability = creation.independent_availability
    binding = ssh_execution_binding(vm, workflow.address, settings, independent_availability=availability)
    selected_metadata = dict(vm.platform_metadata)

    def check_canonical(budget: Deadline) -> None:
        # Selection evidence also authorizes retained cleanup after body admission
        # closes. Public interfaces separately reject new body operations.
        provider_locator_remaining(budget, vm_name=workflow.vm_name)
        with workflow.row_transaction() as current:
            if current.tailscale_host != workflow.address or current.platform_metadata != selected_metadata:
                raise StateError("Created VM canonical selection changed")
        if creation.observe_power(budget) is not VMStatus.RUNNING:
            raise StateError("Created VM availability is unconfirmed")
        if creation.observe_locator(budget) != result.locator:
            raise StateError("Created VM provider locator changed or is unavailable")
        if creation.independent_availability is not availability:
            raise StateError("Created VM platform availability changed")
        provider_locator_remaining(budget, vm_name=workflow.vm_name)

    canonical = _prepare_route(
        workflow,
        db,
        vm,
        platform,
        creation,
        result.locator,
        binding,
        trusted_root=trusted_root,
        agent=None,
        route=ExecutionRoute.CANONICAL,
        route_check=check_canonical,
    )
    with workflow.row_transaction():
        db.update_vm_provisioning_status(workflow.vm_name, ProvisioningStatus.COMPLETE)
    return RunContext(
        config=ctx.config,
        operation_scope=ctx.operation_scope,
        admin_execution_target=canonical.target,
        agent_execution_target_absence=TargetAbsence.UNAVAILABLE,
        platform_host_execution_target=ctx.platform_host_execution_target(),
        platform_host_execution_target_absence=ctx.platform_host_execution_target_absence()
        or TargetAbsence.UNAVAILABLE,
        secrets=ctx._secrets,  # noqa: SLF001
    )


@contextmanager
def vm_creation_context(
    db: Database,
    request: ProvisionRequest,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    deadline: Deadline,
    trusted_root: PurePosixPath,
    template: str | None = None,
    admin_template: str | None = None,
) -> Iterator[RunContext]:
    """Own creation, canonical initialization and exact aggregate cleanup.

    This entry never calls the legacy platform create or initializer. The body
    receives only the new canonical target and owns its initialization outcome.
    Bootstrap completion retains an addressable VM even when that body fails.
    """
    if type(db) is not Database or not isinstance(platform, VMPlatform) or type(ctx) is not RunContext:
        raise ValidationError("VM creation requires core database, platform and context values")
    if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
        raise ValidationError("VM creation requires a live finite deadline")
    if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
        raise ValidationError("VM creation requires a trusted normalized file root")
    validate_name(request.vm_name, max_length=MAX_VM_NAME_LENGTH)
    marker = validate_vm_instance_marker(request.instance_marker)
    require_line_safe_secret(request.tailscale_auth_key, use=LineOrientedSecretUse.TAILSCALE)
    try:
        encode_account_request(AccountRequest("0" * 32, request.admin_username))
    except AccountRequestError as error:
        raise ValidationError("VM creation requires a valid administrative account") from error
    if ctx.operation_scope is not None and ctx.operation_scope.vm not in {None, request.vm_name}:
        raise ValidationError("VM creation context names a different VM")
    settings = ctx.config.operator.ssh if ctx.config is not None else None
    if settings is None:
        raise ConfigError("VM creation requires explicit operator SSH settings")
    platform.validate_create_release(request.debian_release)
    _require_standalone_database(db)
    owner = OperationOwner.acquire(
        db.operations, OperationScope(OperationResourceKind.VM, request.vm_name), "vm-creation-operation"
    )
    workflow = _CreationWorkflow(
        owner=owner,
        deadline=deadline,
        db=db,
        vm_name=request.vm_name,
        site=platform.site_name,
        marker=marker,
        admin_username=request.admin_username,
        hostname=request.hostname,
    )
    primary: BaseException | None = None
    prepared = False
    try:
        created = _prepare_created_context(
            workflow, request, platform, ctx, settings, trusted_root, template, admin_template
        )
        prepared = True
        yield created
    except BaseException as control:
        primary = control
        if workflow.row_attempted and not prepared:
            try:
                with workflow.row_transaction():
                    db.update_vm_provisioning_status(workflow.vm_name, ProvisioningStatus.FAILED)
            except BaseException:
                control.add_note("Created VM failure checkpoint remains unsettled")
        raise
    finally:
        try:
            workflow.close()
        except BaseException as cleanup:
            fact = VMOperationControlFact(workflow)
            if primary is None:
                fact.__cause__ = cleanup.__cause__
                raise cleanup from fact
            primary.add_note("VM creation teardown retained unresolved custody")
            fact.__cause__ = primary.__cause__
            primary.__cause__ = fact
            primary.__suppress_context__ = True
