"""Core-owned custody for one private foreground inline execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._inline import (
    InlineCandidateResult,
    PreparedInlineCandidate,
    execute_inline_candidate,
    prepare_inline_candidate,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.execution.models import Command, Script
    from agentworks.operations import OperationBorrow, OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class OwnedInlineOutcome:
    """Captured inline facts and independent operation-ownership state.

    ``candidate`` is absent only on escaping control flow. Retaining a partial
    helper transcript in an exception's custody chain would unnecessarily keep
    request-derived bytes alive. The original exception remains the escaping
    object, with a safe fact carrying the ownership state instead.
    """

    candidate: InlineCandidateResult | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class InlineExecutionControlFact(Exception):
    """Safe custody state attached to an escaping inline control flow."""

    def __init__(self, outcome: OwnedInlineOutcome) -> None:
        self.outcome = outcome
        super().__init__("private inline execution stopped with retained operation state")


@dataclass(slots=True, repr=False)
class _ActiveInlineCall:
    carrier: Carrier
    borrow: OperationBorrow
    prepared: PreparedInlineCandidate
    candidate: InlineCandidateResult | None = None
    outcome: OwnedInlineOutcome | None = None


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedInlineExecution:
    """Captured non-payload custody for a call whose owner remains retained."""

    outcome: OwnedInlineOutcome


class ExecutionOperation:
    """Run one foreground inline candidate under an existing operation owner."""

    def __init__(self, owner: OperationOwner) -> None:
        self._owner = owner
        self._active_inline_calls: dict[int, _ActiveInlineCall] = {}
        self._unfinished_inline_executions: list[UnfinishedInlineExecution] = []

    @property
    def active_inline_calls(self) -> tuple[_ActiveInlineCall, ...]:
        """Return calls whose borrow has not yet been relinquished."""
        return tuple(self._active_inline_calls.values())

    @property
    def unfinished_inline_executions(self) -> tuple[UnfinishedInlineExecution, ...]:
        """Return custody records for attempts that cannot release ownership."""
        return tuple(self._unfinished_inline_executions)

    def run_inline(
        self,
        carrier: Carrier,
        request: Command | Script,
        *,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        stdin: bytes = b"",
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        capture_limit: int | None = 4_096,
        sensitive: bool = False,
    ) -> OwnedInlineOutcome:
        """Prepare, dispatch and settle one inline candidate without replay."""
        borrow = self._owner.borrow()
        try:
            prepared = prepare_inline_candidate(
                request,
                plan=plan,
                stdin=stdin,
                env=env,
                cwd=cwd,
                capture_limit=capture_limit,
                sensitive=sensitive,
                runtime_selection=runtime_selection,
            )
            active = _ActiveInlineCall(carrier, borrow, prepared)
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_inline_calls[id(active)] = active
        except BaseException:
            borrow.close()
            raise

        operation = BorrowedFixedHelperCarrier(carrier, borrow)
        try:
            candidate = execute_inline_candidate(operation, prepared, deadline=deadline)
            active.candidate = candidate
            operation.settle(candidate.dispatch, candidate.carrier_completion)
            outcome = self._outcome(active, operation, deadline, include_candidate=True)
        except BaseException as control:
            self._raise_control(control, active, operation, deadline)

        self._capture(active, outcome)
        return outcome

    def _outcome(
        self,
        active: _ActiveInlineCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
        *,
        include_candidate: bool,
    ) -> OwnedInlineOutcome:
        return OwnedInlineOutcome(
            candidate=active.candidate if include_candidate else None,
            deadline_exceeded=deadline.expired,
            pending_remote_effects=operation.pending_remote_effects,
            coordination_uncertain=operation.coordination_uncertain,
            requires_owner_retention=operation.requires_owner_retention,
        )

    def _raise_control(
        self,
        control: BaseException,
        active: _ActiveInlineCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
    ) -> None:
        try:
            outcome = self._outcome(active, operation, deadline, include_candidate=False)
            self._capture(active, outcome)
            fact = InlineExecutionControlFact(outcome)
        except BaseException:
            raise control from None
        raise control from fact

    def _capture(self, active: _ActiveInlineCall, outcome: OwnedInlineOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_inline_executions.append(UnfinishedInlineExecution(outcome))
        active.borrow.close()
        self._active_inline_calls.pop(id(active))
