"""Serial owned staging of opaque prepared helper input, without activation."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation, OperationAttempt

from ._file_stage_exchange import (
    FileStageCandidateResult,
    FileStageObservationState,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from ._file_stage_protocol import MAX_STAGE_CHUNK_BYTES, FileStageFailureCode, FileStageOperation
from ._fixed_helper_operation import BorrowedFixedHelperCarrier, BoundHelperCarrier
from ._prepared_helper_obligation import (
    PREPARED_HELPER_OBLIGATION_KIND,
    PREPARED_HELPER_PAYLOAD_VERSION,
    PreparedHelperObligation,
    encode_prepared_helper_obligation,
)
from ._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, _NumericGuestBootstrap
from ._scratch import ScratchReference, _cleanup_debt
from ._staged_helper_protocol import StagedHelperRequest
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db.operations import LifecycleObligation as PersistedLifecycleObligation
    from agentworks.operations import OperationBorrow, OperationOwner

    from ._delivery_custody import LocalDeliveryCustody
    from ._helper_closure import HelperClosureDelivery
    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedTargetIdentity
    from ._runtime_prerequisite import HelperClosureExpectation
    from ._scratch_receipt import ScratchCleanupDebt
    from .carrier import Carrier


class _StageArguments(TypedDict):
    trusted_root_path: str
    relative_path: str
    token: bytes
    plan: IdentityPlan
    deadline: Deadline
    runtime_selection: RuntimeSelection
    bootstrap: _NumericGuestBootstrap
    fixed_scratch_root: bool
    prepare_delivery: Callable[[HelperClosureExpectation], BoundHelperCarrier]


@dataclass(slots=True, repr=False)
class _ActiveStageCall:
    borrow: OperationBorrow
    operation: BorrowedFixedHelperCarrier
    phase: FileStageOperation
    closure_delivery: HelperClosureDelivery | None = None
    candidate: FileStageCandidateResult | None = None


class PreparedHelperStage:
    """Parent-retained cleanup custody; caller bytes are never stored here.

    Construction is passive. The typed core parent retains this instance before
    begin, and owns activation delivery separately from these staging helpers.
    """

    def __init__(
        self,
        owner: OperationOwner,
        carrier: Carrier,
        target: ManagedTargetIdentity,
        bootstrap: _NumericGuestBootstrap,
        runtime: RuntimeSelection,
        deadline: Deadline,
        expected_length: int,
        *,
        before_dispatch: Callable[[Deadline], None] | None = None,
        new_helper_delivery: Callable[[HelperClosureExpectation], HelperClosureDelivery] | None = None,
    ) -> None:
        if deadline.expires_at is None or deadline.expired or not 0 <= expected_length <= 2**63 - 1:
            raise ValidationError("Prepared helper stage requires a finite live budget and bounded length")
        scope = owner.ownership.scope
        if scope.resource_kind.value != target.kind.value or scope.resource_name != target.name:
            raise ValidationError("Prepared helper stage differs from its operation owner")
        self.token = secrets.token_bytes(16)
        self.obligation_id = uuid4().hex
        self._payload = encode_prepared_helper_obligation(
            PreparedHelperObligation(target, bootstrap, runtime, self.token)
        )
        self._owner, self._carrier = owner, carrier
        self._bootstrap, self._runtime = bootstrap, runtime
        self._deadline, self.expected_length = deadline, expected_length
        self._before_dispatch = before_dispatch
        self._new_helper_delivery = new_helper_delivery
        self._active: _ActiveStageCall | None = None
        self._obligation: LifecycleObligation | None = None
        self.reference: ScratchReference | None = None
        self.cleanup_debt: ScratchCleanupDebt | None = None
        self.offset = 0
        self.registration_uncertain = False
        self.failed = False
        self.cleaned = False
        self.cleanup_resolution_uncertain = False
        self._begun = False
        self._activation_prepared = False
        self._activation_settled = False

    @property
    def helper_uncertain(self) -> bool:
        """Whether the exact staging call still retains local or helper custody."""
        return self._active is not None

    @property
    def deadline(self) -> Deadline:
        """The original preparation and activation budget, never renewed."""
        return self._deadline

    @staticmethod
    def _require_budget(deadline: Deadline) -> None:
        if deadline.expired:
            raise StateError("Prepared helper stage budget elapsed")

    def _require_closed(self, deadline: Deadline | None = None) -> None:
        if self.registration_uncertain or self.helper_uncertain or self.cleaned:
            raise StateError("Prepared helper stage retains uncertain or completed custody")
        self._require_budget(self.deadline if deadline is None else deadline)

    def _call(
        self,
        action: Callable[[BoundHelperCarrier], FileStageCandidateResult],
        *,
        deadline: Deadline | None = None,
        phase: FileStageOperation = FileStageOperation.BEGIN,
    ) -> FileStageCandidateResult:
        self._require_closed(deadline)
        borrow = self._owner.borrow()
        operation = BorrowedFixedHelperCarrier(self._carrier, borrow)
        active = _ActiveStageCall(borrow, operation, phase)
        self._active = active
        try:
            if self._before_dispatch is not None:
                self._before_dispatch(self.deadline if deadline is None else deadline)
            result = action(operation)
            active.candidate = result
            observation = result.observation
            if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
                if observation.reference is not None:
                    self.reference = observation.reference
                    self.cleanup_debt = _cleanup_debt(observation.reference)
                if observation.cleanup_debt is not None:
                    self.cleanup_debt = observation.cleanup_debt
                if observation.failure is not None and observation.failure.cleanup_debt is not None:
                    self.cleanup_debt = observation.failure.cleanup_debt
            self._settle_active(active)
            return result
        except BaseException:
            self.failed = True
            raise

    def _prepare_delivery(self, expectation: HelperClosureExpectation) -> BoundHelperCarrier:
        active = self._active
        assert active is not None
        if self._new_helper_delivery is not None:
            delivery = self._new_helper_delivery(expectation)
            active.closure_delivery = delivery
            active.operation = BorrowedFixedHelperCarrier(delivery, active.borrow)
        return active.operation

    def _validate_active(self, active: _ActiveStageCall) -> None:
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            owner._reconcile_transition_locked()  # noqa: SLF001
            borrower = owner._active_borrow  # noqa: SLF001
            if self._active is not active or (
                borrower is not active.borrow and (borrower is not None or not active.borrow._closed)  # noqa: SLF001
            ):
                raise StateError("Prepared stage no longer owns its borrow")
            attempt = owner._outstanding_attempt  # noqa: SLF001
            if attempt is not None and (
                not isinstance(attempt, OperationAttempt)
                or attempt._borrow is not active.borrow  # noqa: SLF001
                or (
                    active.operation.outstanding_attempt is not None
                    and attempt is not active.operation.outstanding_attempt
                )
            ):
                raise StateError("Prepared stage no longer owns its attempt")
        row = self._inspect_registration()
        if row is None or row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("Prepared stage cleanup obligation is no longer admitted")
        delivery = active.closure_delivery
        if not active.operation.wraps(self._carrier if delivery is None else delivery):
            raise StateError("Prepared stage helper binding changed")

    def _settle_active(self, active: _ActiveStageCall) -> None:
        """Retry only this call's local accounting with positive closure proof."""
        self._validate_active(active)
        operation = active.operation
        candidate = active.candidate
        delivery = active.closure_delivery
        known = candidate is not None and (
            candidate.dispatch is Dispatch.NOT_SENT
            or (candidate.dispatch is Dispatch.SENT and candidate.carrier_completion == ExitStatus(0))
        )
        if delivery is not None:
            known = (candidate is not None and candidate.dispatch is Dispatch.NOT_SENT) or delivery.closure_proven
        attempt = operation.outstanding_attempt
        if attempt is not None:
            if not known or not attempt.local_delivery.settled:
                return
            operation.settle_helper_closure()
        elif operation.pending_remote_effects:
            raise StateError("Prepared stage retains unknown helper closure")
        else:
            # A lost begin-attempt reply precedes entry to the actual carrier.
            with self._owner._guard:  # noqa: SLF001
                admission = self._owner._outstanding_attempt  # noqa: SLF001
            if admission is not None:
                assert isinstance(admission, OperationAttempt)
                admission.settle()
            self._recover_dispatch_registration(active)
        if not active.borrow.closed:
            active.borrow.close()
        self._active = None

    def _recover_dispatch_registration(self, active: _ActiveStageCall) -> None:
        """Recover only the generic row whose registration preceded carrier entry."""
        borrow = active.borrow
        identity = borrow._dispatch_obligation_id  # noqa: SLF001
        if identity is None or borrow._dispatch_obligation is not None:  # noqa: SLF001
            return
        row = self._owner.inspect_lifecycle_obligation(identity)
        if row is not None:
            if (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) != (
                "carrier-dispatch",
                1,
                b"",
                0,
            ):
                raise StateError("Prepared stage dispatch registration conflicts")
            borrow._dispatch_obligation = row  # noqa: SLF001

    def observe_cleanup(self, deadline: Deadline) -> None:
        """Drain the exact retained helper without replaying any stage request."""
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Prepared stage observation requires a finite live deadline")
        active = self._active
        if active is None:
            self.retry_bookkeeping()
            return
        self.failed = True
        self._validate_active(active)
        attempt = active.operation.outstanding_attempt
        no_entry = attempt is None and active.candidate is None and not active.operation.pending_remote_effects
        if attempt is not None:
            if not attempt.local_delivery.close(deadline):
                raise StateError("Prepared stage local delivery remains unsettled")
            self._validate_active(active)
            delivery = active.closure_delivery
            not_sent = active.candidate is not None and active.candidate.dispatch is Dispatch.NOT_SENT
            if delivery is not None and not not_sent and not delivery.closure_proven:
                if self._before_dispatch is not None:
                    self._before_dispatch(deadline)
                self._validate_active(active)
                if not delivery.observe_closure(deadline=deadline, custody=attempt.local_delivery):
                    raise StateError("Prepared stage helper closure remains unknown")
                self._validate_active(active)
        self._settle_active(active)
        if self._active is not None:
            raise StateError("Prepared stage helper closure remains unknown")
        candidate = active.candidate
        no_stage = active.phase is FileStageOperation.BEGIN and (
            no_entry or (candidate is not None and self._no_creation(candidate))
        )
        removed = (
            candidate is not None
            and active.phase is FileStageOperation.CLEANUP
            and self._accepted(candidate, FileStageObservationState.CLEANED)
        )
        if no_stage or removed:
            self._resolve_cleanup()

    def _inspect_registration(self) -> PersistedLifecycleObligation | None:
        row = self._owner.inspect_lifecycle_obligation(self.obligation_id)
        if row is not None and (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) != (
            PREPARED_HELPER_OBLIGATION_KIND,
            PREPARED_HELPER_PAYLOAD_VERSION,
            self._payload,
            0,
        ):
            raise StateError("Prepared stage registration conflicts")
        if row is None and not self.registration_uncertain:
            raise StateError("Prepared stage registration disappeared")
        return row

    def retry_bookkeeping(self) -> None:
        """Resolve interrupted exact stage registration or known-clean resolution."""
        if self._active is not None:
            raise StateError("Prepared stage retains helper custody")
        if not self.registration_uncertain and not self.cleanup_resolution_uncertain:
            return
        with self._owner._guard:  # noqa: SLF001
            self._owner._reconcile_transition_locked()  # noqa: SLF001
        row = self._inspect_registration()
        self.failed = True
        if row is not None:
            self._obligation = LifecycleObligation(self._owner, row)
            self._resolve_cleanup()
        else:
            self.cleaned = True
        self.registration_uncertain = False

    @staticmethod
    def _accepted(result: FileStageCandidateResult, state: FileStageObservationState) -> bool:
        return (
            result.dispatch is Dispatch.SENT
            and result.carrier_completion == ExitStatus(0)
            and result.carrier_failure is None
            and result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
            and result.observation is not None
            and result.observation.state is state
        )

    @classmethod
    def _no_creation(cls, result: FileStageCandidateResult) -> bool:
        observation = result.observation
        return result.dispatch is Dispatch.NOT_SENT or (
            cls._accepted(result, FileStageObservationState.REFUSED)
            and observation is not None
            and observation.failure is not None
            and observation.failure.code is not FileStageFailureCode.SCRATCH
        )

    def begin(self) -> bool:
        """Admit cleanup identity before one non-replayed BEGIN attempt."""
        self._require_closed()
        if self._begun:
            raise StateError("Prepared helper stage BEGIN is one-shot")
        self._begun = True
        self.failed = True
        self.registration_uncertain = True
        self._obligation = self._owner.register_lifecycle_obligation(
            PREPARED_HELPER_OBLIGATION_KIND,
            payload_version=PREPARED_HELPER_PAYLOAD_VERSION,
            payload=self._payload,
            obligation_id=self.obligation_id,
        )
        self._obligation.mark_possible_effect()
        self.registration_uncertain = False
        result = self._call(
            lambda carrier: stage_begin(carrier, expected_length=self.expected_length, **self._arguments())
        )
        self.failed = not (
            self._accepted(result, FileStageObservationState.CREATED)
            and self.reference is not None
            and not self.helper_uncertain
        )
        if not self.helper_uncertain and self._no_creation(result):
            self._resolve_cleanup()
        return not self.failed

    def _arguments(self, deadline: Deadline | None = None) -> _StageArguments:
        return {
            "trusted_root_path": "/tmp",
            "relative_path": "prepared-input",
            "token": self.token,
            "plan": self._bootstrap.root_entry,
            "deadline": self.deadline if deadline is None else deadline,
            "runtime_selection": self._runtime,
            "bootstrap": self._bootstrap,
            "fixed_scratch_root": True,
            "prepare_delivery": self._prepare_delivery,
        }

    def append(self, data: bytes) -> bool:
        """Write bounded chunks once; advance only positively acknowledged ranges."""
        self._require_closed()
        if self.failed or self.reference is None or self._activation_prepared:
            raise StateError("Prepared helper stage cannot append")
        if len(data) > self.expected_length - self.offset:
            raise ValidationError("Prepared helper input exceeds its fixed length")
        reference = self.reference
        # An escaping control between acknowledgement and offset publication
        # must never leave permission to replay an uncertain range.
        self.failed = True
        for start in range(0, len(data), MAX_STAGE_CHUNK_BYTES):
            chunk = data[start : start + MAX_STAGE_CHUNK_BYTES]

            def write(carrier: BoundHelperCarrier, chunk: bytes = chunk) -> FileStageCandidateResult:
                return stage_chunk(
                    carrier,
                    reference=reference,
                    offset=self.offset,
                    data=chunk,
                    chunk_digest=hashlib.sha256(chunk).digest(),
                    **self._arguments(),
                )

            result = self._call(write, phase=FileStageOperation.CHUNK)
            if self.helper_uncertain or not self._accepted(result, FileStageObservationState.ACCEPTED):
                self.failed = True
                return False
            self.offset += len(chunk)
        self.failed = False
        return True

    def prepare_activation(self, nonce: str, digest: bytes) -> StagedHelperRequest:
        """Hand off one complete active reference, never historical ownership."""
        self._require_closed()
        if self.failed or self.reference is None or self.offset != self.expected_length or self._activation_prepared:
            raise StateError("Prepared helper stage is not available for activation")
        remaining = self.deadline.remaining()
        assert remaining is not None
        request = StagedHelperRequest(
            nonce,
            self._bootstrap.root_entry.expected,
            self.token,
            self.reference,
            digest,
            int(remaining * 1_000_000_000),
        )
        self._activation_prepared = True
        return request

    def note_activation_completion(
        self, dispatch: Dispatch, completion: ExitStatus | None, local_delivery: LocalDeliveryCustody
    ) -> None:
        """Record parent's actual activation closure, not application success."""
        if not self._activation_prepared or self._activation_settled:
            raise StateError("Prepared helper activation handoff is unavailable")
        self._activation_settled = local_delivery.settled and (
            dispatch is Dispatch.NOT_SENT or (dispatch is Dispatch.SENT and completion == ExitStatus(0))
        )

    def note_activation_closure(self, local_delivery: LocalDeliveryCustody) -> None:
        """Accept the parent's exact helper-closure proof and actual local custody."""
        if not self._activation_prepared or self._activation_settled or not local_delivery.settled:
            raise StateError("Prepared helper activation closure is unavailable")
        self._activation_settled = True

    def note_activation_not_dispatched(self) -> None:
        """Accept parent proof that its retained start-custody callback was never reached."""
        if not self._activation_prepared or self._activation_settled:
            raise StateError("Prepared helper activation handoff is unavailable")
        self._activation_settled = True

    def cleanup(self, *, deadline: Deadline | None = None) -> bool:
        """Remove exact owned objects, optionally under a fresh cleanup budget.

        This explicit continuation never reopens preparation or activation and
        never substitutes elapsed time for actual helper or activation closure.
        """
        cleanup_deadline = self.deadline if deadline is None else deadline
        if cleanup_deadline.expires_at is None:
            raise ValidationError("Prepared helper cleanup requires a finite budget")
        self._require_closed(cleanup_deadline)
        if not self._begun or self._obligation is None or (self._activation_prepared and not self._activation_settled):
            raise StateError("Prepared helper cleanup lacks closed activation custody")
        # Once removal is attempted this active reference can never load or
        # accept more input, including after a partial cleanup failure.
        self.failed = True
        if self.cleanup_debt is None:
            result = self._call(
                lambda carrier: stage_reconcile(carrier, **self._arguments(cleanup_deadline)),
                deadline=cleanup_deadline,
                phase=FileStageOperation.RECONCILE,
            )
            if (
                self.helper_uncertain
                or not self._accepted(result, FileStageObservationState.RECOVERED)
                or self.cleanup_debt is None
            ):
                return False
        debt = self.cleanup_debt
        result = self._call(
            lambda carrier: stage_cleanup(carrier, cleanup_debt=debt, **self._arguments(cleanup_deadline)),
            deadline=cleanup_deadline,
            phase=FileStageOperation.CLEANUP,
        )
        if self.helper_uncertain or not self._accepted(result, FileStageObservationState.CLEANED):
            return False
        self._resolve_cleanup()
        return True

    def _resolve_cleanup(self) -> None:
        assert self._obligation is not None
        self.cleaned = True
        self.cleanup_debt = None
        self.cleanup_resolution_uncertain = True
        self._obligation.resolve()
        self.cleanup_resolution_uncertain = False
