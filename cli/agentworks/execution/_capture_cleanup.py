"""Same-owner capture custody and exact two-token cleanup continuation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation

from ._capture_control import CaptureCreation
from ._capture_obligation import CAPTURE_OBLIGATION_KIND, CaptureObligation, encode_capture_obligation
from ._closing_helper_delivery import ClosingHelperDelivery
from ._file_snapshot_exchange import (
    FileSnapshotCandidateResult,
    FileSnapshotObservationState,
    snapshot_cleanup,
    snapshot_reconcile,
)
from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._inline import InlineCandidateResult
from ._runtime_prerequisite import RuntimePrerequisiteState
from ._scratch import _cleanup_debt
from .carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._delivery_custody import LocalDeliveryCustody
    from ._execution_operation import ExecutionOperation, _ActiveHelperCall
    from ._fixed_helper_operation import BoundHelperCarrier
    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import HelperClosureExpectation, RuntimeSelection, _NumericGuestBootstrap
    from ._scratch_receipt import ScratchCleanupDebt
    from .carrier import Carrier, Deadline

    SnapshotAction = Callable[
        [BoundHelperCarrier, Callable[[HelperClosureExpectation], BoundHelperCarrier]], FileSnapshotCandidateResult
    ]


class SnapshotArguments(TypedDict):
    plan: IdentityPlan
    deadline: Deadline
    runtime_selection: RuntimeSelection
    bootstrap: _NumericGuestBootstrap


@dataclass(slots=True, repr=False)
class _TokenCleanup:
    token: bytes
    debt: ScratchCleanupDebt | None = None
    done: bool = False


class CaptureCleanup:
    """One durable binding, actual helpers, and no retained application bytes."""

    def __init__(self, operation: ExecutionOperation, carrier: Carrier, binding: CaptureObligation) -> None:
        self.operation, self.carrier, self.binding = operation, carrier, binding
        self.payload = encode_capture_obligation(binding)
        self.owner = operation._owner
        self.ownership = self.owner.ownership
        self.obligation_id = uuid4().hex
        self.obligation: LifecycleObligation | None = None
        self.registration_uncertain = False
        self.resolution_uncertain = False
        self.registered = False
        self.cleaned = False
        self.application_closed = False
        self.activation_seen = False
        self._application: _ActiveHelperCall | None = None
        self._local: LocalDeliveryCustody | None = None
        self._active: _ActiveHelperCall | None = None
        self._closing: ClosingHelperDelivery | None = None
        self._closing_result: FileSnapshotCandidateResult | None = None
        self._closing_index = 0
        self._closing_cleanup = False
        self.tokens = (_TokenCleanup(binding.tokens[0]), _TokenCleanup(binding.tokens[1]))
        native = operation._native_binding
        self._factory = native._new_helper_delivery if native is not None and native.carrier is carrier else None

    @property
    def pending(self) -> bool:
        return self.registration_uncertain or self.resolution_uncertain or (self.registered and not self.cleaned)

    @property
    def helper_pending(self) -> bool:
        return (
            (self._application is not None and not self.application_closed)
            or (self._active is not None and self._active.operation.requires_owner_retention)
            or (self._closing is not None and self._closing.helper_pending)
        )

    @property
    def closing_helper_pending(self) -> bool:
        return self._closing is not None and self._closing.helper_pending

    def register(self) -> None:
        self.registration_uncertain = True
        self.obligation = self.owner.register_lifecycle_obligation(
            CAPTURE_OBLIGATION_KIND,
            payload_version=1,
            payload=self.payload,
            obligation_id=self.obligation_id,
        )
        self.registered = True
        self.obligation.mark_possible_effect()
        self.registration_uncertain = False

    def _check(self) -> None:
        if (
            self.owner is not self.operation._owner
            or self.owner.ownership != self.ownership
            or encode_capture_obligation(self.binding) != self.payload
        ):
            raise StateError("Capture cleanup binding changed")
        row = self.owner.inspect_lifecycle_obligation(self.obligation_id)
        if row is None and self.application_closed and not self.registered:
            self.registration_uncertain = False
            self.cleaned = True
            return
        if row is None or (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) != (
            CAPTURE_OBLIGATION_KIND,
            1,
            self.payload,
            0,
        ):
            raise StateError("Capture cleanup obligation changed")
        if self.registration_uncertain:
            self.obligation = LifecycleObligation(self.owner, row)
            self.registered = True
            self.registration_uncertain = False
        if row.state is LifecycleObligationState.RESOLVED and not self.helper_pending:
            self.cleaned = True
            self.resolution_uncertain = False

    def note_not_dispatched(self) -> None:
        if self._application is not None:
            raise StateError("Capture activation already retains an actual helper")
        self.application_closed = True
        for token in self.tokens:
            token.done = True

    def note_application(self, active: _ActiveHelperCall) -> None:
        self.activation_seen = True
        if self.application_closed:
            return
        if self._application is not None and self._application is not active:
            raise StateError("Capture activation helper changed")
        self._application = active
        attempt = active.operation.outstanding_attempt
        if attempt is not None:
            self._local = attempt.local_delivery
        candidate = active.candidate
        if isinstance(candidate, InlineCandidateResult) and candidate.observation is not None:
            observation = candidate.observation
            for fact in observation.capture_facts:
                index = 0 if fact.end.stream.value == "stdout" else 1
                if fact.ready is not None:
                    self.tokens[index].debt = _cleanup_debt(fact.ready)
                elif (
                    observation.trusted_terminal
                    and observation.error is None
                    and fact.creation is CaptureCreation.NOT_ENTERED
                ):
                    self.tokens[index].done = True
        if not active.operation.delivery_started:
            self.application_closed = True
            for token in self.tokens:
                token.done = True
        elif self._local is not None and self._local.settled:
            delivery = active.closure_delivery
            not_sent = active.not_sent or (candidate is not None and candidate.dispatch is Dispatch.NOT_SENT)
            self.application_closed = not_sent or (
                delivery.closure_proven
                if delivery is not None
                else candidate is not None and candidate.carrier_completion == ExitStatus(0)
            )
            if not_sent:
                for token in self.tokens:
                    token.done = True
        if self.application_closed:
            self._application = None
            self._local = None

    @staticmethod
    def accepted(candidate: FileSnapshotCandidateResult, state: FileSnapshotObservationState) -> bool:
        return (
            candidate.dispatch is Dispatch.SENT
            and candidate.carrier_completion == ExitStatus(0)
            and candidate.carrier_failure is None
            and candidate.runtime_prerequisite.state is RuntimePrerequisiteState.READY
            and candidate.observation is not None
            and candidate.observation.state is state
        )

    def _route(self, deadline: Deadline) -> None:
        if self.operation._route_check is not None:
            self.operation._route_check(deadline)

    def _prepare_normal(self, expectation: HelperClosureExpectation) -> BoundHelperCarrier:
        active = self._active
        assert active is not None
        active.closure_expectation = expectation
        if self._factory is not None:
            active.closure_delivery = self._factory(expectation)
            active.operation = BorrowedFixedHelperCarrier(active.closure_delivery, active.borrow)
        return active.operation

    def call(self, action: SnapshotAction, deadline: Deadline) -> FileSnapshotCandidateResult:
        """Use the ordinary helper tracker for exactly one read or open-owner cleanup."""
        self._check()
        if not self.application_closed or self.helper_pending or self.operation.scratch_helper_pending:
            raise StateError("Capture still retains helper custody")
        with self.operation._admission_guard:
            self.operation._require_no_inline_preparation()
            if self.operation._active_inline_calls or self.operation._unfinished_inline_executions:
                raise StateError("Capture helper cannot overlap ordinary delivery")
            active = self.operation._borrow_helper_call(self.carrier, None, inline=True)
            self._active = active
        try:
            self.operation._admit(active)
            self._route(deadline)
            result = action(active.operation, self._prepare_normal)
            active.candidate = replace(result, observation=None)
            helper = active.operation
            if active.closure_delivery is not None and active.closure_delivery.closure_proven:
                helper.settle_helper_closure()
            else:
                helper.settle(
                    result.dispatch, None if active.closure_delivery is not None else result.carrier_completion
                )
            with self.operation._admission_guard:
                self.operation._capture(
                    active, self.operation._outcome(active, helper, deadline, include_candidate=False)
                )
            if not helper.requires_owner_retention:
                self._active = None
            return result
        except BaseException as control:
            self.operation._raise_control(control, active, active.operation, deadline)
            raise AssertionError("Capture control returned") from None

    def arguments(self, deadline: Deadline) -> SnapshotArguments:
        return dict(
            plan=self.binding.workload,
            deadline=deadline,
            runtime_selection=self.binding.preparation.runtime,
            bootstrap=self.binding.preparation.bootstrap,
        )

    def _require_closing(self, helper: ClosingHelperDelivery, deadline: Deadline) -> None:
        self._check()
        if (
            self.cleaned
            or helper is not self._closing
            or helper.base_carrier is not self.carrier
            or not self.application_closed
            or deadline.expired
        ):
            raise StateError("Capture closing delivery is unavailable")
        with self.owner._guard:
            self.owner._reconcile_transition_locked()
            self.owner._require_no_active_work_locked()
            if self.owner._released or not self.owner._close_requested.is_set():
                raise StateError("Capture closing authority is unavailable")

    def _prepare_closing(self, expectation: HelperClosureExpectation) -> ClosingHelperDelivery:
        assert self._closing is not None
        return self._closing.bind(expectation, self._factory)

    def _apply(self, result: FileSnapshotCandidateResult, index: int, cleanup: bool) -> None:
        observation = result.observation
        if observation is not None and observation.cleanup_debt is not None:
            self.tokens[index].debt = observation.cleanup_debt
        if observation is not None and observation.failure is not None and observation.failure.cleanup_debt is not None:
            self.tokens[index].debt = observation.failure.cleanup_debt
        if cleanup and self.accepted(result, FileSnapshotObservationState.CLEANED):
            self.tokens[index].done = True

    def _exchange(self, index: int, cleanup: bool, deadline: Deadline) -> bool:
        token = self.tokens[index]

        def action(
            carrier: BoundHelperCarrier, prepare: Callable[[HelperClosureExpectation], BoundHelperCarrier]
        ) -> FileSnapshotCandidateResult:
            if cleanup:
                assert token.debt is not None
                return snapshot_cleanup(
                    carrier,
                    token=token.token,
                    cleanup_debt=token.debt,
                    prepare_delivery=prepare,
                    **self.arguments(deadline),
                )
            return snapshot_reconcile(carrier, token=token.token, prepare_delivery=prepare, **self.arguments(deadline))

        if not self.owner._close_requested.is_set():
            result = self.call(action, deadline)
            if self.helper_pending:
                return False
            self._apply(result, index, cleanup)
            return True
        self._closing_index, self._closing_cleanup = index, cleanup
        self._closing = ClosingHelperDelivery(self.carrier, self._require_closing, self._route)
        result = action(self._closing, self._prepare_closing)
        self._closing_result = result
        return self._settle_closing(deadline, observe=False)

    def _settle_closing(self, deadline: Deadline, *, observe: bool) -> bool:
        assert self._closing is not None
        if not self._closing.settle(deadline, observe=observe):
            return False
        if self._closing_result is not None:
            self._apply(self._closing_result, self._closing_index, self._closing_cleanup)
        self._closing, self._closing_result = None, None
        return True

    def settle_closing_helper(self, deadline: Deadline) -> bool:
        """Observe only retained closing custody, without starting token cleanup."""
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Capture cleanup requires a finite live budget")
        self._check()
        return self._closing is None or self._settle_closing(deadline, observe=True)

    def finish(self, deadline: Deadline) -> bool:
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Capture cleanup requires a finite live budget")
        self._check()
        if self.cleaned:
            return True
        if self._application is not None:
            self.note_application(self._application)
        if not self.application_closed:
            return False
        if self._active is not None:
            if self._active.tracking_key in self.operation._active_inline_calls:
                self.operation.observe_inline_cleanup(deadline)
                self.operation.retry_inline_bookkeeping()
            if self._active.operation.requires_owner_retention:
                return False
            self._active = None
        if self._closing is not None and not self._settle_closing(deadline, observe=True):
            return False
        for index, token in enumerate(self.tokens):
            if token.done:
                continue
            if token.debt is None and not self._exchange(index, False, deadline):
                return False
            if token.debt is not None and not self._exchange(index, True, deadline):
                return False
        if not all(token.done for token in self.tokens):
            return False
        assert self.obligation is not None
        self.resolution_uncertain = True
        self.obligation.resolve()
        self.cleaned = True
        self.resolution_uncertain = False
        return True
