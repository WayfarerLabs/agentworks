"""Concrete carrier admission for file calls borrowing one core operation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution.carrier import CarrierIO, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.execution.carrier import (
        Carrier,
        CarrierReport,
        ChannelFeatures,
        Deadline,
        PreparedInvocation,
    )
    from agentworks.operations import OperationAttempt, OperationBorrow


class BorrowedFileCarrier:
    """Arm and settle one borrowed owner around concrete carrier executions.

    File workflows must record their protocol facts before calling ``settle``.
    This class deliberately knows nothing about file observations or cleanup
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

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        try:
            attempt = self._borrow.begin_attempt()
            self.outstanding_attempt = attempt
        except BaseException:
            self.coordination_uncertain = self._borrow.has_outstanding_attempt
            raise
        try:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
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
        if dispatch is Dispatch.NOT_SENT:
            attempt.settle()
            self.outstanding_attempt = None
            return False
        if dispatch is Dispatch.SENT and completion == ExitStatus(code=0):
            attempt.settle()
            self.outstanding_attempt = None
            return True
        self.pending_remote_effects = True
        return False
