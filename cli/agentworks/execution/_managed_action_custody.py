"""Borrowed dispatch and obligation custody shared by managed later actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.operations import release_borrow_after_custody

from ._fixed_helper_operation import BorrowedHelperCustody
from .carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.operations import OperationBorrow

    from ._fixed_helper_operation import BorrowedFixedHelperCarrier


class ManagedActionCustody:
    """Own registration, borrowed attempt settlement, and one release attempt."""

    def __init__(self, borrow: OperationBorrow, operation: BorrowedFixedHelperCarrier) -> None:
        self._borrow = borrow
        self._operation = operation
        self._registration_started = False
        self._release_attempted = False

    def register(self, obligation_id: str, kind: str, payload: bytes, *, payload_version: int) -> None:
        self._registration_started = True
        self._borrow.install_dispatch_obligation(obligation_id, kind, payload_version=payload_version, payload=payload)

    def settle_and_release(
        self, dispatch: Dispatch, completion: ExitStatus | None, *, accepted_response: bool
    ) -> BorrowedHelperCustody:
        self._operation.settle(dispatch, completion)
        proved = dispatch is Dispatch.NOT_SENT or (
            dispatch is Dispatch.SENT and completion == ExitStatus(code=0) and accepted_response
        )
        retained = not proved or self._operation.requires_owner_retention
        fact = BorrowedHelperCustody(
            self._operation.pending_remote_effects or (self._borrow.dispatch_obligation_may_be_armed and retained),
            self._operation.coordination_uncertain,
            retained,
        )
        self._release_attempted = True
        release_borrow_after_custody(self._borrow, retain_effect=retained)
        return fact

    def close_pre_registration_refusal(self) -> None:
        self._borrow.close()

    def escaped(self) -> BorrowedHelperCustody:
        """Release once if possible and retain facts about an interrupted transition."""
        armed = self._borrow.dispatch_obligation_may_be_armed
        uncertain_registration = self._registration_started and not self._borrow.has_installed_dispatch_obligation
        release_failed = self._release_attempted
        if not self._release_attempted:
            self._release_attempted = True
            try:
                release_borrow_after_custody(self._borrow, retain_effect=armed)
            except BaseException:
                release_failed = True
        return BorrowedHelperCustody(
            self._operation.pending_remote_effects or armed,
            self._operation.coordination_uncertain
            or self._operation.has_outstanding_attempt
            or uncertain_registration
            or release_failed,
            armed or self._operation.requires_owner_retention or uncertain_registration or release_failed,
        )
