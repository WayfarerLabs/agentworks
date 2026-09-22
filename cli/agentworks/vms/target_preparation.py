"""Owned preparation of one managed VM target identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import ProviderLocatorUnavailable
from agentworks.db import OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution._vm_guest_identity import (
    VMGuestIdentityObservationResult,
    VMGuestIdentityObservationState,
    observe_vm_guest_identity,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import Dispatch
from agentworks.operations import release_borrow_after_custody
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_identity import compose_managed_vm_target_identity

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProviderLocatorObservation
    from agentworks.db import VMRow
    from agentworks.execution._managed_runs import ManagedTargetIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner


class VMTargetPreparationStatus(StrEnum):
    """Closed outcome state for one managed VM target preparation."""

    PREPARED = "prepared"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class VMTargetPreparationFailure(StrEnum):
    """Closed non-payload reason a VM target was not prepared."""

    LOCATOR_UNAVAILABLE = "locator_unavailable"
    MARKER_MISSING = "marker_missing"
    DEADLINE = "deadline"
    DISPATCH = "dispatch"
    TERMINATION = "termination"
    CARRIER = "carrier"
    RUNTIME_PREREQUISITE = "runtime_prerequisite"
    GUEST_REFUSED = "guest_refused"
    GUEST_OBSERVATION = "guest_observation"
    IDENTITY = "identity"


@dataclass(frozen=True, slots=True, repr=False)
class VMTargetPreparation:
    """Prepared identity or bounded facts explaining why it is unavailable."""

    status: VMTargetPreparationStatus
    target: ManagedTargetIdentity | None
    guest_result: VMGuestIdentityObservationResult | None
    failure: VMTargetPreparationFailure | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class VMTargetPreparationControlFact(Exception):
    """Safe custody fact attached to escaping preparation control flow."""

    def __init__(self, preparation: VMTargetPreparation) -> None:
        self.preparation = preparation
        super().__init__("managed VM target preparation stopped with retained operation state")


@dataclass(slots=True, repr=False)
class _State:
    operation: BorrowedFixedHelperCarrier
    guest_result: VMGuestIdentityObservationResult | None = None
    target: ManagedTargetIdentity | None = None
    failure: VMTargetPreparationFailure | None = None
    deadline_exceeded: bool = False

    def fail(self, failure: VMTargetPreparationFailure) -> None:
        if self.failure is None:
            self.failure = failure

    def finish(self) -> VMTargetPreparation:
        pending = self.operation.pending_remote_effects
        coordination = self.operation.coordination_uncertain
        retain = self.operation.requires_owner_retention
        if self.target is not None and self.failure is None and not retain:
            status = VMTargetPreparationStatus.PREPARED
        elif retain:
            status = VMTargetPreparationStatus.UNCERTAIN
        else:
            status = VMTargetPreparationStatus.FAILED
        return VMTargetPreparation(
            status,
            self.target,
            self.guest_result,
            self.failure,
            self.deadline_exceeded,
            pending,
            coordination,
            retain,
        )


def prepare_managed_vm_target(
    vm: VMRow,
    locator: ProviderLocatorObservation,
    binding: NativeExecutionBinding,
    *,
    deadline: Deadline,
    owner: OperationOwner,
) -> VMTargetPreparation:
    """Observe and compose one managed VM target under one existing owner.

    This neither activates a route nor changes persisted identity state.  It
    borrows the supplied owner only for the one helper attempt and leaves any
    unresolved attempt durably retained for the caller's later recovery path.
    """
    _validate_operation_boundary(vm, deadline, owner)
    if type(locator) is ProviderLocatorUnavailable:
        return _failed(VMTargetPreparationFailure.LOCATOR_UNAVAILABLE)
    if vm.instance_marker is None:
        return _failed(VMTargetPreparationFailure.MARKER_MISSING)
    validate_vm_instance_marker(vm.instance_marker)
    if deadline.expired:
        return _failed(VMTargetPreparationFailure.DEADLINE, deadline_exceeded=True)

    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(binding.carrier, borrow)
    state = _State(operation)
    try:
        try:
            result = observe_vm_guest_identity(
                operation,
                runtime_selection=binding.runtime_selection,
                deadline=deadline,
            )
            state.guest_result = result
            normal = operation.settle(result.dispatch, result.carrier_completion)
            if not normal:
                state.fail(
                    VMTargetPreparationFailure.DISPATCH
                    if result.dispatch is Dispatch.NOT_SENT
                    else VMTargetPreparationFailure.TERMINATION
                )
                _record_deadline(state, deadline)
                return state.finish()
            if _record_deadline(state, deadline):
                return state.finish()
            if result.carrier_failure is not None:
                state.fail(VMTargetPreparationFailure.CARRIER)
                return state.finish()
            if result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY:
                state.fail(VMTargetPreparationFailure.RUNTIME_PREREQUISITE)
                return state.finish()
            observation = result.observation
            if observation is None:
                state.fail(VMTargetPreparationFailure.GUEST_OBSERVATION)
                return state.finish()
            if observation.state is VMGuestIdentityObservationState.REFUSED:
                state.fail(VMTargetPreparationFailure.GUEST_REFUSED)
                return state.finish()
            if (
                observation.state is not VMGuestIdentityObservationState.RESOLVED
                or type(observation.identity) is not VMGuestIdentity
            ):
                state.fail(VMTargetPreparationFailure.GUEST_OBSERVATION)
                return state.finish()
            try:
                state.target = compose_managed_vm_target_identity(vm, locator, observation.identity)
            except StateError:
                state.fail(VMTargetPreparationFailure.IDENTITY)
            _record_deadline(state, deadline)
            return state.finish()
        except BaseException as control:
            _record_deadline(state, deadline)
            raise control from VMTargetPreparationControlFact(state.finish())
    finally:
        release_borrow_after_custody(borrow)


def _failed(
    failure: VMTargetPreparationFailure,
    *,
    deadline_exceeded: bool = False,
) -> VMTargetPreparation:
    return VMTargetPreparation(
        VMTargetPreparationStatus.FAILED,
        None,
        None,
        failure,
        deadline_exceeded,
    )


def _record_deadline(state: _State, deadline: Deadline) -> bool:
    if not deadline.expired:
        return False
    state.deadline_exceeded = True
    state.target = None
    state.fail(VMTargetPreparationFailure.DEADLINE)
    return True


def _validate_operation_boundary(
    vm: VMRow,
    deadline: Deadline,
    owner: OperationOwner,
) -> None:
    if deadline.expires_at is None:
        raise ValidationError("Managed VM target preparation requires a finite deadline")
    scope = owner.ownership.scope
    if scope.resource_kind is not OperationResourceKind.VM or scope.resource_name != vm.name:
        raise ValidationError("Managed VM target preparation requires ownership of the exact VM")
