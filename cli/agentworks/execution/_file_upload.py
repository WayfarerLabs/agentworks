"""Private whole-file upload composition under borrowed core ownership."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._account import FileOwnershipResolutionResult, resolve_file_ownership
from agentworks.execution._file_paths import normalized_relative_path, normalized_root
from agentworks.execution._file_publication import Create, CreateMetadata, Match, PublicationFailureKind, Replace
from agentworks.execution._file_publication_exchange import (
    FilePublicationObservation,
    FilePublicationObservationState,
    publication_cleanup,
    publication_reconcile,
    publish,
)
from agentworks.execution._file_publication_protocol import (
    FilePublicationFailureCode,
    FilePublicationFailureControl,
    FilePublicationRequestError,
    PublicationCleanupState,
    validate_file_publication_inputs,
)
from agentworks.execution._file_stage_exchange import (
    FileStageObservation,
    FileStageObservationState,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from agentworks.execution._file_stage_protocol import (
    MAX_STAGE_CHUNK_BYTES,
    FileStageBeginRequest,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageRequestError,
    encode_file_stage_request,
)
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import IdentityPlan, _validate_plan
from agentworks.execution._publication_receipt import PublicationReceiptFailureKind
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
)
from agentworks.execution._scratch import ScratchFailureKind, ScratchReference, _cleanup_debt
from agentworks.execution.carrier import (
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
)
from agentworks.execution.files import NewMetadata
from agentworks.operations import OperationBorrow

if TYPE_CHECKING:
    from agentworks.execution._file_publication_wire import BoundPublicationCleanupDebt
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import ByteSource, Carrier

_MAX_SIZE = (1 << 63) - 1
_RUNTIME_REFUSALS = frozenset(
    {
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    }
)


class FileUploadStatus(StrEnum):
    """Closed private result categories for one upload composition."""

    COMPLETE = "complete"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class FileUploadFailure(StrEnum):
    """Safe primary failure classification without source or provider text."""

    DEADLINE = "deadline"
    OWNERSHIP = "ownership"
    SOURCE = "source"
    SOURCE_CONTRACT = "source_contract"
    STAGE = "stage"
    PUBLICATION = "publication"
    CLEANUP = "cleanup"
    OBSERVATION = "observation"
    RUNTIME_PREREQUISITE = "runtime_prerequisite"
    TERMINATION = "termination"


class FileUploadFailurePhase(StrEnum):
    """Exact workflow exchange that established the primary failure."""

    OWNERSHIP = "ownership"
    STAGE_BEGIN = "stage_begin"
    STAGE_CHUNK = "stage_chunk"
    PUBLICATION = "publication"
    STAGE_RECONCILE = "stage_reconcile"
    PUBLICATION_RECONCILE = "publication_reconcile"
    PUBLICATION_CLEANUP = "publication_cleanup"
    STAGE_CLEANUP = "stage_cleanup"


@dataclass(frozen=True, slots=True, repr=False)
class FileUploadBinding:
    """Original core-bound upload inputs needed for exact recovery."""

    trusted_root_path: str
    relative_path: str
    expected_size: int
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileUploadOutcome:
    """Bounded upload facts; payloads and source exceptions are never retained."""

    status: FileUploadStatus
    binding: FileUploadBinding
    token: bytes = field(repr=False)
    bytes_consumed: int
    staged_bytes: int
    content_digest: bytes | None = field(default=None, repr=False)
    reference: ScratchReference | None = field(default=None, repr=False)
    scratch_cleanup_debt: ScratchCleanupDebt | None = field(default=None, repr=False)
    publication_cleanup_debt: BoundPublicationCleanupDebt | None = field(default=None, repr=False)
    revision: FileRevision | None = field(default=None, repr=False)
    publication_confirmed: bool = False
    publication_uncertain: bool = False
    stage_ownership_uncertain: bool = False
    publication_ownership_uncertain: bool = False
    pending_remote_effects: bool = False
    deadline_exceeded: bool = False
    failure: FileUploadFailure | None = None
    ownership_result: FileOwnershipResolutionResult | None = None
    failure_phase: FileUploadFailurePhase | None = None
    failure_dispatch: Dispatch | None = None
    carrier_failure: Failure | None = None
    stage_failure: FileStageFailureControl | None = field(default=None, repr=False)
    publication_failure: FilePublicationFailureControl | None = field(default=None, repr=False)
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class FileUploadControlFact(Exception):
    """Safe bounded upload state attached to an escaping control flow."""

    def __init__(self, outcome: FileUploadOutcome) -> None:
        self.outcome = outcome
        super().__init__("private upload stopped with retained operation state")


class FileOwnershipResolutionUncertain(Exception):
    """Safe cause for an ownership lookup without a returned carrier result."""

    def __init__(self) -> None:
        super().__init__("private file ownership lookup observation is unavailable")


@dataclass(slots=True, repr=False)
class _WorkingState:
    binding: FileUploadBinding
    token: bytes
    operation: BorrowedFixedHelperCarrier
    bytes_consumed: int = 0
    staged_bytes: int = 0
    digest: bytes | None = None
    reference: ScratchReference | None = None
    scratch_debt: ScratchCleanupDebt | None = None
    publication_debt: BoundPublicationCleanupDebt | None = None
    revision: FileRevision | None = None
    publication_confirmed: bool = False
    publication_uncertain: bool = False
    stage_ownership_uncertain: bool = False
    publication_ownership_uncertain: bool = False
    pending_remote_effects: bool = False
    deadline_exceeded: bool = False
    failure: FileUploadFailure | None = None
    ownership_result: FileOwnershipResolutionResult | None = None
    failure_phase: FileUploadFailurePhase | None = None
    failure_dispatch: Dispatch | None = None
    carrier_failure: Failure | None = None
    stage_failure: FileStageFailureControl | None = None
    publication_failure: FilePublicationFailureControl | None = None
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None

    def fail(
        self,
        failure: FileUploadFailure,
        *,
        phase: FileUploadFailurePhase | None = None,
        dispatch: Dispatch | None = None,
        carrier_failure: Failure | None = None,
    ) -> None:
        if self.failure is None:
            self.failure = failure
            self.failure_phase = phase
            self.failure_dispatch = dispatch
            self.carrier_failure = carrier_failure

    def finish(self) -> FileUploadOutcome:
        pending_remote_effects = self.pending_remote_effects or self.operation.pending_remote_effects
        coordination_uncertain = self.operation.coordination_uncertain
        retain = (
            pending_remote_effects
            or self.scratch_debt is not None
            or self.publication_debt is not None
            or self.stage_ownership_uncertain
            or self.publication_ownership_uncertain
            or self.operation.has_outstanding_attempt
            or coordination_uncertain
        )
        if self.publication_confirmed and self.failure is None and not retain:
            status = FileUploadStatus.COMPLETE
        elif self.publication_uncertain or pending_remote_effects:
            status = FileUploadStatus.UNCERTAIN
        else:
            status = FileUploadStatus.FAILED
        return FileUploadOutcome(
            status,
            self.binding,
            self.token,
            self.bytes_consumed,
            self.staged_bytes,
            self.digest,
            self.reference,
            self.scratch_debt,
            self.publication_debt,
            self.revision,
            self.publication_confirmed,
            self.publication_uncertain,
            self.stage_ownership_uncertain,
            self.publication_ownership_uncertain,
            pending_remote_effects,
            self.deadline_exceeded,
            self.failure,
            self.ownership_result,
            self.failure_phase,
            self.failure_dispatch,
            self.carrier_failure,
            self.stage_failure,
            self.publication_failure,
            self.runtime_prerequisite,
            coordination_uncertain,
            retain,
        )


def upload_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    source: ByteSource,
    size: int,
    condition: Create | Replace | Match,
    create_metadata: NewMetadata,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> FileUploadOutcome:
    """Consume one exact source under the caller's active serial borrow."""
    return _prepare_upload(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        source=source,
        size=size,
        condition=condition,
        create_metadata=create_metadata,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_upload(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    source: ByteSource,
    size: int,
    condition: Create | Replace | Match,
    create_metadata: NewMetadata,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> _PreparedUpload:
    """Prepare one validated concrete upload without dispatching it."""
    binding, canonical_condition, canonical_metadata = _validate_inputs(
        trusted_root_path,
        relative_path,
        source,
        size,
        condition,
        create_metadata,
        plan,
        deadline,
        runtime_selection,
        borrow,
    )
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    return _prepare_upload_borrowed(
        operation,
        source=source,
        deadline=deadline,
        inputs=(binding, canonical_condition, canonical_metadata),
    )


def _prepare_upload_borrowed(
    operation: BorrowedFixedHelperCarrier,
    *,
    source: ByteSource,
    deadline: Deadline,
    inputs: tuple[FileUploadBinding, Create | Replace | Match, NewMetadata | None],
) -> _PreparedUpload:
    """Prepare one validated nested upload without dispatching it."""
    binding, canonical_condition, canonical_metadata = inputs
    state = _WorkingState(binding, secrets.token_bytes(16), operation)
    workflow = _UploadWorkflow(operation, source, canonical_condition, canonical_metadata, deadline, state)
    return _PreparedUpload(binding, state, workflow)


@dataclass(slots=True, repr=False)
class _PreparedUpload:
    """Validated upload state attachable to core custody before dispatch."""

    binding: FileUploadBinding
    state: _WorkingState
    workflow: _UploadWorkflow
    control_outcome: FileUploadOutcome | None = field(default=None, init=False)

    def run(self) -> FileUploadOutcome:
        try:
            return self.workflow.run()
        except BaseException as control:
            try:
                self.workflow.note_control_stop()
                self.state.fail(FileUploadFailure.OBSERVATION)
                if not self.state.operation.coordination_uncertain:
                    try:
                        self.workflow.cleanup_after_local_stop()
                    except BaseException:
                        self.workflow.note_control_stop()
                        self.state.fail(FileUploadFailure.CLEANUP)
                self.workflow.note_control_stop()
                outcome = self.state.finish()
                fact = FileUploadControlFact(outcome)
                self.control_outcome = outcome
            except BaseException:
                raise control from None
            raise control from fact


class _UploadWorkflow:
    def __init__(
        self,
        carrier: BorrowedFixedHelperCarrier,
        source: ByteSource,
        condition: Create | Replace | Match,
        create_metadata: NewMetadata | None,
        deadline: Deadline,
        state: _WorkingState,
    ) -> None:
        self._carrier = carrier
        self._source = source
        self._condition = condition
        self._new_metadata = create_metadata
        self._create_metadata: CreateMetadata | None = None
        self._deadline = deadline
        self._state = state

    def run(self) -> FileUploadOutcome:
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileUploadFailure.DEADLINE)
            return self._state.finish()
        if not self._resolve_create_metadata():
            return self._state.finish()
        if not self._begin_stage():
            self._cleanup_after_failure()
            return self._state.finish()
        if not self._consume_source():
            self._cleanup_after_failure()
            return self._state.finish()
        if not self._publish():
            self._cleanup_after_failure()
            return self._state.finish()
        self._cleanup_scratch()
        return self._state.finish()

    def _resolve_create_metadata(self) -> bool:
        if not isinstance(self._condition, Create):
            self._new_metadata = None
            self._create_metadata = CreateMetadata(0, 0, 0)
            return True
        metadata = self._new_metadata
        assert metadata is not None
        mode = metadata.mode
        try:
            result = resolve_file_ownership(
                self._carrier,
                metadata.owner,
                metadata.group,
                self._deadline,
                self._state.binding.runtime_selection,
            )
        except BaseException as control:
            raise control from FileOwnershipResolutionUncertain()
        self._state.ownership_result = result
        self._new_metadata = None
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if self._state.deadline_exceeded:
            self._state.fail(
                FileUploadFailure.DEADLINE,
                phase=FileUploadFailurePhase.OWNERSHIP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if not normal or self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileUploadFailure.OWNERSHIP,
                phase=FileUploadFailurePhase.OWNERSHIP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        observation = result.observation
        if observation is None or observation.ownership is None:
            self._state.fail(
                FileUploadFailure.OWNERSHIP,
                phase=FileUploadFailurePhase.OWNERSHIP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        self._create_metadata = CreateMetadata(observation.ownership.uid, observation.ownership.gid, mode)
        return True

    def cleanup_after_local_stop(self) -> None:
        if not self._state.pending_remote_effects and not self._state.operation.coordination_uncertain:
            self._cleanup_after_failure()

    def note_control_stop(self) -> None:
        self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
        if self._state.operation.outstanding_attempt is not None:
            self._state.pending_remote_effects = True

    def _begin_stage(self) -> bool:
        result = stage_begin(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            expected_length=self._state.binding.expected_size,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            if observation.reference is not None:
                self._state.reference = observation.reference
                self._state.scratch_debt = _cleanup_debt(observation.reference)
            self._record_stage_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileUploadFailure.OBSERVATION
                if result.dispatch is Dispatch.NOT_SENT
                else FileUploadFailure.TERMINATION,
                phase=FileUploadFailurePhase.STAGE_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileUploadFailure.RUNTIME_PREREQUISITE,
                phase=FileUploadFailurePhase.STAGE_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if observation is not None and observation.state is FileStageObservationState.CREATED:
            return True
        self._state.fail(
            FileUploadFailure.DEADLINE
            if self._state.deadline_exceeded
            else FileUploadFailure.STAGE
            if observation is not None and observation.state is FileStageObservationState.REFUSED
            else FileUploadFailure.OBSERVATION,
            phase=FileUploadFailurePhase.STAGE_BEGIN,
            dispatch=result.dispatch,
            carrier_failure=result.carrier_failure,
        )
        if observation is None or observation.state in {
            FileStageObservationState.INVALID,
            FileStageObservationState.INCOMPLETE,
            FileStageObservationState.UNCERTAIN,
        }:
            self._reconcile_stage()
        return False

    def _consume_source(self) -> bool:
        reference = self._state.reference
        assert reference is not None
        digest = hashlib.sha256()
        remaining = self._state.binding.expected_size
        while remaining:
            data = self._read_source(min(MAX_STAGE_CHUNK_BYTES, remaining))
            if data is None:
                return False
            if not data:
                self._state.fail(FileUploadFailure.SOURCE_CONTRACT)
                return False
            chunk_digest = hashlib.sha256(data).digest()
            result = stage_chunk(
                self._carrier,
                trusted_root_path=self._state.binding.trusted_root_path,
                relative_path=self._state.binding.relative_path,
                token=self._state.token,
                reference=reference,
                offset=self._state.staged_bytes,
                data=data,
                chunk_digest=chunk_digest,
                plan=self._state.binding.identity_plan,
                deadline=self._deadline,
                runtime_selection=self._state.binding.runtime_selection,
            )
            observation = result.observation
            if result.dispatch is not Dispatch.NOT_SENT:
                self._record_runtime_prerequisite(result.runtime_prerequisite)
            if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
                self._record_stage_observation(observation)
                if observation.state is FileStageObservationState.ACCEPTED:
                    digest.update(data)
                    self._state.staged_bytes += len(data)
            normal = self._settle_attempt(result.dispatch, result.carrier_completion)
            if not normal:
                self._state.fail(
                    FileUploadFailure.OBSERVATION
                    if result.dispatch is Dispatch.NOT_SENT
                    else FileUploadFailure.TERMINATION,
                    phase=FileUploadFailurePhase.STAGE_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            if self._runtime_refused(result.runtime_prerequisite):
                self._state.fail(
                    FileUploadFailure.RUNTIME_PREREQUISITE,
                    phase=FileUploadFailurePhase.STAGE_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            if observation is None or observation.state is not FileStageObservationState.ACCEPTED:
                self._state.fail(
                    FileUploadFailure.DEADLINE
                    if self._state.deadline_exceeded
                    else FileUploadFailure.STAGE
                    if observation is not None and observation.state is FileStageObservationState.REFUSED
                    else FileUploadFailure.OBSERVATION,
                    phase=FileUploadFailurePhase.STAGE_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            remaining = self._state.binding.expected_size - self._state.staged_bytes
        extra = self._read_source(1)
        if extra is None:
            return False
        if extra:
            self._state.fail(FileUploadFailure.SOURCE_CONTRACT)
            return False
        self._state.digest = digest.digest()
        return True

    def _read_source(self, limit: int) -> bytes | None:
        while True:
            if self._deadline.expired:
                self._state.deadline_exceeded = True
                self._state.fail(FileUploadFailure.DEADLINE)
                return None
            try:
                value = self._source.try_read(limit)
            except Exception:
                self._state.fail(FileUploadFailure.SOURCE)
                return None
            if value is None:
                remaining = self._deadline.remaining()
                time.sleep(0.001 if remaining is None else min(0.001, remaining))
                continue
            if type(value) is not bytes or len(value) > limit:
                if type(value) is bytes:
                    self._state.bytes_consumed += len(value)
                self._state.fail(FileUploadFailure.SOURCE_CONTRACT)
                return None
            self._state.bytes_consumed += len(value)
            return value

    def _publish(self) -> bool:
        reference = self._state.reference
        digest = self._state.digest
        create_metadata = self._create_metadata
        assert reference is not None and digest is not None and create_metadata is not None
        result = publish(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            reference=reference,
            digest=digest,
            condition=self._condition,
            create_metadata=create_metadata,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_publication_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileUploadFailure.OBSERVATION
                if result.dispatch is Dispatch.NOT_SENT
                else FileUploadFailure.TERMINATION,
                phase=FileUploadFailurePhase.PUBLICATION,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            if result.dispatch is not Dispatch.NOT_SENT:
                self._state.publication_uncertain = not self._state.publication_confirmed
            return False
        if self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileUploadFailure.RUNTIME_PREREQUISITE,
                phase=FileUploadFailurePhase.PUBLICATION,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if observation is not None and observation.state is FilePublicationObservationState.PUBLISHED:
            if observation.deadline_exceeded:
                self._state.fail(
                    FileUploadFailure.DEADLINE,
                    phase=FileUploadFailurePhase.PUBLICATION,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            return True
        if observation is not None and observation.state is FilePublicationObservationState.REFUSED:
            failure = FileUploadFailure.DEADLINE if self._state.deadline_exceeded else FileUploadFailure.PUBLICATION
        else:
            failure = FileUploadFailure.OBSERVATION
            self._state.publication_uncertain = True
        self._state.fail(
            failure,
            phase=FileUploadFailurePhase.PUBLICATION,
            dispatch=result.dispatch,
            carrier_failure=result.carrier_failure,
        )
        if self._state.publication_ownership_uncertain or (
            self._state.publication_debt is None
            and (
                observation is None
                or observation.state
                in {
                    FilePublicationObservationState.INVALID,
                    FilePublicationObservationState.INCOMPLETE,
                    FilePublicationObservationState.UNCERTAIN,
                }
            )
        ):
            self._reconcile_publication()
        return False

    def _reconcile_stage(self) -> None:
        if self._deadline.expired or self._state.pending_remote_effects:
            self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
            self._state.stage_ownership_uncertain = self._state.scratch_debt is None
            return
        result = stage_reconcile(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_stage_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileUploadFailure.OBSERVATION
                if result.dispatch is Dispatch.NOT_SENT
                else FileUploadFailure.TERMINATION,
                phase=FileUploadFailurePhase.STAGE_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            self._state.stage_ownership_uncertain = self._state.scratch_debt is None
            return
        if self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileUploadFailure.RUNTIME_PREREQUISITE,
                phase=FileUploadFailurePhase.STAGE_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            self._state.stage_ownership_uncertain = self._state.scratch_debt is None
            return
        self._state.stage_ownership_uncertain = self._state.scratch_debt is None

    def _reconcile_publication(self) -> None:
        reference = self._state.reference
        assert reference is not None
        if self._deadline.expired or self._state.pending_remote_effects:
            self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
            self._state.publication_ownership_uncertain = self._state.publication_debt is None
            return
        result = publication_reconcile(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            reference=reference,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_publication_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileUploadFailure.OBSERVATION
                if result.dispatch is Dispatch.NOT_SENT
                else FileUploadFailure.TERMINATION,
                phase=FileUploadFailurePhase.PUBLICATION_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            self._state.publication_ownership_uncertain = self._state.publication_debt is None
            return
        if self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileUploadFailure.RUNTIME_PREREQUISITE,
                phase=FileUploadFailurePhase.PUBLICATION_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            self._state.publication_ownership_uncertain = self._state.publication_debt is None
            return
        self._state.publication_ownership_uncertain = self._state.publication_debt is None

    def _cleanup_after_failure(self) -> None:
        if (
            self._state.pending_remote_effects
            or self._state.deadline_exceeded
            or self._runtime_refused(self._state.runtime_prerequisite)
        ):
            return
        if self._state.publication_debt is not None:
            self._cleanup_publication()
        if self._state.publication_debt is not None or self._state.publication_ownership_uncertain:
            return
        if self._state.deadline_exceeded:
            return
        self._cleanup_scratch()

    def _cleanup_publication(self) -> None:
        reference = self._state.reference
        debt = self._state.publication_debt
        assert reference is not None and debt is not None
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileUploadFailure.DEADLINE)
            return
        result = publication_cleanup(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            reference=reference,
            cleanup_debt=debt,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_publication_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if normal and observation is not None and observation.state is FilePublicationObservationState.CLEANED:
            self._state.publication_debt = None
        else:
            self._state.fail(
                FileUploadFailure.DEADLINE if self._state.deadline_exceeded else FileUploadFailure.CLEANUP,
                phase=FileUploadFailurePhase.PUBLICATION_CLEANUP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )

    def _cleanup_scratch(self) -> None:
        debt = self._state.scratch_debt
        if debt is None or self._state.pending_remote_effects:
            return
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileUploadFailure.DEADLINE)
            return
        result = stage_cleanup(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            token=self._state.token,
            cleanup_debt=debt,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime_prerequisite(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_stage_observation(observation)
        normal = self._settle_attempt(result.dispatch, result.carrier_completion)
        if normal and observation is not None and observation.state is FileStageObservationState.CLEANED:
            self._state.scratch_debt = None
        else:
            self._state.fail(
                FileUploadFailure.DEADLINE if self._state.deadline_exceeded else FileUploadFailure.CLEANUP,
                phase=FileUploadFailurePhase.STAGE_CLEANUP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )

    def _settle_attempt(
        self,
        dispatch: Dispatch,
        completion: ExitStatus | None,
    ) -> bool:
        normal = self._state.operation.settle(dispatch, completion)
        self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
        self._state.pending_remote_effects = (
            self._state.pending_remote_effects or self._state.operation.pending_remote_effects
        )
        return normal

    def _record_stage_observation(self, observation: FileStageObservation) -> None:
        if observation.cleanup_debt is not None:
            self._state.scratch_debt = observation.cleanup_debt
        failure = observation.failure
        if failure is not None:
            if self._state.stage_failure is None:
                self._state.stage_failure = failure
            if failure.cleanup_debt is not None:
                self._state.scratch_debt = failure.cleanup_debt
            if failure.code is FileStageFailureCode.DEADLINE or (
                failure.code is FileStageFailureCode.SCRATCH and failure.kind is ScratchFailureKind.DEADLINE
            ):
                self._state.deadline_exceeded = True

    def _record_publication_observation(self, observation: FilePublicationObservation) -> None:
        if observation.revision is not None:
            self._state.revision = observation.revision
            self._state.publication_confirmed = True
        if observation.cleanup_debt is not None:
            self._state.publication_debt = observation.cleanup_debt
        failure = observation.failure
        if failure is not None:
            if self._state.failure is None and self._state.publication_failure is None:
                self._state.publication_failure = failure
            if failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN:
                self._state.publication_debt = None
                self._state.publication_ownership_uncertain = True
            if self._publication_deadline(failure):
                self._state.deadline_exceeded = True
        if observation.deadline_exceeded:
            self._state.deadline_exceeded = True

    @staticmethod
    def _publication_deadline(failure: FilePublicationFailureControl) -> bool:
        return (
            failure.code is FilePublicationFailureCode.DEADLINE
            or (
                failure.code is FilePublicationFailureCode.SCRATCH
                and failure.scratch_kind is ScratchFailureKind.DEADLINE
            )
            or (
                failure.code is FilePublicationFailureCode.PUBLICATION
                and failure.publication_kind is PublicationFailureKind.DEADLINE
            )
            or (
                failure.code is FilePublicationFailureCode.RECEIPT
                and failure.receipt_kind is PublicationReceiptFailureKind.DEADLINE
            )
        )

    def _record_runtime_prerequisite(self, observation: RuntimePrerequisiteObservation) -> None:
        current = self._state.runtime_prerequisite
        rank = (
            2 if observation.state in _RUNTIME_REFUSALS else int(observation.state is RuntimePrerequisiteState.UNKNOWN)
        )
        current_rank = (
            -1
            if current is None
            else 2
            if current.state in _RUNTIME_REFUSALS
            else int(current.state is RuntimePrerequisiteState.UNKNOWN)
        )
        if rank > current_rank:
            self._state.runtime_prerequisite = observation

    @staticmethod
    def _runtime_refused(observation: RuntimePrerequisiteObservation | None) -> bool:
        return observation is not None and observation.state in _RUNTIME_REFUSALS


def _validate_inputs(
    trusted_root_path: object,
    relative_path: object,
    source: object,
    size: object,
    condition: object,
    create_metadata: object,
    plan: object,
    deadline: object,
    runtime_selection: object,
    borrow: object,
) -> tuple[FileUploadBinding, Create | Replace | Match, NewMetadata]:
    if type(trusted_root_path) is not str or not normalized_root(trusted_root_path):
        raise ValidationError("Upload requires a normalized absolute trusted root")
    if type(relative_path) is not str or not normalized_relative_path(relative_path):
        raise ValidationError("Upload requires a normalized nonempty relative path")
    if type(size) is not int or not 0 <= size <= _MAX_SIZE:
        raise ValidationError("Upload size must be a nonnegative bounded integer")
    if not isinstance(condition, Create | Replace | Match):
        raise ValidationError("Upload requires one supported publication condition")
    if type(create_metadata) is not NewMetadata:
        raise ValidationError("Upload requires named create metadata")
    if create_metadata.mode > 0o777:
        raise ValidationError("Regular-file creation mode must contain only permission bits")
    publication_inputs: tuple[Create | Replace | Match, CreateMetadata] | None = None
    publication_validation_failed = False
    try:
        publication_inputs = validate_file_publication_inputs(condition, CreateMetadata(0, 0, 0))
    except FilePublicationRequestError:
        publication_validation_failed = True
    if publication_validation_failed or publication_inputs is None:
        raise ValidationError("Upload requires publication inputs accepted by the file protocol")
    if type(plan) is not IdentityPlan:
        raise ValidationError("Upload requires a bound identity plan")
    _validate_plan(plan)
    if type(deadline) is not Deadline:
        raise ValidationError("Upload requires one shared deadline")
    if type(runtime_selection) is not RuntimeSelection:
        raise ValidationError("Upload requires a bound runtime selection")
    if type(borrow) is not OperationBorrow:
        raise ValidationError("Upload requires an active core operation borrow")
    stage_validation_failed = False
    try:
        encode_file_stage_request(
            FileStageBeginRequest(
                "0" * 32,
                trusted_root_path,
                relative_path,
                b"\0" * 16,
                size,
                plan.expected,
                deadline.remaining(),
            )
        )
    except FileStageRequestError:
        stage_validation_failed = True
    if stage_validation_failed:
        raise ValidationError("Upload requires stage inputs accepted by the file protocol")
    getter_failed = False
    reader = None
    try:
        reader = getattr(source, "try_read", None)
    except Exception:
        getter_failed = True
    if getter_failed or not callable(reader):
        raise ValidationError("Upload requires a nonblocking byte source")
    return (
        FileUploadBinding(trusted_root_path, relative_path, size, plan, runtime_selection),
        publication_inputs[0],
        create_metadata,
    )
