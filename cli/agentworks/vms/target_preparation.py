"""Owned preparation of one managed VM target identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.db import OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier, FixedObservationCarrier
from agentworks.execution._managed_runs import ManagedTargetIdentity
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
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocatorObservation, VMPlatform
    from agentworks.db import VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationBorrow, OperationOwner


class VMTargetPreparationStatus(StrEnum):
    """Closed outcome state for one managed VM target preparation."""

    PREPARED = "prepared"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class VMTargetPreparationFailure(StrEnum):
    """Closed non-payload reason a VM target was not prepared."""

    LOCATOR_UNAVAILABLE = "locator_unavailable"
    LOCATOR_UNCONFIRMED = "locator_unconfirmed"
    LOCATOR_CHANGED = "locator_changed"
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

    def __post_init__(self) -> None:
        if self.pending_remote_effects and not self.requires_owner_retention:
            raise ValidationError("Pending remote effects require owner retention")
        if self.coordination_uncertain and not self.requires_owner_retention:
            raise ValidationError("Uncertain coordination requires owner retention")
        if self.status is VMTargetPreparationStatus.PREPARED:
            if (
                type(self.target) is not ManagedTargetIdentity
                or self.failure is not None
                or self.deadline_exceeded
                or self.requires_owner_retention
            ):
                raise ValidationError("Prepared VM target requires a complete unretained identity")
        elif self.status is VMTargetPreparationStatus.FAILED:
            if self.target is not None or self.requires_owner_retention:
                raise ValidationError("Failed VM target cannot carry identity or retained effects")
        elif self.status is VMTargetPreparationStatus.UNCERTAIN:
            if not self.requires_owner_retention:
                raise ValidationError("Uncertain VM target requires owner retention")
        else:
            raise ValidationError("VM target preparation requires a known status")


class VMTargetPreparationControlFact(Exception):
    """Safe custody fact attached to escaping preparation control flow."""

    def __init__(self, preparation: VMTargetPreparation) -> None:
        self.preparation = preparation
        super().__init__("managed VM target preparation stopped with custody facts")


@dataclass(slots=True, repr=False)
class _State:
    operation: FixedObservationCarrier
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


def prepare_managed_vm_target_from_platform(
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
    expected_locator: ProviderLocator,
    binding: NativeExecutionBinding,
    *,
    deadline: Deadline,
    owner: OperationOwner,
) -> VMTargetPreparation:
    """Prepare one selected target using the caller's observed route and binding.

    The caller observes the locator before resolving and owning the binding.
    This borrow compares the locator before the guest probe and confirms it
    afterward. It owns no outer operation or platform route lifetime.
    """
    _validate_operation_boundary(vm, deadline, owner)
    if platform.site_name != vm.site:
        raise ValidationError("Managed VM target preparation requires the VM's bound platform")
    preflight = _preflight_marker_and_deadline(vm, deadline)
    if preflight is not None:
        return preflight

    borrow = owner.borrow()
    try:
        result = _prepare_selected_platform_with_borrow(
            vm, platform, ctx, expected_locator, binding, deadline=deadline, borrow=borrow
        )
    except BaseException as control:
        _release_preparation_borrow(borrow, control=control)
        raise
    _release_preparation_borrow(borrow, preparation=result)
    return result


def _prepare_selected_platform_with_borrow(
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
    expected_locator: ProviderLocator,
    binding: NativeExecutionBinding,
    *,
    deadline: Deadline,
    borrow: OperationBorrow,
) -> VMTargetPreparation:
    early = binding._early_guest_facts_route
    operation = BorrowedFixedHelperCarrier(binding.carrier if early is None else early.carrier, borrow)
    return _prepare_selected_platform_observations(
        vm, platform, ctx, expected_locator, binding, operation, deadline=deadline
    )


def _prepare_selected_platform_observations(
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
    expected_locator: ProviderLocator,
    binding: NativeExecutionBinding,
    operation: FixedObservationCarrier,
    *,
    deadline: Deadline,
) -> VMTargetPreparation:
    """Confirm the selected locator around a caller-owned fixed guest probe."""
    locator = platform.observe_provider_locator(vm, ctx, deadline=deadline)
    if deadline.expired:
        return _failed(VMTargetPreparationFailure.DEADLINE, deadline_exceeded=True)
    if type(locator) is ProviderLocatorUnavailable:
        return _failed(VMTargetPreparationFailure.LOCATOR_UNAVAILABLE)
    locator = _validated_provider_locator(locator)
    if locator != expected_locator:
        return _failed(VMTargetPreparationFailure.LOCATOR_CHANGED)

    preparation = _prepare_managed_vm_target_observations(vm, expected_locator, binding, operation, deadline=deadline)
    if preparation.status is not VMTargetPreparationStatus.PREPARED:
        return preparation

    try:
        confirmation = platform.observe_provider_locator(vm, ctx, deadline=deadline)
        if deadline.expired:
            return _failed_after_guest(preparation, VMTargetPreparationFailure.DEADLINE, deadline_exceeded=True)
        if type(confirmation) is ProviderLocatorUnavailable:
            return _failed_after_guest(preparation, VMTargetPreparationFailure.LOCATOR_UNCONFIRMED)
        confirmation = _validated_provider_locator(confirmation)
    except BaseException as control:
        deadline_exceeded = deadline.expired
        fact = _failed_after_guest(
            preparation,
            VMTargetPreparationFailure.DEADLINE
            if deadline_exceeded
            else VMTargetPreparationFailure.LOCATOR_UNCONFIRMED,
            deadline_exceeded=deadline_exceeded,
        )
        raise control from VMTargetPreparationControlFact(fact)
    if confirmation != locator:
        return _failed_after_guest(preparation, VMTargetPreparationFailure.LOCATOR_CHANGED)
    return preparation


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
    preflight = _preflight_marker_and_deadline(vm, deadline)
    if preflight is not None:
        return preflight

    borrow = owner.borrow()
    try:
        preparation = _prepare_managed_vm_target_with_borrow(vm, locator, binding, deadline=deadline, borrow=borrow)
    except BaseException as control:
        _release_preparation_borrow(borrow, control=control)
        raise
    _release_preparation_borrow(borrow, preparation=preparation)
    return preparation


def _prepare_managed_vm_target_with_borrow(
    vm: VMRow,
    locator: ProviderLocatorObservation,
    binding: NativeExecutionBinding,
    *,
    deadline: Deadline,
    borrow: OperationBorrow,
) -> VMTargetPreparation:
    """Run one guest attempt under custody acquired by either entry point."""
    early = binding._early_guest_facts_route
    operation = BorrowedFixedHelperCarrier(binding.carrier if early is None else early.carrier, borrow)
    return _prepare_managed_vm_target_observations(vm, locator, binding, operation, deadline=deadline)


def _prepare_managed_vm_target_observations(
    vm: VMRow,
    locator: ProviderLocatorObservation,
    binding: NativeExecutionBinding,
    operation: FixedObservationCarrier,
    *,
    deadline: Deadline,
) -> VMTargetPreparation:
    """Classify guest observations before settling the concrete attempt."""
    early = binding._early_guest_facts_route
    state = _State(operation)
    try:
        result = observe_vm_guest_identity(
            operation,
            runtime_selection=binding.runtime_selection,
            deadline=deadline,
            _bootstrap_route=early,
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


def _failed_after_guest(
    preparation: VMTargetPreparation,
    failure: VMTargetPreparationFailure,
    *,
    deadline_exceeded: bool = False,
) -> VMTargetPreparation:
    return VMTargetPreparation(
        VMTargetPreparationStatus.FAILED,
        None,
        preparation.guest_result,
        failure,
        deadline_exceeded,
    )


def _release_preparation_borrow(
    borrow: OperationBorrow,
    *,
    preparation: VMTargetPreparation | None = None,
    control: BaseException | None = None,
) -> None:
    """Release once; attach conservative custody facts if release itself fails."""
    try:
        release_borrow_after_custody(borrow)
    except BaseException as release_error:
        source = preparation
        if source is None and control is not None and isinstance(control.__cause__, VMTargetPreparationControlFact):
            source = control.__cause__.preparation
        uncertain = VMTargetPreparation(
            VMTargetPreparationStatus.UNCERTAIN,
            None,
            source.guest_result if source is not None else None,
            source.failure if source is not None else None,
            source.deadline_exceeded if source is not None else False,
            source.pending_remote_effects if source is not None else False,
            True,
            True,
        )
        fact = VMTargetPreparationControlFact(uncertain)
        if control is not None:
            fact.__cause__ = control
        raise release_error from fact


def _record_deadline(state: _State, deadline: Deadline) -> bool:
    if not deadline.expired:
        return False
    state.deadline_exceeded = True
    state.target = None
    state.fail(VMTargetPreparationFailure.DEADLINE)
    return True


def _preflight_marker_and_deadline(vm: VMRow, deadline: Deadline) -> VMTargetPreparation | None:
    if vm.instance_marker is None:
        return _failed(VMTargetPreparationFailure.MARKER_MISSING)
    validate_vm_instance_marker(vm.instance_marker)
    if deadline.expired:
        return _failed(VMTargetPreparationFailure.DEADLINE, deadline_exceeded=True)
    return None


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


def _validated_provider_locator(observation: object) -> ProviderLocator:
    """Reconstruct a plugin locator before it participates in target identity."""
    if type(observation) is not ProviderLocator:
        raise ValidationError("VM platform returned an invalid provider locator observation")
    try:
        return ProviderLocator(observation.token)
    except AttributeError as error:
        raise ValidationError("VM platform returned an incomplete provider locator") from error
