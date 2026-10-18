"""Serial owned staging of opaque prepared helper input, without activation."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

from agentworks.errors import StateError, ValidationError
from agentworks.operations import release_borrow_after_custody

from ._file_stage_exchange import (
    FileStageCandidateResult,
    FileStageObservationState,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from ._file_stage_protocol import MAX_STAGE_CHUNK_BYTES, FileStageFailureCode
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

    from agentworks.operations import LifecycleObligation, OperationOwner

    from ._delivery_custody import LocalDeliveryCustody
    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedTargetIdentity
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
        before_dispatch: Callable[[], None] | None = None,
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
        self._obligation: LifecycleObligation | None = None
        self.reference: ScratchReference | None = None
        self.cleanup_debt: ScratchCleanupDebt | None = None
        self.offset = 0
        self.registration_uncertain = False
        self.helper_uncertain = False
        self.failed = False
        self.cleaned = False
        self.cleanup_resolution_uncertain = False
        self._begun = False
        self._activation_prepared = False
        self._activation_settled = False

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
        self, action: Callable[[BoundHelperCarrier], FileStageCandidateResult], *, deadline: Deadline | None = None
    ) -> FileStageCandidateResult:
        self._require_closed(deadline)
        borrow = self._owner.borrow()
        operation = BorrowedFixedHelperCarrier(self._carrier, borrow)
        try:
            if self._before_dispatch is not None:
                self._before_dispatch()
            result = action(operation)
            observation = result.observation
            if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
                if observation.reference is not None:
                    self.reference = observation.reference
                    self.cleanup_debt = _cleanup_debt(observation.reference)
                if observation.cleanup_debt is not None:
                    self.cleanup_debt = observation.cleanup_debt
                if observation.failure is not None and observation.failure.cleanup_debt is not None:
                    self.cleanup_debt = observation.failure.cleanup_debt
            operation.settle(result.dispatch, result.carrier_completion)
            self.helper_uncertain = operation.requires_owner_retention
            release_borrow_after_custody(borrow, retain_effect=self.helper_uncertain)
            return result
        except BaseException:
            self.helper_uncertain = True
            self.failed = True
            with suppress(BaseException):
                release_borrow_after_custody(borrow, retain_effect=operation.requires_owner_retention)
            raise

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

    def begin(self) -> bool:
        """Admit cleanup identity before one non-replayed BEGIN attempt."""
        self._require_closed()
        if self._begun:
            raise StateError("Prepared helper stage BEGIN is one-shot")
        self._begun = True
        self.registration_uncertain = True
        self._obligation = self._owner.register_lifecycle_obligation(
            PREPARED_HELPER_OBLIGATION_KIND,
            payload_version=PREPARED_HELPER_PAYLOAD_VERSION,
            payload=self._payload,
            obligation_id=self.obligation_id,
        )
        self._obligation.mark_possible_effect()
        self.registration_uncertain = False
        self.failed = True
        result = self._call(
            lambda carrier: stage_begin(carrier, expected_length=self.expected_length, **self._arguments())
        )
        self.failed = not (
            self._accepted(result, FileStageObservationState.CREATED)
            and self.reference is not None
            and not self.helper_uncertain
        )
        observation = result.observation
        refused_before_creation = (
            self._accepted(result, FileStageObservationState.REFUSED)
            and observation is not None
            and observation.failure is not None
            and observation.failure.code is not FileStageFailureCode.SCRATCH
        )
        if not self.helper_uncertain and (result.dispatch is Dispatch.NOT_SENT or refused_before_creation):
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

            result = self._call(write)
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
                lambda carrier: stage_reconcile(carrier, **self._arguments(cleanup_deadline)), deadline=cleanup_deadline
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
