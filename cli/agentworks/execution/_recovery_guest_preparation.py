"""Single-use recovery guest and numeric account preparation custody.

The empty carrier-dispatch row retains this batch's observation debt. It is
independent of a caller-retained availability hold and is not restart drain
evidence. Outer recovery composition must retain its ready hold through the
final file action and establish native drain evidence separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityPreparation,
    _prepare_target_identity_observations,
    _validate_inputs,
)
from agentworks.execution.carrier import Dispatch, ExitStatus
from agentworks.operations import _is_pre_registration_refusal
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    _preflight_marker_and_deadline,
    _prepare_selected_platform_observations,
    _validate_operation_boundary,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorObservation, VMPlatform
    from agentworks.db import VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import (
        Carrier,
        CarrierIO,
        CarrierReport,
        ChannelFeatures,
        Deadline,
        PreparedInvocation,
    )
    from agentworks.operations import (
        LifecycleObligation,
        OperationOwner,
        RecoveredLifecycleObligation,
        RecoveryAttempt,
        RecoveryDispatch,
    )


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryGuestPreparation:
    """Bounded preparation and batch custody facts, never an availability lease."""

    guest: VMTargetPreparation | None
    identity: TargetIdentityPreparation | None
    pending_remote_effects: bool
    coordination_uncertain: bool
    requires_owner_retention: bool


class RecoveryGuestPreparationControlFact(Exception):
    """Safe preparation facts attached to the original escaping exception."""

    def __init__(self, preparation: RecoveryGuestPreparation) -> None:
        self.preparation = preparation
        super().__init__("recovery guest preparation stopped with retained custody")


class _RecoveryFixedObservationCarrier:
    """Internal fixed helper boundary sharing the batch's serial dispatcher."""

    def __init__(self, carrier: Carrier, batch: RecoveryGuestPreparationBatch) -> None:
        self._carrier = carrier
        self._batch = batch

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    @property
    def pending_remote_effects(self) -> bool:
        return self._batch._pending_remote_effects

    @property
    def coordination_uncertain(self) -> bool:
        return self._batch._coordination_uncertain

    @property
    def requires_owner_retention(self) -> bool:
        return self.pending_remote_effects or self.coordination_uncertain or self._batch._attempt is not None

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        batch = self._batch
        dispatch = batch._dispatch
        if dispatch is None or self.requires_owner_retention:
            raise StateError("Recovery fixed preparation has no available dispatcher")
        try:
            batch._attempt = dispatch.begin_attempt()
        except BaseException:
            # No carrier ran. Abort only unreturned local admission, preserving
            # the durable row and never settling an unknown attempt.
            batch._coordination_uncertain = True
            try:
                dispatch._abort_unreturned_attempt()  # noqa: SLF001
                batch._dispatch = None
            except BaseException:
                pass
            raise
        try:
            assert batch._attempt is not None
            return self._carrier.execute(invocation, io=io, deadline=deadline, custody=batch._attempt.local_delivery)
        except BaseException:
            batch._pending_remote_effects = True
            raise

    def settle(self, dispatch: Dispatch, completion: ExitStatus | None) -> bool:
        batch = self._batch
        attempt = batch._attempt
        if attempt is None:
            if dispatch is not Dispatch.NOT_SENT:
                batch._coordination_uncertain = True
            return False
        if dispatch is Dispatch.NOT_SENT or (dispatch is Dispatch.SENT and completion == ExitStatus(code=0)):
            if not attempt.local_delivery.settled:
                return False
            try:
                attempt.settle()
            except BaseException:
                batch._coordination_uncertain = True
                raise
            batch._attempt = None
            return dispatch is Dispatch.SENT
        batch._pending_remote_effects = True
        return False


