"""Private bounded JSON-file composition under one borrowed core operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ValidationError
from agentworks.execution._file_object_exchange import (
    FileObjectObservation,
    FileObjectObservationState,
    stat_file,
)
from agentworks.execution._file_objects import FileKind, FileObjectFailureKind
from agentworks.execution._file_operation import BorrowedFileCarrier
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    Match,
    PublicationFailureKind,
    PublicationPhase,
    Replace,
)
from agentworks.execution._file_publication_protocol import FilePublicationFailureCode
from agentworks.execution._file_read import FileReadObservation, FileReadObservationState, read_file
from agentworks.execution._file_read_protocol import FileReadFailure
from agentworks.execution._file_stat import FileRevision
from agentworks.execution._file_upload import (
    FileUploadControlFact,
    FileUploadFailure,
    FileUploadOutcome,
    FileUploadStatus,
    _upload_file_borrowed,
    _validate_inputs,
)
from agentworks.execution._json import (
    ValidatedJsonObject,
    merge_json_objects,
    serialize_json_source,
    validate_json_object,
    validate_json_source,
)
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
)
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.operations import OperationOwner

if TYPE_CHECKING:
    from agentworks.execution._file_object_protocol import FileObjectFailureControl
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier

type JsonFileStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]

_MAX_PUBLICATION_ATTEMPTS = 8
_RUNTIME_REFUSALS = frozenset(
    {
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    }
)


class FileJsonStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class FileJsonChange(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class FileJsonFailure(StrEnum):
    DEADLINE = "deadline"
    ABSENT = "absent"
    OBJECT = "object"
    READ = "read"
    INVALID_EXISTING = "invalid_existing"
    TRANSFORM = "transform"
    PUBLICATION = "publication"
    CONFLICT = "conflict"
    CLEANUP = "cleanup"
    OBSERVATION = "observation"
    RUNTIME_PREREQUISITE = "runtime_prerequisite"
    TERMINATION = "termination"


@dataclass(frozen=True, slots=True, repr=False)
class FileJsonBinding:
    trusted_root_path: str
    relative_path: str
    strategy: JsonFileStrategy
    create: bool
    max_bytes: int
    max_depth: int
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileJsonOutcome:
    status: FileJsonStatus
    binding: FileJsonBinding
    change: FileJsonChange | None = None
    revision: FileRevision | None = field(default=None, repr=False)
    publication_attempts: int = 0
    failure: FileJsonFailure | None = None
    object_failure: FileObjectFailureControl | None = None
    read_failure: FileReadFailure | None = None
    upload_outcome: FileUploadOutcome | None = field(default=None, repr=False)
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class FileJsonControlFact(Exception):
    """Safe bounded JSON state attached to an escaping control flow."""

    def __init__(self, outcome: FileJsonOutcome) -> None:
        self.outcome = outcome
        super().__init__("private JSON update stopped with retained operation state")


class _BytesSource:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0

    def try_read(self, limit: int) -> bytes:
        if self._offset == len(self._content):
            return b""
        end = min(len(self._content), self._offset + limit)
        result = self._content[self._offset : end]
        self._offset = end
        return result


@dataclass(slots=True, repr=False)
class _Inputs:
    binding: FileJsonBinding
    source: ValidatedJsonObject
    create_metadata: CreateMetadata
    owner: OperationOwner


@dataclass(slots=True, repr=False)
class _State:
    binding: FileJsonBinding
    operation: BorrowedFileCarrier
    change: FileJsonChange | None = None
    revision: FileRevision | None = None
    publication_attempts: int = 0
    failure: FileJsonFailure | None = None
    object_failure: FileObjectFailureControl | None = None
    read_failure: FileReadFailure | None = None
    upload_outcome: FileUploadOutcome | None = None
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None
    deadline_exceeded: bool = False

    def fail(self, failure: FileJsonFailure) -> None:
        if self.failure is None:
            self.failure = failure

    def record_runtime(self, observation: RuntimePrerequisiteObservation) -> None:
        current = self.runtime_prerequisite
        replace = current is None or (
            current.state is RuntimePrerequisiteState.READY and observation.state is not RuntimePrerequisiteState.READY
        )
        replace = replace or (
            current is not None
            and current.state is RuntimePrerequisiteState.UNKNOWN
            and observation.state in _RUNTIME_REFUSALS
        )
        if replace:
            self.runtime_prerequisite = observation

    def finish(self) -> FileJsonOutcome:
        upload = self.upload_outcome
        pending = self.operation.pending_remote_effects or (upload is not None and upload.pending_remote_effects)
        coordination = self.operation.coordination_uncertain or (upload is not None and upload.coordination_uncertain)
        retain = self.operation.requires_owner_retention or (upload is not None and upload.requires_owner_retention)
        if self.change is not None and self.failure is None and not retain:
            status = FileJsonStatus.COMPLETE
        elif pending or (upload is not None and upload.status is FileUploadStatus.UNCERTAIN):
            status = FileJsonStatus.UNCERTAIN
        else:
            status = FileJsonStatus.FAILED
        return FileJsonOutcome(
            status,
            self.binding,
            self.change,
            self.revision,
            self.publication_attempts,
            self.failure,
            self.object_failure,
            self.read_failure,
            upload,
            self.runtime_prerequisite,
            self.deadline_exceeded,
            pending,
            coordination,
            retain,
        )


def update_json_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    source: bytes,
    strategy: JsonFileStrategy,
    create: bool,
    create_metadata: CreateMetadata,
    max_bytes: int,
    max_depth: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> FileJsonOutcome:
    """Apply one bounded JSON strategy while holding one serial owner borrow."""
    inputs = _validate_json_inputs(
        trusted_root_path,
        relative_path,
        source,
        strategy,
        create,
        create_metadata,
        max_bytes,
        max_depth,
        plan,
        deadline,
        runtime_selection,
        owner,
    )
    borrow = owner.borrow()
    operation = BorrowedFileCarrier(carrier, borrow)
    state = _State(inputs.binding, operation)
    workflow = _JsonWorkflow(inputs, deadline, state)
    try:
        try:
            return workflow.run()
        except BaseException as control:
            cause = control.__cause__
            if isinstance(cause, FileUploadControlFact):
                state.upload_outcome = cause.outcome
                if cause.outcome.runtime_prerequisite is not None:
                    state.record_runtime(cause.outcome.runtime_prerequisite)
                state.deadline_exceeded = state.deadline_exceeded or cause.outcome.deadline_exceeded
            raise control from FileJsonControlFact(state.finish())
    finally:
        borrow.close()


class _JsonWorkflow:
    def __init__(self, inputs: _Inputs, deadline: Deadline, state: _State) -> None:
        self._inputs = inputs
        self._deadline = deadline
        self._state = state

    def run(self) -> FileJsonOutcome:
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileJsonFailure.DEADLINE)
            return self._state.finish()
        strategy = self._state.binding.strategy
        if strategy == "replace" or strategy == "skip-existing":
            return self._run_nonmerge(strategy)
        if strategy == "merge-overwrite" or strategy == "merge-preserve":
            return self._run_merge(strategy)
        raise AssertionError("validated JSON strategy is not closed")

    def _run_nonmerge(self, strategy: Literal["replace", "skip-existing"]) -> FileJsonOutcome:
        observation = self._stat()
        if observation is None:
            return self._state.finish()
        if observation.state is FileObjectObservationState.PRESENT:
            if observation.object_kind is not FileKind.REGULAR or observation.revision is None:
                self._state.fail(FileJsonFailure.OBJECT)
                return self._state.finish()
            if strategy == "skip-existing":
                self._state.change = FileJsonChange.UNCHANGED
                self._state.revision = observation.revision
                return self._state.finish()
            condition: Create | Replace = Replace()
        elif observation.state is FileObjectObservationState.ABSENT:
            if not self._state.binding.create:
                self._state.fail(FileJsonFailure.ABSENT)
                return self._state.finish()
            condition = Create()
        else:
            return self._state.finish()
        content = self._serialize_source()
        if content is None:
            return self._state.finish()
        return self._publish(content, condition)

    def _run_merge(self, strategy: Literal["merge-overwrite", "merge-preserve"]) -> FileJsonOutcome:
        for attempt in range(_MAX_PUBLICATION_ATTEMPTS):
            observation = self._read()
            if observation is None:
                return self._state.finish()
            if observation.state is FileReadObservationState.ABSENT:
                if not self._state.binding.create:
                    self._state.fail(FileJsonFailure.ABSENT)
                    return self._state.finish()
                source_content = self._serialize_source()
                if source_content is None:
                    return self._state.finish()
                content = source_content
                condition: Create | Match = Create()
            elif observation.state is FileReadObservationState.PRESENT:
                snapshot = observation.snapshot
                if snapshot is None:
                    self._state.fail(FileJsonFailure.OBSERVATION)
                    return self._state.finish()
                try:
                    existing = validate_json_object(
                        snapshot.data,
                        max_bytes=self._state.binding.max_bytes,
                        max_depth=self._state.binding.max_depth,
                    )
                except ValidationError:
                    self._state.fail(FileJsonFailure.INVALID_EXISTING)
                    return self._state.finish()
                try:
                    content = merge_json_objects(
                        self._inputs.source,
                        existing,
                        preserve_existing=strategy == "merge-preserve",
                    )
                except ValidationError:
                    self._state.fail(FileJsonFailure.TRANSFORM)
                    return self._state.finish()
                condition = Match(FileRevision(snapshot.metadata, snapshot.digest))
            else:
                return self._state.finish()
            outcome = self._upload(content, condition)
            if outcome.status is FileUploadStatus.COMPLETE:
                return self._complete_upload(outcome)
            if not self._retryable_conflict(outcome):
                self._record_upload_failure(outcome)
                return self._state.finish()
            if attempt == _MAX_PUBLICATION_ATTEMPTS - 1:
                self._state.fail(FileJsonFailure.CONFLICT)
                return self._state.finish()
        raise AssertionError("bounded JSON publication loop did not terminate")

    def _stat(self) -> FileObjectObservation | None:
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileJsonFailure.DEADLINE)
            return None
        result = stat_file(
            self._state.operation,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        self._state.record_runtime(result.runtime_prerequisite)
        observation = result.observation
        if observation is not None and observation.failure is not None:
            self._state.object_failure = observation.failure
            if observation.failure.kind is FileObjectFailureKind.DEADLINE:
                self._state.deadline_exceeded = True
                self._state.fail(FileJsonFailure.DEADLINE)
        normal = self._state.operation.settle(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileJsonFailure.OBSERVATION if result.dispatch is Dispatch.NOT_SENT else FileJsonFailure.TERMINATION
            )
            return None
        if _runtime_refused(result.runtime_prerequisite):
            self._state.fail(FileJsonFailure.RUNTIME_PREREQUISITE)
            return None
        if observation is None:
            self._state.fail(FileJsonFailure.OBSERVATION)
            return None
        if observation.state is FileObjectObservationState.REFUSED:
            self._state.fail(FileJsonFailure.OBJECT)
        elif observation.state not in {
            FileObjectObservationState.PRESENT,
            FileObjectObservationState.ABSENT,
        }:
            self._state.fail(FileJsonFailure.OBSERVATION)
        return observation

    def _read(self) -> FileReadObservation | None:
        if self._deadline.expired:
            self._state.deadline_exceeded = True
            self._state.fail(FileJsonFailure.DEADLINE)
            return None
        result = read_file(
            self._state.operation,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            max_bytes=self._state.binding.max_bytes,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        self._state.record_runtime(result.runtime_prerequisite)
        observation = result.observation
        if observation is not None and observation.failure is not None:
            self._state.read_failure = observation.failure
            if observation.failure is FileReadFailure.DEADLINE:
                self._state.deadline_exceeded = True
                self._state.fail(FileJsonFailure.DEADLINE)
        normal = self._state.operation.settle(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileJsonFailure.OBSERVATION if result.dispatch is Dispatch.NOT_SENT else FileJsonFailure.TERMINATION
            )
            return None
        if _runtime_refused(result.runtime_prerequisite):
            self._state.fail(FileJsonFailure.RUNTIME_PREREQUISITE)
            return None
        if observation is None:
            self._state.fail(FileJsonFailure.OBSERVATION)
            return None
        if observation.state is FileReadObservationState.REFUSED:
            self._state.fail(FileJsonFailure.READ)
        elif observation.state not in {
            FileReadObservationState.PRESENT,
            FileReadObservationState.ABSENT,
        }:
            self._state.fail(FileJsonFailure.OBSERVATION)
        return observation

    def _publish(self, content: bytes, condition: Create | Replace) -> FileJsonOutcome:
        outcome = self._upload(content, condition)
        if outcome.status is FileUploadStatus.COMPLETE:
            return self._complete_upload(outcome)
        self._record_upload_failure(outcome)
        return self._state.finish()

    def _serialize_source(self) -> bytes | None:
        try:
            return serialize_json_source(self._inputs.source)
        except ValidationError:
            self._state.fail(FileJsonFailure.TRANSFORM)
            return None

    def _upload(self, content: bytes, condition: Create | Replace | Match) -> FileUploadOutcome:
        source = _BytesSource(content)
        inputs = _validate_inputs(
            self._state.binding.trusted_root_path,
            self._state.binding.relative_path,
            source,
            len(content),
            condition,
            self._inputs.create_metadata,
            self._state.binding.identity_plan,
            self._deadline,
            self._state.binding.runtime_selection,
            self._inputs.owner,
        )
        self._state.publication_attempts += 1
        outcome = _upload_file_borrowed(
            self._state.operation,
            source=source,
            deadline=self._deadline,
            inputs=inputs,
        )
        self._state.upload_outcome = outcome
        if outcome.runtime_prerequisite is not None:
            self._state.record_runtime(outcome.runtime_prerequisite)
        self._state.deadline_exceeded = self._state.deadline_exceeded or outcome.deadline_exceeded
        return outcome

    def _complete_upload(self, outcome: FileUploadOutcome) -> FileJsonOutcome:
        if outcome.revision is None:
            self._state.fail(FileJsonFailure.OBSERVATION)
        else:
            self._state.change = FileJsonChange.CHANGED
            self._state.revision = outcome.revision
        return self._state.finish()

    def _record_upload_failure(self, outcome: FileUploadOutcome) -> None:
        mapping = {
            FileUploadFailure.DEADLINE: FileJsonFailure.DEADLINE,
            FileUploadFailure.PUBLICATION: FileJsonFailure.PUBLICATION,
            FileUploadFailure.CLEANUP: FileJsonFailure.CLEANUP,
            FileUploadFailure.RUNTIME_PREREQUISITE: FileJsonFailure.RUNTIME_PREREQUISITE,
            FileUploadFailure.TERMINATION: FileJsonFailure.TERMINATION,
        }
        failure = outcome.failure
        self._state.fail(
            FileJsonFailure.OBSERVATION if failure is None else mapping.get(failure, FileJsonFailure.OBSERVATION)
        )

    @staticmethod
    def _retryable_conflict(outcome: FileUploadOutcome) -> bool:
        failure = outcome.publication_failure
        return (
            outcome.status is FileUploadStatus.FAILED
            and outcome.failure is FileUploadFailure.PUBLICATION
            and failure is not None
            and failure.code is FilePublicationFailureCode.PUBLICATION
            and failure.publication_kind is PublicationFailureKind.CONFLICT
            and failure.publication_phase is PublicationPhase.CONDITION
            and not outcome.deadline_exceeded
            and not _runtime_refused_outcome(outcome.runtime_prerequisite)
            and not outcome.requires_owner_retention
        )


def _validate_json_inputs(
    trusted_root_path: object,
    relative_path: object,
    source: object,
    strategy: object,
    create: object,
    create_metadata: object,
    max_bytes: object,
    max_depth: object,
    plan: object,
    deadline: object,
    runtime_selection: object,
    owner: object,
) -> _Inputs:
    if type(source) is not bytes:
        raise ValidationError("JSON update requires source bytes")
    canonical_strategy = _validate_strategy(strategy)
    if type(create) is not bool:
        raise ValidationError("JSON update create must be a boolean")
    if type(max_bytes) is not int or type(max_depth) is not int:
        raise ValidationError("JSON update limits must be positive integers")
    validated = validate_json_source(source, max_bytes=max_bytes, max_depth=max_depth)
    if not isinstance(create_metadata, CreateMetadata):
        raise ValidationError("JSON update requires numeric create metadata")
    if type(owner) is not OperationOwner:
        raise ValidationError("JSON update requires core operation ownership")
    probe = _BytesSource(b"")
    binding, _, canonical_metadata = _validate_inputs(
        trusted_root_path,
        relative_path,
        probe,
        0,
        Create(),
        create_metadata,
        plan,
        deadline,
        runtime_selection,
        owner,
    )
    json_binding = FileJsonBinding(
        binding.trusted_root_path,
        binding.relative_path,
        canonical_strategy,
        create,
        max_bytes,
        max_depth,
        binding.identity_plan,
        binding.runtime_selection,
    )
    return _Inputs(json_binding, validated, canonical_metadata, owner)


def _runtime_refused(observation: RuntimePrerequisiteObservation) -> bool:
    return observation.state in _RUNTIME_REFUSALS


def _runtime_refused_outcome(observation: RuntimePrerequisiteObservation | None) -> bool:
    return observation is not None and _runtime_refused(observation)


def _validate_strategy(value: object) -> JsonFileStrategy:
    if type(value) is not str:
        raise ValidationError("JSON update requires one supported strategy")
    if value == "replace":
        return "replace"
    if value == "merge-overwrite":
        return "merge-overwrite"
    if value == "merge-preserve":
        return "merge-preserve"
    if value == "skip-existing":
        return "skip-existing"
    raise ValidationError("JSON update requires one supported strategy")
