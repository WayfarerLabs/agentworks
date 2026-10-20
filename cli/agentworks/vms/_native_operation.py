"""Private core-owned VM operation, selected route and context lifetime."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from threading import Lock
from typing import TYPE_CHECKING

from agentworks.capabilities.base import RunContext, TargetAbsence
from agentworks.capabilities.vm_platform.base import VMPlatform, provider_locator_remaining
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.execution._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_gate_namespace import NamespacePreparation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_paths import normalized_root
from agentworks.execution._managed_runs import ManagedRunOwner, ManagedRunOwnerKind, ManagedRunRepository
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityPreparation,
    TargetIdentityStatus,
    prepare_target_identity,
)
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.execution.target import ExecutionIdentity, ExecutionRoute, ExecutionTarget
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name
from agentworks.operations import OperationOwner
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_execution_binding
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.vm_platform.base import ProviderLocator
    from agentworks.db import AgentRow, VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.vms._native_execution_access import OwnedVMPlatformAccess


@dataclass(frozen=True, slots=True)
class VMOperation:
    """Private operation custody and target sharing one exact claim and deadline."""

    owner: OperationOwner
    files: FileAccess
    execution: ExecutionAccess
    file_operation: FileOperation
    execution_operation: ExecutionOperation
    target: ExecutionTarget
    agent_target: ExecutionTarget | None = None


class VMOperationControlFact(Exception):
    """Safe handoff of exact in-memory cleanup custody after failed teardown."""

    def __init__(self, workflow: _Workflow) -> None:
        self._workflow = workflow
        super().__init__("VM operation retains cleanup custody")

    def retry_cleanup(self, deadline: Deadline) -> None:
        """Retry only aggregate cleanup under a fresh finite observation budget."""
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("VM cleanup requires a live finite deadline")
        self._workflow.close(cleanup_deadline=deadline)


@dataclass
class _RoutePreparation:
    """Retained preparation and admission for one route under the outer owner."""

    preparation: VMTargetPreparation | None = None
    identity: TargetIdentityPreparation | None = None
    agent_identity: TargetIdentityPreparation | None = None
    namespace: NamespacePreparation | None = None
    views: VMOperation | None = None
    body_admission_closed: bool = False

    def components_settled(self) -> bool:
        if self.namespace is not None and not self.namespace.settled:
            return False
        preparation = self.preparation
        if preparation is not None and (
            preparation.pending_remote_effects
            or preparation.coordination_uncertain
            or preparation.requires_owner_retention
        ):
            return False
        for identity in (self.identity, self.agent_identity):
            if identity is not None and (
                identity.pending_remote_effects or identity.coordination_uncertain or identity.requires_owner_retention
            ):
                return False
        views = self.views
        if views is None:
            return True
        files = views.file_operation
        execution = views.execution_operation
        return not (files.has_pending_custody or execution.has_pending_custody)


@dataclass
class _Workflow:
    owner: OperationOwner
    deadline: Deadline
    local_delivery: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody, repr=False)
    access: OwnedVMPlatformAccess | None = None
    routes: list[_RoutePreparation] = field(default_factory=list)
    finalizing: bool = False
    body_admission_closed: bool = False
    _cleanup_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def _components_settled(self) -> bool:
        return all(route.components_settled() for route in self.routes)

    def close(self, *, cleanup_deadline: Deadline | None = None) -> None:
        """Stop body admission, then release only on aggregate exact settlement."""
        self.body_admission_closed = True
        self.owner.stop_admission()
        if not self._cleanup_lock.acquire(blocking=False):
            raise StateError("VM operation cleanup is already active")
        try:
            self._close(cleanup_deadline=cleanup_deadline)
        finally:
            self._cleanup_lock.release()

    def _close(self, *, cleanup_deadline: Deadline | None = None) -> None:
        """Run aggregate cleanup while this workflow holds its cleanup lock."""
        if self.finalizing:
            self.owner.close()
            return
        self.owner.retire_unused_borrow()
        budget = self.deadline if cleanup_deadline is None else cleanup_deadline
        views = tuple(route.views for route in self.routes if route.views is not None)
        # No route may dispatch fresh cleanup while another route retains an
        # actual closing scratch helper under the shared serial borrow.
        for view in views:
            view.execution_operation.settle_closing_scratch_helpers(budget)
        drained = True
        interrupted: BaseException | None = None
        for view in views:
            for run in view.execution_operation.managed_runs:
                try:
                    if not run.keeper.drain(budget).drained or run.keeper.closing_exchange_pending:
                        drained = False
                except BaseException as control:
                    drained = False
                    if interrupted is None:
                        interrupted = control
        if interrupted is not None:
            raise interrupted
        if not drained:
            raise StateError("VM managed keeper drain remains unsettled")
        if not self.owner.close_local_delivery(budget):
            raise StateError("VM operation retains unsettled helper delivery")
        for view in views:
            if view.execution_operation.active_inline_calls:
                view.execution_operation.observe_inline_cleanup(budget)
        # Proven helper closure can still retain a completed handoff's borrow.
        # Retire every route before resolving lifetimes or dispatching cleanup.
        for view in views:
            view.execution_operation.retry_inline_bookkeeping(finishing=True)
        # Retire every route's completed file and namespace borrow before any
        # execution lifetime resolution, which also needs the shared borrow.
        for view in views:
            view.file_operation.settle_local_file_bookkeeping(budget)
            view.file_operation.settle_download_bookkeeping()
            view.file_operation.settle_completed_bookkeeping()
        for route in self.routes:
            if route.namespace is not None:
                route.namespace.settle_local(budget)
        for view in views:
            try:
                view.execution_operation.finish()
            except BaseException as control:
                # Closed helpers can release scratch while unknown launch
                # facts retain the independent START obligation and claim.
                try:
                    view.execution_operation.finish_scratch_cleanup(budget)
                except BaseException:
                    raise control from control.__cause__
                raise
            view.execution_operation.finish_scratch_cleanup(budget)
        for view in views:
            for run in view.execution_operation.managed_runs:
                run.finish_cleanup(budget)
        if not self.local_delivery.close(budget) or not self.owner.close_local_delivery(budget):
            raise StateError("VM operation retains unsettled local delivery")
        if not self._components_settled():
            raise StateError("VM operation retains unsettled work")
        access_settled = self._settle_access(budget)
        obligations = self.owner.list_pending_lifecycle_obligations()
        if access_settled and self.local_delivery.settled and self._components_settled() and not obligations:
            self.owner.seal_lifecycle_obligations()
            self.owner.record_effects_resolved()
            # A release may commit before its reply is interrupted. Retry the
            # owner's exact reconciliation, not reads against the removed claim.
            self.finalizing = True
            self.owner.close()
            return
        raise StateError("VM operation retains unsettled work")

    def _settle_access(self, deadline: Deadline) -> bool:
        """Settle the retained platform after route and local delivery cleanup."""
        return self.access is None or self.access.settle(deadline)


def _prepare_target(
    workflow: _Workflow,
    route: _RoutePreparation,
    vm: VMRow,
    platform: VMPlatform,
    locator: ProviderLocator,
    binding: NativeExecutionBinding,
) -> VMTargetPreparation:
    """Retain common guest preparation before any platform guest check."""
    assert workflow.access is not None
    try:
        route.preparation = prepare_managed_vm_target_from_platform(
            vm,
            platform,
            locator,
            binding,
            deadline=workflow.deadline,
            owner=workflow.owner,
            observe_locator=workflow.access.observe_locator,
        )
    except BaseException as control:
        if isinstance(control.__cause__, VMTargetPreparationControlFact):
            route.preparation = control.__cause__.preparation
        raise
    provider_locator_remaining(workflow.deadline, vm_name=vm.name)
    return route.preparation


def _require_vm_row(db: Database, vm_name: str, platform: VMPlatform, ctx: RunContext) -> VMRow:
    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError("VM operation requires an existing VM", entity_kind="vm", entity_name=vm_name)
    if vm.site != platform.site_name:
        raise StateError("VM operation platform does not match the VM site", entity_kind="vm", entity_name=vm_name)
    scope = ctx.operation_scope
    if scope is not None and scope.vm is not None and scope.vm != vm_name:
        raise ValidationError("VM operation context names a different VM")
    return vm


def _prepare(
    workflow: _Workflow,
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    trusted_root: PurePosixPath,
    agent_name: str | None,
    route: ExecutionRoute,
) -> VMOperation:
    vm = _require_vm_row(db, vm_name, platform, ctx)
    validate_vm_instance_marker(vm.instance_marker)
    try:
        encode_account_request(AccountRequest("0" * 32, vm.admin_username))
    except AccountRequestError as error:
        raise ValidationError("VM operation requires a valid administrative account") from error
    agent: AgentRow | None = None
    if agent_name is not None:
        agent = db.get_agent(agent_name)
        if agent is None:
            raise NotFoundError("VM operation requires an existing agent", entity_kind="agent", entity_name=agent_name)
        if agent.vm_name != vm_name:
            raise StateError(
                "VM operation agent does not belong to the selected VM",
                entity_kind="agent",
                entity_name=agent_name,
            )
        try:
            encode_account_request(AccountRequest("0" * 32, agent.linux_user))
        except AccountRequestError as error:
            raise ValidationError("VM operation requires a valid agent account") from error
    canonical_binding = (
        ssh_execution_binding(vm, vm.tailscale_host, require_ssh_settings(vm, ctx, ctx.config))
        if route is ExecutionRoute.CANONICAL
        else None
    )
    selected_vm = (vm.site, vm.instance_marker, vm.admin_username, vm.tailscale_host)
    selected_agent = (agent.vm_name, agent.linux_user) if agent is not None else None
    access = platform.build_native_execution_access(vm, ctx, owner=workflow.owner, custody=workflow.local_delivery)
    workflow.access = access
    if route is ExecutionRoute.NATIVE and access.native_configuration_check is not None:
        access.native_configuration_check()
    power = access.observe_power(workflow.deadline)
    if workflow.deadline.expired:
        raise StateError("VM power observation exceeded its deadline", entity_kind="vm", entity_name=vm_name)
    if type(power) is not VMStatus or power not in {VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED}:
        raise StateError("VM power cannot authorize activation", entity_kind="vm", entity_name=vm_name)
    if power in {VMStatus.STOPPED, VMStatus.DEALLOCATED} and vm.operator_stopped:
        raise StateError("Operator-stopped VM cannot be started automatically", entity_kind="vm", entity_name=vm_name)
    locator = access.ensure_available(power, workflow.deadline)
    selected_availability = access.independent_availability

    def check_canonical(deadline: Deadline) -> None:
        """Revalidate the retained provider and pinned canonical selection without activation."""
        # Cleanup retains this selection check after public body admission closes.
        provider_locator_remaining(deadline, vm_name=vm_name)
        claim = db.operations.inspect(workflow.owner.ownership.scope)
        if claim is None or claim.ownership != workflow.owner.ownership:
            raise StateError("Canonical VM operation no longer owns its selected claim")
        current = _require_vm_row(db, vm_name, platform, ctx)
        if (current.site, current.instance_marker, current.admin_username, current.tailscale_host) != selected_vm:
            raise StateError("Canonical VM selection changed")
        if agent is not None:
            current_agent = db.get_agent(agent.name)
            if current_agent is None or (current_agent.vm_name, current_agent.linux_user) != selected_agent:
                raise StateError("Canonical VM agent selection changed")
        if access.observe_power(deadline) is not VMStatus.RUNNING:
            raise StateError("Canonical VM availability is unconfirmed")
        if access.observe_locator(deadline) != locator:
            raise StateError("Canonical VM provider locator changed or is unavailable")
        if access.independent_availability is not selected_availability:
            raise StateError("Canonical VM platform availability changed")
        provider_locator_remaining(deadline, vm_name=vm_name)

    if route is ExecutionRoute.NATIVE:
        binding = access.prepare_native_route(workflow.deadline)
        route_check = access.route_check
    else:
        assert canonical_binding is not None
        binding = replace(canonical_binding, _independent_availability=selected_availability)
        route_check = check_canonical

    return _prepare_route(
        workflow,
        db,
        vm,
        platform,
        access,
        locator,
        binding,
        trusted_root=trusted_root,
        agent=agent,
        route=route,
        route_check=route_check,
    )


def _prepare_route(
    workflow: _Workflow,
    db: Database,
    vm: VMRow,
    platform: VMPlatform,
    access: OwnedVMPlatformAccess,
    locator: ProviderLocator,
    binding: NativeExecutionBinding,
    *,
    trusted_root: PurePosixPath,
    agent: AgentRow | None,
    route: ExecutionRoute,
    route_check: Callable[[Deadline], None] | None,
) -> VMOperation:
    """Assemble one route using retained availability and explicit route evidence.

    The outer workflow retains platform access before calling this assembly.
    Each route remains addressable even if target or account preparation raises.
    """
    if workflow.body_admission_closed:
        raise StateError("VM operation no longer admits route preparation")
    assert workflow.access is access
    retained = _RoutePreparation()
    workflow.routes.append(retained)
    vm_name = vm.name
    instance_marker = validate_vm_instance_marker(vm.instance_marker)

    def check_route() -> None:
        if workflow.body_admission_closed or retained.body_admission_closed:
            raise StateError("VM operation no longer admits body work")
        if route_check is not None:
            route_check(workflow.deadline)
        provider_locator_remaining(workflow.deadline, vm_name=vm_name)

    check_route()
    preparation = _prepare_target(workflow, retained, vm, platform, locator, binding)
    result = preparation.guest_result if preparation is not None else None
    observation = result.observation if result is not None else None
    guest = observation.identity if observation is not None else None
    if (
        binding is None
        or guest is None
        or preparation is None
        or preparation.status is not VMTargetPreparationStatus.PREPARED
    ):
        raise StateError(
            "VM target preparation did not establish an exact guest", entity_kind="vm", entity_name=vm_name
        )
    target = preparation.target
    assert target is not None
    if access.guest_check is not None:
        access.guest_check(guest)
    check_route()

    try:
        identity = prepare_target_identity(
            binding.carrier,
            delivery_account=binding.delivery_account,
            workload_account=vm.admin_username,
            include_elevated=True,
            runtime_selection=binding.runtime_selection,
            deadline=workflow.deadline,
            owner=workflow.owner,
        )
    except BaseException as control:
        if isinstance(control.__cause__, TargetIdentityControlFact):
            retained.identity = control.__cause__.preparation
        raise
    retained.identity = identity
    if (
        workflow.deadline.expired
        or identity.status is not TargetIdentityStatus.PREPARED
        or identity.ordinary_plan is None
        or identity.elevated_plan is None
    ):
        raise StateError("VM account preparation is unavailable", entity_kind="vm", entity_name=vm_name)
    check_route()
    bootstrap = _NumericGuestBootstrap(identity.elevated_plan, guest)
    agent_identity: TargetIdentityPreparation | None = None
    if agent is not None:
        try:
            agent_identity = prepare_target_identity(
                binding.carrier,
                delivery_account=binding.delivery_account,
                workload_account=agent.linux_user,
                include_elevated=False,
                runtime_selection=binding.runtime_selection,
                deadline=workflow.deadline,
                owner=workflow.owner,
                bootstrap=bootstrap,
            )
        except BaseException as control:
            if isinstance(control.__cause__, TargetIdentityControlFact):
                retained.agent_identity = control.__cause__.preparation
            raise
        retained.agent_identity = agent_identity
        if (
            workflow.deadline.expired
            or agent_identity.status is not TargetIdentityStatus.PREPARED
            or agent_identity.ordinary_plan is None
        ):
            raise StateError("VM agent account preparation is unavailable", entity_kind="agent", entity_name=agent.name)
        check_route()
        retained.namespace = NamespacePreparation(
            workflow.owner,
            binding.carrier,
            target,
            agent_identity.ordinary_plan,
            bootstrap,
            binding.runtime_selection,
            workflow.deadline,
        )
        retained.namespace.prepare()
        check_route()
    file_operation = FileOperation(workflow.owner, target, bootstrap=bootstrap)
    execution_operation = ExecutionOperation(
        workflow.owner,
        target,
        bootstrap=bootstrap,
        managed_repository=ManagedRunRepository(db),
        resource_owner=ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "vm:" + instance_marker),
        native_binding=binding,
        route_check=route_check,
    )

    def selected_deadline() -> Deadline:
        if workflow.body_admission_closed or retained.body_admission_closed:
            raise StateError("VM operation no longer admits body work")
        return workflow.deadline

    files = FileAccess(
        file_operation,
        binding.carrier,
        trusted_root=trusted_root,
        runtime_selection=binding.runtime_selection,
        ordinary_plan=identity.ordinary_plan,
        elevated_plan=identity.elevated_plan,
        entity_kind="vm",
        entity_name=vm_name,
        deadline=selected_deadline,
        route_check=route_check,
    )
    execution = ExecutionAccess(
        execution_operation,
        binding.carrier,
        runtime_selection=binding.runtime_selection,
        ordinary_plan=identity.ordinary_plan,
        elevated_plan=identity.elevated_plan,
        entity_kind="vm",
        entity_name=vm_name,
        deadline=selected_deadline,
    )
    expected = identity.ordinary_plan.expected
    execution_target = ExecutionTarget(
        execution,
        files,
        binding.carrier.features,
        ExecutionIdentity(vm.admin_username, expected.euid, expected.egid, expected.groups),
        route,
    )
    agent_target = None
    if agent is not None and agent_identity is not None:
        assert agent_identity.ordinary_plan is not None
        agent_files = FileAccess(
            file_operation,
            binding.carrier,
            trusted_root=trusted_root,
            runtime_selection=binding.runtime_selection,
            ordinary_plan=agent_identity.ordinary_plan,
            elevated_plan=None,
            entity_kind="agent",
            entity_name=agent.name,
            deadline=selected_deadline,
            route_check=route_check,
        )
        agent_execution = ExecutionAccess(
            execution_operation,
            binding.carrier,
            runtime_selection=binding.runtime_selection,
            ordinary_plan=agent_identity.ordinary_plan,
            elevated_plan=None,
            entity_kind="agent",
            entity_name=agent.name,
            deadline=selected_deadline,
        )
        expected = agent_identity.ordinary_plan.expected
        agent_target = ExecutionTarget(
            agent_execution,
            agent_files,
            binding.carrier.features,
            ExecutionIdentity(agent.linux_user, expected.euid, expected.egid, expected.groups),
            route,
        )
    views = VMOperation(
        workflow.owner, files, execution, file_operation, execution_operation, execution_target, agent_target
    )
    retained.views = views
    return views


@contextmanager
def _vm_operation(
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    route: ExecutionRoute,
    deadline: Deadline,
    trusted_root: PurePosixPath,
    agent_name: str | None = None,
) -> Iterator[VMOperation]:
    """Claim and close one selected-route operation with shared prepared views."""
    if type(route) is not ExecutionRoute or route not in {ExecutionRoute.NATIVE, ExecutionRoute.CANONICAL}:
        raise ValidationError("VM execution requires an explicit native or canonical route")
    if type(db) is not Database or not isinstance(platform, VMPlatform):
        raise ValidationError("VM operation requires core database and platform values")
    if type(ctx) is not RunContext or type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
        raise ValidationError("VM operation requires context and a live finite deadline")
    validate_name(vm_name, max_length=MAX_VM_NAME_LENGTH)
    if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
        raise ValidationError("VM operation requires a trusted normalized file root")

    owner = OperationOwner.acquire(
        db.operations, OperationScope(OperationResourceKind.VM, vm_name), "vm-execution-operation"
    )
    workflow = _Workflow(owner, deadline)
    primary: BaseException | None = None
    try:
        yield _prepare(workflow, db, vm_name, platform, ctx, trusted_root, agent_name, route)
    except BaseException as control:
        primary = control
        raise
    finally:
        try:
            workflow.close()
        except BaseException as cleanup:
            fact = VMOperationControlFact(workflow)
            if primary is None:
                fact.__cause__ = cleanup.__cause__
                raise cleanup from fact
            primary.add_note("VM operation teardown retained unresolved custody")
            fact.__cause__ = primary.__cause__
            primary.__cause__ = fact
            primary.__suppress_context__ = True


@contextmanager
def vm_execution_context(
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    route: ExecutionRoute,
    deadline: Deadline,
    trusted_root: PurePosixPath,
    agent_name: str | None = None,
) -> Iterator[RunContext]:
    """Bind prepared selected-route targets for one operation-owned context lifetime.

    Scope describes the caller's operation; VM and optional agent selection
    come only from the explicit arguments. The shared operation owns admission,
    preparation and aggregate teardown, including failures during context assembly.
    """
    with _vm_operation(
        db, vm_name, platform, ctx, route=route, deadline=deadline, trusted_root=trusted_root, agent_name=agent_name
    ) as operation:
        yield RunContext(
            config=ctx.config,
            operation_scope=ctx.operation_scope,
            admin_target=ctx.admin_target(),
            agent_target=ctx.agent_target(),
            admin_execution_target=operation.target,
            agent_execution_target=operation.agent_target,
            agent_execution_target_absence=ctx.agent_execution_target_absence() or TargetAbsence.UNAVAILABLE,
            # Trusted core stage assembly preserves the exact scoped reader.
            secrets=ctx._secrets,  # noqa: SLF001
        )