class RecoveryGuestPreparationBatch:
    """Pin one native binding, owner and fresh ID before any support admission.

    Prepare exactly once. Uncertain custody stays on this caller-retained
    object; no takeover path can replay its guest or account probes.
    """

    def __init__(
        self,
        binding: NativeExecutionBinding,
        owner: OperationOwner,
        obligation_id: str,
    ) -> None:
        self._binding = binding
        self._owner = owner
        self._obligation_id = obligation_id
        self._started = False
        self._obligation: LifecycleObligation | None = None
        self._recovered: RecoveredLifecycleObligation | None = None
        self._dispatch: RecoveryDispatch | None = None
        self._attempt: RecoveryAttempt | None = None
        self._pending_remote_effects = False
        self._coordination_uncertain = False
        self._queries_accounted_for = False
        self._resolved = False
        self._guest: VMTargetPreparation | None = None
        self._identity: TargetIdentityPreparation | None = None

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def preparation(self) -> RecoveryGuestPreparation:
        return RecoveryGuestPreparation(
            self._guest,
            self._identity,
            self._pending_remote_effects,
            self._coordination_uncertain,
            self._pending_remote_effects
            or self._coordination_uncertain
            or self._attempt is not None
            or (self._started and not self._resolved),
        )

    def prepare(
        self,
        vm: VMRow,
        platform: VMPlatform,
        expected_locator: ProviderLocator,
        *,
        workload_account: str,
        include_elevated: bool,
        deadline: Deadline,
        observe_locator: Callable[[Deadline], ProviderLocatorObservation],
    ) -> RecoveryGuestPreparation:
        """Confirm the guest, then compose numeric plans on the pinned route."""
        if self._started:
            raise StateError("Recovery guest preparation batch is single-use")
        _validate_inputs(
            delivery_account=self._binding.delivery_account,
            workload_account=workload_account,
            include_elevated=include_elevated,
            runtime_selection=self._binding.runtime_selection,
            deadline=deadline,
            owner=self._owner,
        )
        _validate_operation_boundary(vm, deadline, self._owner)
        if platform.site_name != vm.site:
            raise ValidationError("Recovery guest preparation requires the VM's bound platform")
        if self._owner.inspect_lifecycle_obligation(self._obligation_id) is not None:
            raise StateError("Recovery guest preparation requires a fresh batch identifier")
        self._started = True
        try:
            self._admit()
            preflight = _preflight_marker_and_deadline(vm, deadline)
            if preflight is not None:
                self._guest = preflight
            else:
                early = self._binding._early_guest_facts_route
                guest_carrier = self._binding.carrier if early is None else early.carrier
                self._guest = _prepare_selected_platform_observations(
                    vm,
                    expected_locator,
                    self._binding,
                    _RecoveryFixedObservationCarrier(guest_carrier, self),
                    deadline=deadline,
                    observe_locator=observe_locator,
                )
                if self._guest.status is VMTargetPreparationStatus.PREPARED:
                    self._identity = _prepare_target_identity_observations(
                        _RecoveryFixedObservationCarrier(self._binding.carrier, self),
                        delivery_account=self._binding.delivery_account,
                        workload_account=workload_account,
                        include_elevated=include_elevated,
                        runtime_selection=self._binding.runtime_selection,
                        deadline=deadline,
                    )
            self._finish_settled_batch()
            return self.preparation
        except BaseException as control:
            cause = control.__cause__
            if isinstance(cause, VMTargetPreparationControlFact):
                self._guest = cause.preparation
            elif isinstance(cause, TargetIdentityControlFact):
                self._identity = cause.preparation
            # Never replace the original control exception with cleanup failure.
            if not self._coordination_uncertain and not self._pending_remote_effects and self._attempt is None:
                try:
                    self._finish_settled_batch()
                except BaseException:
                    self._coordination_uncertain = True
            raise control from RecoveryGuestPreparationControlFact(self.preparation)

    def _admit(self) -> None:
        try:
            self._obligation = self._owner.admit_recovery_support_obligation(
                "carrier-dispatch", payload_version=1, payload=b"", obligation_id=self._obligation_id
            )
            self._recovered = self._owner.rebind_possible_effect_lifecycle_obligation(
                self._obligation_id,
                "carrier-dispatch",
                payload_version=1,
                payload=b"",
                payload_revision=self._obligation.payload_revision,
            )
            self._dispatch = self._recovered.open_dispatch()
        except BaseException as control:
            if _is_pre_registration_refusal(control):
                self._queries_accounted_for = True
                self._resolved = True
                raise
            self._coordination_uncertain = True
            if self._recovered is not None:
                # Opening has no carrier action. Retain its exact binding for
                # cleanup of an activation whose handle did not reach us.
                self._queries_accounted_for = True
            raise

    def _finish_settled_batch(self) -> None:
        if self._pending_remote_effects or self._coordination_uncertain or self._attempt is not None:
            return
        dispatch = self._dispatch
        obligation = self._obligation
        if dispatch is None or obligation is None:
            return
        self._queries_accounted_for = True
        try:
            dispatch.close()
            obligation.resolve()
            self._resolved = True
            self._dispatch = None
        except BaseException:
            self._coordination_uncertain = True
            raise

    def retry_resolution(self) -> RecoveryGuestPreparation:
        """Reconcile only a stopped, settled batch's exact durable resolution."""
        if not self._queries_accounted_for or self._obligation is None or self._recovered is None:
            raise StateError("Recovery guest preparation lacks settled batch resolution evidence")
        if not self._resolved:
            try:
                if self._dispatch is None:
                    self._recovered._close_retained_dispatch()  # noqa: SLF001
                else:
                    self._dispatch.close()
                self._obligation.resolve()
            except BaseException as control:
                self._coordination_uncertain = True
                raise control from RecoveryGuestPreparationControlFact(self.preparation)
            self._resolved = True
        self._dispatch = None
        self._coordination_uncertain = False
        return self.preparation
