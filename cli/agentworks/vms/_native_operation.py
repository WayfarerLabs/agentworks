"""Private core-owned existing-VM native operation and availability boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.capabilities.base import RunContext, ScopeLevel
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.execution._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation
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
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name
from agentworks.operations import OperationOwner
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_preparation import (
    VMTargetPreparationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import VMRow
    from agentworks.vms._native_execution_access import OwnedNativePlatformAccess


@dataclass(frozen=True, slots=True)
class NativeVMOperation:
    """Two private bound views sharing one exact VM claim and deadline."""

    owner: OperationOwner
    files: FileAccess
    execution: ExecutionAccess
    file_operation: FileOperation
    execution_operation: ExecutionOperation


class NativeVMOperationControlFact(Exception):
    """Safe handoff of exact in-memory cleanup custody after failed teardown."""

    def __init__(self, workflow: _Workflow) -> None:
        self._workflow = workflow
        super().__init__("native VM operation retains cleanup custody")

    def retry_cleanup(self, deadline: Deadline) -> None:
        """Retry only aggregate cleanup under a fresh finite observation budget."""
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("Native VM cleanup requires a live finite deadline")
        self._workflow.close(cleanup_deadline=deadline)


@dataclass
class _Workflow:
    owner: OperationOwner
    deadline: Deadline
    local_delivery: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody, repr=False)
    access: OwnedNativePlatformAccess | None = None
    identity: TargetIdentityPreparation | None = None
    views: NativeVMOperation | None = None
    finalizing: bool = False

    def _components_settled(self) -> bool:
        preparation = self.access.preparation if self.access is not None else None
        if preparation is not None and (
            preparation.pending_remote_effects
            or preparation.coordination_uncertain
            or preparation.requires_owner_retention
        ):
            return False
        identity = self.identity
        if identity is not None and (
            identity.pending_remote_effects or identity.coordination_uncertain or identity.requires_owner_retention
        ):
            return False
        views = self.views
        if views is None:
            return True
        files = views.file_operation
        execution = views.execution_operation
        return not (
            files.active_downloads
            or files.unfinished_downloads
            or files.has_unfinished_local_download_call
            or files.retained_local_download_stage is not None
            or files.active_uploads
            or files.active_package_uploads
            or files.unfinished_uploads
            or files.unfinished_package_uploads
            or files.active_json_updates
            or files.unfinished_json_updates
            or files.active_stats
            or files.active_inventories
            or files.active_removals
            or files.active_metadata
            or files.unfinished_owned_files
            or execution.active_inline_calls
            or execution.unfinished_inline_executions
            or any(not run.cleanup_complete for run in execution.managed_runs)
        )

    def close(self, *, cleanup_deadline: Deadline | None = None) -> None:
        """Stop body admission, then release only on aggregate exact settlement."""
        if self.finalizing:
            self.owner.close()
            return
        self.owner.stop_admission()
        budget = self.deadline if cleanup_deadline is None else cleanup_deadline
        if self.views is not None:
            drained = True
            interrupted: BaseException | None = None
            for run in self.views.execution_operation.managed_runs:
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
                raise StateError("Native VM managed keeper drain remains unsettled")
            self.views.execution_operation.finish()
        if not self.local_delivery.close(budget) or not self.owner.close_local_delivery(budget):
            raise StateError("Native VM operation retains unsettled local delivery")
        if self.views is not None:
            for run in self.views.execution_operation.managed_runs:
                run.finish_cleanup(budget)
        if not self._components_settled():
            raise StateError("Native VM operation retains unsettled work")
        access_settled = self.access is None or self.access.settle(budget)
        obligations = self.owner.list_pending_lifecycle_obligations()
        if access_settled and self.local_delivery.settled and not obligations:
            self.owner.seal_lifecycle_obligations()
            self.owner.record_effects_resolved()
            # A release may commit before its reply is interrupted. Retry the
            # owner's exact reconciliation, not reads against the removed claim.
            self.finalizing = True
            self.owner.close()
            return
        raise StateError("Native VM operation retains unsettled work")


def _require_vm_row(db: Database, vm_name: str, platform: VMPlatform, ctx: RunContext) -> VMRow:
    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError("Native VM operation requires an existing VM", entity_kind="vm", entity_name=vm_name)
    if vm.site != platform.site_name:
        raise StateError(
            "Native VM operation platform does not match the VM site", entity_kind="vm", entity_name=vm_name
        )
    scope = ctx.operation_scope
    if scope is not None and (scope.level is not ScopeLevel.VM or scope.vm != vm_name):
        raise ValidationError("Native VM operation requires the selected VM context")
    return vm


def _prepare(
    workflow: _Workflow,
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    trusted_root: PurePosixPath,
) -> NativeVMOperation:
    vm = _require_vm_row(db, vm_name, platform, ctx)
    instance_marker = validate_vm_instance_marker(vm.instance_marker)
    try:
        encode_account_request(AccountRequest("0" * 32, vm.admin_username))
    except AccountRequestError as error:
        raise ValidationError("Native VM operation requires a valid administrative account") from error
    power = platform.observe_execution_power(vm, ctx, deadline=workflow.deadline, custody=workflow.local_delivery)
    if workflow.deadline.expired:
        raise StateError("Native VM power observation exceeded its deadline", entity_kind="vm", entity_name=vm_name)
    if type(power) is not VMStatus or power not in {VMStatus.RUNNING, VMStatus.STOPPED}:
        raise StateError("Native VM power cannot authorize activation", entity_kind="vm", entity_name=vm_name)
    if power is VMStatus.STOPPED and vm.operator_stopped:
        raise StateError("Operator-stopped VM cannot be started automatically", entity_kind="vm", entity_name=vm_name)
    access = platform.build_native_execution_access(vm, ctx, owner=workflow.owner, custody=workflow.local_delivery)
    workflow.access = access
    access.prepare(power, workflow.deadline)
    preparation = access.preparation
    binding = access.binding
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
            "Native VM target preparation did not establish an exact guest", entity_kind="vm", entity_name=vm_name
        )
    target = preparation.target
    assert target is not None

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
            workflow.identity = control.__cause__.preparation
        raise
    workflow.identity = identity
    if (
        identity.status is not TargetIdentityStatus.PREPARED
        or identity.ordinary_plan is None
        or identity.elevated_plan is None
    ):
        raise StateError("Native VM account preparation is unavailable", entity_kind="vm", entity_name=vm_name)
    bootstrap = _NumericGuestBootstrap(identity.elevated_plan, guest)
    file_operation = FileOperation(workflow.owner, target, bootstrap=bootstrap)
    execution_operation = ExecutionOperation(
        workflow.owner,
        target,
        bootstrap=bootstrap,
        managed_repository=ManagedRunRepository(db),
        resource_owner=ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "vm:" + instance_marker),
        native_binding=binding,
        route_check=access.route_check,
    )

    def selected_deadline() -> Deadline:
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
    views = NativeVMOperation(workflow.owner, files, execution, file_operation, execution_operation)
    workflow.views = views
    return views


@contextmanager
def native_vm_operation(
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    deadline: Deadline,
    trusted_root: PurePosixPath,
) -> Iterator[NativeVMOperation]:
    """Claim, admit, prepare and close one administrative native VM operation."""
    if type(db) is not Database or not isinstance(platform, VMPlatform):
        raise ValidationError("Native VM operation requires core database and platform values")
    if type(ctx) is not RunContext or type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
        raise ValidationError("Native VM operation requires context and a live finite deadline")
    validate_name(vm_name, max_length=MAX_VM_NAME_LENGTH)
    if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
        raise ValidationError("Native VM operation requires a trusted normalized file root")

    owner = OperationOwner.acquire(
        db.operations, OperationScope(OperationResourceKind.VM, vm_name), "native-vm-operation"
    )
    workflow = _Workflow(owner, deadline)
    primary: BaseException | None = None
    try:
        yield _prepare(workflow, db, vm_name, platform, ctx, trusted_root)
    except BaseException as control:
        primary = control
        raise
    finally:
        try:
            workflow.close()
        except BaseException as cleanup:
            fact = NativeVMOperationControlFact(workflow)
            if primary is None:
                fact.__cause__ = cleanup.__cause__
                raise cleanup from fact
            primary.add_note("Native VM operation teardown retained unresolved custody")
            fact.__cause__ = primary.__cause__
            primary.__cause__ = fact
            primary.__suppress_context__ = True
