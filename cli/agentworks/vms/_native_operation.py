"""Private core-owned existing-VM native operation and availability boundary."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from agentworks.capabilities.base import RunContext, ScopeLevel
from agentworks.capabilities.vm_platform.base import ProviderLocator, VMPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.execution._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_paths import normalized_root
from agentworks.execution._managed_runs import ManagedRunRepository
from agentworks.execution._proxmox_activation import (
    ActivationObservation,
    ProxmoxActivation,
    TaskPhase,
)
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityPreparation,
    TargetIdentityStatus,
    prepare_target_identity,
)
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence, HostClientStatus, WSL2AnchorEvidence
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name
from agentworks.operations import OperationOwner
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import VMRow
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carriers.proxmox import ProxmoxCarrier


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
    selected: WSL2OwnedOperation | None = None
    start_attempted: bool = False
    preparation_fact: VMTargetPreparation | None = None
    selected_locator: ProviderLocator | None = None
    selected_binding: NativeExecutionBinding | None = None
    activation: ProxmoxActivation | None = None
    activation_observation: ActivationObservation | None = None
    identity: TargetIdentityPreparation | None = None
    views: NativeVMOperation | None = None
    finalizing: bool = False

    def _activation_settled(self, deadline: Deadline) -> bool:
        activation = self.activation
        if activation is None:
            return True
        row = self.owner.inspect_lifecycle_obligation(activation.obligation_id)
        obligation = activation.obligation
        if row is None and obligation is None:
            # No POST can precede a returned registration handle and durable arm.
            return True
        if (
            obligation is not None
            and row is not None
            and row == obligation._persisted_obligation  # noqa: SLF001
            and row.state is LifecycleObligationState.RESOLVED
        ):
            return True
        activation.reconcile(deadline)
        if activation.obligation is not None and activation.obligation.state is LifecycleObligationState.RESOLVED:
            return True
        if activation.payload.upid is not None:
            _wait_activation(self, deadline)
            return True
        return False

    def _hold_settled(self, cleanup_deadline: Deadline) -> bool:
        selected = self.selected
        if selected is None or not self.start_attempted:
            return True
        hold = selected.hold
        if hold.registration_uncertain:
            return False
        if hold.payload is None:
            evidence = hold.evidence
        else:
            try:
                evidence = hold.release(cleanup_deadline)
            except Exception:
                return False
        if selected.ready is not None and evidence.identity != selected.ready.identity:
            return False
        return _exact_hold_release(evidence)

    def _components_settled(self) -> bool:
        selected = self.selected
        preparation = selected.preparation if selected is not None else None
        if preparation is None:
            preparation = self.preparation_fact
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
        activation_settled = self._activation_settled(budget)
        if not activation_settled:
            raise StateError("Native VM activation remains unsettled")
        obligations = self.owner.list_pending_lifecycle_obligations()
        hold = self.selected.hold.obligation if self.selected is not None else None
        held_row = hold._persisted_obligation if hold is not None else None  # noqa: SLF001
        if any(row != held_row for row in obligations):
            raise StateError("Native VM lifecycle obligations remain unsettled")
        hold_settled = self._hold_settled(budget)
        obligations = self.owner.list_pending_lifecycle_obligations()
        if hold_settled and activation_settled and self.local_delivery.settled and not obligations:
            self.owner.seal_lifecycle_obligations()
            self.owner.record_effects_resolved()
            # A release may commit before its reply is interrupted. Retry the
            # owner's exact reconciliation, not reads against the removed claim.
            self.finalizing = True
            self.owner.close()
            return
        raise StateError("Native VM operation retains unsettled work")


def _exact_hold_release(evidence: WSL2AnchorEvidence) -> bool:
    """Use native settlement plus no-client creation or exact guest absence."""
    if not evidence.local.settled:
        return False
    if evidence.local.host_client_status is HostClientStatus.NOT_CREATED:
        return True
    return evidence.identity is not None and evidence.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED


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


def _remaining(deadline: Deadline) -> float:
    remaining = deadline.remaining()
    assert remaining is not None
    if remaining <= 0:
        raise StateError("Native Proxmox startup exceeded its deadline")
    return remaining


def _pause(deadline: Deadline) -> None:
    time.sleep(min(0.1, _remaining(deadline)))
    _remaining(deadline)


def _wait_activation(workflow: _Workflow, deadline: Deadline) -> None:
    activation = workflow.activation
    assert activation is not None
    while True:
        _remaining(deadline)
        try:
            observation = activation.observe(deadline)
        except Exception:
            raise StateError("Native Proxmox activation observation is unavailable") from None
        workflow.activation_observation = observation
        if observation.request_settled:
            return
        if observation.ha_handoff or observation.phase is not TaskPhase.RUNNING:
            raise StateError("Native Proxmox activation remains unsettled")
        _pause(deadline)


def _guest_info_responded(data: dict[str, object]) -> bool:
    """Validate the required external guest-info shape, without capability claims."""
    result = data.get("result")
    if type(result) is not dict or type(result.get("version")) is not str:
        return False
    commands = result.get("supported_commands")
    return type(commands) is list and all(
        type(command) is dict
        and type(command.get("name")) is str
        and type(command.get("enabled")) is bool
        and ("success-response" not in command or type(command["success-response"]) is bool)
        for command in commands
    )


def _activate_proxmox(
    workflow: _Workflow, vm: VMRow, binding: NativeExecutionBinding, locator: ProviderLocator
) -> None:
    carrier = cast("ProxmoxCarrier", binding.carrier)
    wire = carrier._wire  # noqa: SLF001
    workflow.activation = ProxmoxActivation(
        workflow.owner,
        vm.name,
        wire._connection,
        locator,
        custody=workflow.local_delivery,  # noqa: SLF001
    )
    try:
        workflow.activation.start(workflow.deadline)
    except Exception:
        raise StateError("Native Proxmox activation request is unavailable") from None
    _wait_activation(workflow, workflow.deadline)
    try:
        power = wire.request_power(timeout=_remaining(workflow.deadline), custody=workflow.local_delivery)
    except Exception:
        raise StateError("Native Proxmox current power is unavailable") from None
    _remaining(workflow.deadline)
    if power.get("status") != "running":
        raise StateError("Native Proxmox activation did not establish running power")
    while True:
        workflow.owner.list_pending_lifecycle_obligations()
        try:
            info = wire.request_guest_info(timeout=_remaining(workflow.deadline), custody=workflow.local_delivery)
        except Exception:
            info = {}
        if not workflow.local_delivery.settled:
            raise StateError("Native Proxmox guest information retains unsettled local delivery")
        _remaining(workflow.deadline)
        workflow.owner.list_pending_lifecycle_obligations()
        if _guest_info_responded(info):
            _remaining(workflow.deadline)
            return
        _pause(workflow.deadline)


def _prepare_proxmox(
    workflow: _Workflow, vm: VMRow, platform: VMPlatform, ctx: RunContext, power: VMStatus
) -> tuple[NativeExecutionBinding, VMTargetPreparation, VMGuestIdentity | None]:
    """Select one QGA route, activate if needed, then prepare the exact guest.

    Proxmox has no idle-stop hold. The exact VM owner still covers preparation,
    child obligations and aggregate teardown. Startup does not admit a body.
    """
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    if type(platform) is not ProxmoxPlatform:
        raise StateError("Native VM operation is unavailable on this platform", entity_kind="vm", entity_name=vm.name)
    locator = platform.observe_provider_locator(vm, ctx, deadline=workflow.deadline, custody=workflow.local_delivery)
    if workflow.deadline.expired:
        raise StateError("Native VM locator observation exceeded its deadline", entity_kind="vm", entity_name=vm.name)
    if type(locator) is not ProviderLocator:
        raise StateError("Native VM route is unavailable", entity_kind="vm", entity_name=vm.name)
    workflow.selected_locator = locator
    binding = platform.resolve_native_execution_binding(vm, ctx, deadline=workflow.deadline, config=ctx.config)
    if workflow.deadline.expired:
        raise StateError("Native VM binding resolution exceeded its deadline", entity_kind="vm", entity_name=vm.name)
    workflow.selected_binding = binding
    if power is VMStatus.STOPPED:
        _activate_proxmox(workflow, vm, binding, locator)
    try:
        preparation = prepare_managed_vm_target_from_platform(
            vm,
            platform,
            ctx,
            locator,
            binding,
            deadline=workflow.deadline,
            owner=workflow.owner,
            provider_custody=workflow.local_delivery,
        )
    except BaseException as control:
        if isinstance(control.__cause__, VMTargetPreparationControlFact):
            workflow.preparation_fact = control.__cause__.preparation
        raise
    workflow.preparation_fact = preparation
    result = preparation.guest_result
    observation = result.observation if result is not None else None
    return binding, preparation, observation.identity if observation is not None else None


def _prepare(
    workflow: _Workflow,
    db: Database,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    trusted_root: PurePosixPath,
) -> NativeVMOperation:
    vm = _require_vm_row(db, vm_name, platform, ctx)
    validate_vm_instance_marker(vm.instance_marker)
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
    if type(platform) is WSL2Platform:
        selected = WSL2OwnedOperation.from_platform(
            vm,
            platform,
            ctx,
            owner=workflow.owner,
            deadline=workflow.deadline,
            config=ctx.config,
            provider_custody=workflow.local_delivery,
        )
        if selected is None:
            raise StateError("Native VM route is unavailable", entity_kind="vm", entity_name=vm_name)
        workflow.selected = selected
        workflow.start_attempted = True
        try:
            guest = selected.start_and_prepare(workflow.deadline)
        except BaseException as control:
            if isinstance(control.__cause__, VMTargetPreparationControlFact):
                workflow.preparation_fact = control.__cause__.preparation
            raise
        preparation = selected.preparation
        binding = selected.binding
    else:
        binding, preparation, guest = _prepare_proxmox(workflow, vm, platform, ctx, power)
    if guest is None or preparation is None or preparation.status is not VMTargetPreparationStatus.PREPARED:
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
        native_binding=binding,
        wsl2_route=workflow.selected,
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
