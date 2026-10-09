"""Fixed-observation custody and ordinary borrowed carrier admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import StateError
from agentworks.execution.carrier import CarrierIO, Dispatch, ExitStatus
from agentworks.operations import release_borrow_after_custody

if TYPE_CHECKING:
    from agentworks.execution.carrier import (
        Carrier,
        CarrierReport,
        ChannelFeatures,
        Deadline,
        PreparedInvocation,
    )
    from agentworks.operations import OperationAttempt, OperationBorrow, RecoveryAttempt


class BoundHelperCarrier(Protocol):
    """Prepared helper delivery under custody already held by its wrapper."""

    @property
    def features(self) -> ChannelFeatures: ...

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None: ...

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport: ...


class FixedObservationCarrier(BoundHelperCarrier, Protocol):
    """Concrete fixed-observation dispatch and its local custody facts."""

    @property
    def pending_remote_effects(self) -> bool: ...

    @property
    def coordination_uncertain(self) -> bool: ...

    @property
    def requires_owner_retention(self) -> bool: ...

    def settle(self, dispatch: Dispatch, completion: ExitStatus | None) -> bool: ...


class AttemptBoundHelperCarrier:
    """Adapt an already-admitted attempt to fixed helper delivery.

    This does not begin, settle or reopen an attempt. Its enclosing workflow
    retains the supplied attempt before constructing this helper view.
    """

    def __init__(self, carrier: Carrier, attempt: OperationAttempt | RecoveryAttempt) -> None:
        self._carrier = carrier
        self._attempt = attempt

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        if not self._attempt.local_delivery.settled:
            raise StateError("Helper attempt retains unsettled local delivery")
        return self._carrier.execute(invocation, io=io, deadline=deadline, custody=self._attempt.local_delivery)


@dataclass(frozen=True, slots=True)
class BorrowedHelperCustody:
    """Safe owner-retention facts after a fixed helper releases its borrow."""

    pending_remote_effects: bool
    coordination_uncertain: bool
    requires_owner_retention: bool


class BorrowedFixedHelperCarrier:
    """Arm and settle one borrowed owner around concrete carrier executions.

    Helper workflows must record their protocol facts before calling ``settle``.
    This class deliberately knows nothing about protocol observations or cleanup
    debt; it owns only actual-dispatch admission and termination evidence.
    """

    def __init__(self, carrier: Carrier, borrow: OperationBorrow) -> None:
        self._carrier = carrier
        self._borrow = borrow
        self.outstanding_attempt: OperationAttempt | None = None
        self.pending_remote_effects = False
        self.coordination_uncertain = False

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    @property
    def has_outstanding_attempt(self) -> bool:
        return self.outstanding_attempt is not None or self._borrow.has_outstanding_attempt

    @property
    def requires_owner_retention(self) -> bool:
        return self.pending_remote_effects or self.coordination_uncertain or self.has_outstanding_attempt

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Delegate pure structural validation without borrowing an attempt."""
        self._carrier.validate(invocation, io=io)

    def wraps(self, carrier: Carrier) -> bool:
        """Identify the exact carrier behind this borrowed dispatch boundary."""
        return self._carrier is carrier

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        try:
            attempt = self._borrow.begin_attempt()
            self.outstanding_attempt = attempt
        except BaseException:
            self.coordination_uncertain = self._borrow.has_outstanding_attempt
            raise
        try:
            return self._carrier.execute(invocation, io=io, deadline=deadline, custody=attempt.local_delivery)
        except BaseException:
            self.pending_remote_effects = True
            raise

    def settle(self, dispatch: Dispatch, completion: ExitStatus | None) -> bool:
        """Settle the current attempt only with the supported termination proof."""
        attempt = self.outstanding_attempt
        if attempt is None:
            if dispatch is not Dispatch.NOT_SENT:
                self.coordination_uncertain = True
            return False
        if dispatch is Dispatch.NOT_SENT or (dispatch is Dispatch.SENT and completion == ExitStatus(code=0)):
            if not attempt.local_delivery.settled:
                return False
            try:
                attempt.settle()
            except BaseException:
                self.coordination_uncertain = True
                raise
            self.outstanding_attempt = None
            return dispatch is Dispatch.SENT
        self.pending_remote_effects = True
        return False

    def settle_helper_closure(self) -> None:
        """Settle caller-proven closure, reconciling the same lost settlement reply."""
        attempt = self.outstanding_attempt
        if attempt is None:
            raise StateError("Helper closure has no original attempt")
        if not attempt.local_delivery.settled:
            raise StateError("Helper closure retains unsettled local delivery")
        try:
            if self._borrow.has_outstanding_attempt:
                attempt.settle()
        except BaseException:
            self.coordination_uncertain = True
            raise
        self.outstanding_attempt = None
        self.pending_remote_effects = False
        self.coordination_uncertain = False

    def release(self, *, control_escaped: bool = False) -> tuple[BorrowedHelperCustody, BaseException | None]:
        """Release or retain borrowed custody and report an interrupted release.

        An escaped control path reports an outstanding attempt as uncertain
        coordination even after the borrow hands its custody back to core.
        """
        retained = self.requires_owner_retention
        try:
            release_borrow_after_custody(self._borrow, retain_effect=retained)
        except BaseException as error:
            return BorrowedHelperCustody(self.pending_remote_effects, True, True), error
        return (
            BorrowedHelperCustody(
                self.pending_remote_effects,
                self.coordination_uncertain or (control_escaped and self.has_outstanding_attempt),
                retained,
            ),
            None,
        )
