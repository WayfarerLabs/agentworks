"""Reduce private file workflow facts to public values and typed failures."""

from __future__ import annotations

import stat
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Never, Protocol

from agentworks.errors import (
    AgentworksError,
    ConflictError,
    ErrorDetails,
    ExternalError,
    LimitExceededError,
    PartialMutationError,
    StateError,
    UncertainOutcomeError,
)

from ._account import AccountObservationState
from ._account_protocol import FileOwnershipFailure
from ._file_inventory_exchange import (
    FileInventoryCandidateResult,
    FileInventoryObservationState,
)
from ._file_inventory_protocol import FileInventoryFailureCode
from ._file_metadata import MetadataFailureKind, MetadataPhase
from ._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservationState,
)
from ._file_metadata_protocol import FileMetadataFailureCode, FileMetadataFailureControl
from ._file_object_exchange import FileObjectCandidateResult, FileObjectObservation, FileObjectObservationState
from ._file_object_protocol import FileObjectFailureCode, FileObjectFailureControl
from ._file_objects import FileObjectFailureKind, FileObjectPhase
from ._file_read_protocol import FileReadFailure
from ._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimePrerequisiteState
from .carrier import Dispatch, Failure
from .files import (
    Change,
    DirectoryEntry,
    FileFailureReason,
    FileMetadata,
    FileOperationPhase,
    MutationResult,
    _kind_from_mode,
    _revision_from_file_revision,
)

if TYPE_CHECKING:
    from enum import StrEnum

    from ._file_inventory import FileInventoryEntry
    from ._file_operations import OwnedFileOutcome
    from ._file_stat import FileRevision
    from .carrier import ExitStatus


class _Candidate(Protocol):
    @property
    def dispatch(self) -> Dispatch: ...

    @property
    def carrier_completion(self) -> ExitStatus | None: ...

    @property
    def carrier_failure(self) -> Failure | None: ...

    @property
    def runtime_prerequisite(self) -> RuntimePrerequisiteObservation: ...


def _metadata(revision: FileRevision) -> FileMetadata:
    token = _revision_from_file_revision(revision)
    observed = revision.stat
    return FileMetadata(
        _kind_from_mode(observed.mode),
        observed.size,
        stat.S_IMODE(observed.mode),
        observed.uid,
        observed.gid,
        observed.modified_ns,
        token,
    )


def _entry(entry: FileInventoryEntry) -> DirectoryEntry:
    return DirectoryEntry(PurePosixPath(entry.relative_path), _metadata(entry.revision))


def _details(
    phase: FileOperationPhase,
    reason: FileFailureReason,
    *,
    effect: Change | None = None,
) -> ErrorDetails:
    return ErrorDetails(phase, reason, effect)


def _raise(
    error_type: type[AgentworksError],
    phase: FileOperationPhase,
    reason: FileFailureReason,
    *,
    entity_kind: str,
    entity_name: str,
    effect: Change | None = None,
) -> Never:
    message, hint = _diagnostic(phase, reason, effect=effect)
    raise error_type(
        message,
        entity_kind=entity_kind,
        entity_name=entity_name,
        hint=hint,
        details=_details(phase, reason, effect=effect),
    ) from None


def _diagnostic(
    phase: FileOperationPhase,
    reason: FileFailureReason,
    *,
    effect: Change | None,
) -> tuple[str, str | None]:
    """Select value-free operator guidance from closed public facts."""
    if effect is Change.CHANGED:
        if reason is FileFailureReason.DEADLINE:
            message = "file target changed, but the operation deadline expired"
        elif phase is FileOperationPhase.CLEANUP:
            message = "file target changed, but cleanup did not complete"
        else:
            message = "file target changed, but the operation did not complete cleanly"
        return message, "Inspect the changed target before deciding whether to retry."
    return {
        FileFailureReason.DEADLINE: (
            "file operation deadline expired",
            "Retry with a deadline that allows the operation to finish.",
        ),
        FileFailureReason.MISSING_OWNER: (
            "requested file owner does not exist",
            "Choose an owner that exists on the target.",
        ),
        FileFailureReason.MISSING_GROUP: (
            "requested file group does not exist",
            "Choose a group that exists on the target.",
        ),
        FileFailureReason.RUNTIME_MISSING: (
            "required Python runtime is missing on the target",
            "Install Python 3 on the target and retry.",
        ),
        FileFailureReason.RUNTIME_SHIM: (
            "target Python resolves to the Xcode command-line tools shim",
            "Install a usable Python 3 runtime on the target and retry.",
        ),
        FileFailureReason.RUNTIME_UNUSABLE: (
            "target Python runtime is unusable",
            "Repair or replace the target Python 3 runtime and retry.",
        ),
        FileFailureReason.RUNTIME_UNSUPPORTED_VERSION: (
            "target Python runtime version is unsupported",
            "Install a supported Python 3 version on the target and retry.",
        ),
        FileFailureReason.RUNTIME_MISSING_MODULES: (
            "target Python runtime is missing required modules",
            "Use a complete Python 3 installation on the target and retry.",
        ),
        FileFailureReason.RUNTIME_UNKNOWN: (
            "target Python runtime could not be verified",
            "Verify the target Python 3 installation and retry.",
        ),
        FileFailureReason.NOT_FOUND: ("file target was not found", None),
        FileFailureReason.UNSUPPORTED: ("file target or operation is unsupported", None),
        FileFailureReason.REFUSED: ("file operation was refused", None),
        FileFailureReason.LIMIT: (
            "file operation exceeded a configured safety limit",
            "Raise the applicable bounded file limit or narrow the operation.",
        ),
        FileFailureReason.CONFLICT: (
            "file operation precondition no longer matches the target",
            "Observe the target again before retrying.",
        ),
        FileFailureReason.INTEGRITY: ("file content failed integrity verification", None),
        FileFailureReason.INVALID_RESPONSE: ("file operation returned an invalid response", None),
        FileFailureReason.INCOMPLETE_RESPONSE: ("file operation returned an incomplete response", None),
        FileFailureReason.CARRIER_DISPATCH: ("file operation could not be submitted", None),
        FileFailureReason.CARRIER_OBSERVATION: ("file operation result could not be observed", None),
        FileFailureReason.CARRIER_INPUT: ("file operation input transfer failed", None),
        FileFailureReason.CARRIER_OUTPUT: ("file operation output transfer failed", None),
        FileFailureReason.CARRIER_OUTPUT_LIMIT: (
            "file operation output exceeded its safety limit",
            "Narrow the operation or raise its bounded output limit.",
        ),
        FileFailureReason.SOURCE: ("file source could not be read", None),
        FileFailureReason.SOURCE_CONTRACT: ("file source violated the transfer contract", None),
        FileFailureReason.SINK: ("file destination could not accept the transfer", None),
        FileFailureReason.SINK_CONTRACT: ("file destination violated the transfer contract", None),
        FileFailureReason.TERMINATION: ("file operation did not terminate successfully", None),
        FileFailureReason.IO: ("file operation failed during target I/O", None),
        FileFailureReason.CLEANUP: (
            "file operation cleanup did not complete",
            "Inspect the target before retrying the operation.",
        ),
        FileFailureReason.COORDINATION: (
            "file operation could not complete ownership handoff",
            "Retry after the unfinished operation has been reconciled.",
        ),
        FileFailureReason.EXISTING_CONTENT: ("existing file content cannot be updated safely", None),
        FileFailureReason.TRANSFORM: ("file content transformation failed", None),
    }[reason]


def _raise_reason(
    phase: FileOperationPhase,
    reason: FileFailureReason,
    *,
    entity_kind: str,
    entity_name: str,
    effect: Change | None = None,
) -> Never:
    error_type: type[AgentworksError]
    if reason is FileFailureReason.LIMIT:
        error_type = LimitExceededError
    elif reason is FileFailureReason.CONFLICT:
        error_type = ConflictError
    elif reason in {
        FileFailureReason.NOT_FOUND,
        FileFailureReason.UNSUPPORTED,
        FileFailureReason.REFUSED,
        FileFailureReason.MISSING_OWNER,
        FileFailureReason.MISSING_GROUP,
    }:
        error_type = StateError
    else:
        error_type = ExternalError
    _raise(
        error_type,
        phase,
        reason,
        entity_kind=entity_kind,
        entity_name=entity_name,
        effect=effect,
    )


def _raise_uncertain(
    phase: FileOperationPhase,
    reason: FileFailureReason,
    *,
    entity_kind: str,
    entity_name: str,
    dispatch: Dispatch | None,
    completed_steps: tuple[StrEnum, ...] = (),
    attempted_step: StrEnum | None = None,
    effect: Change | None = None,
) -> Never:
    raise UncertainOutcomeError(
        "file mutation may have changed its target",
        dispatch=dispatch,
        completed_steps=completed_steps,
        attempted_step=attempted_step,
        entity_kind=entity_kind,
        entity_name=entity_name,
        hint="Inspect the target before retrying the operation.",
        details=_details(phase, reason, effect=effect),
    ) from None


def _runtime_reason(observation: RuntimePrerequisiteObservation) -> FileFailureReason | None:
    return {
        RuntimePrerequisiteState.READY: None,
        RuntimePrerequisiteState.MISSING: FileFailureReason.RUNTIME_MISSING,
        RuntimePrerequisiteState.SHIM: FileFailureReason.RUNTIME_SHIM,
        RuntimePrerequisiteState.UNUSABLE: FileFailureReason.RUNTIME_UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES: FileFailureReason.RUNTIME_MISSING_MODULES,
        RuntimePrerequisiteState.UNKNOWN: FileFailureReason.RUNTIME_UNKNOWN,
    }[observation.state]


def _carrier_reason(failure: Failure) -> FileFailureReason:
    return {
        Failure.DEADLINE: FileFailureReason.DEADLINE,
        Failure.DISPATCH: FileFailureReason.CARRIER_DISPATCH,
        Failure.OBSERVATION: FileFailureReason.CARRIER_OBSERVATION,
        Failure.INVALID_RESPONSE: FileFailureReason.INVALID_RESPONSE,
        Failure.INPUT: FileFailureReason.CARRIER_INPUT,
        Failure.OUTPUT: FileFailureReason.CARRIER_OUTPUT,
        Failure.OUTPUT_LIMIT: FileFailureReason.CARRIER_OUTPUT_LIMIT,
    }[failure]


def _require_candidate(
    candidate: _Candidate,
    phase: FileOperationPhase,
    *,
    entity_kind: str,
    entity_name: str,
) -> None:
    reason = _candidate_failure_reason(candidate)
    if reason is not None:
        _raise_reason(phase, reason, entity_kind=entity_kind, entity_name=entity_name)


def _candidate_failure_reason(candidate: _Candidate) -> FileFailureReason | None:
    runtime_reason = _runtime_reason(candidate.runtime_prerequisite)
    if runtime_reason not in {None, FileFailureReason.RUNTIME_UNKNOWN}:
        return runtime_reason
    if candidate.carrier_failure is not None:
        return _carrier_reason(candidate.carrier_failure)
    completion = candidate.carrier_completion
    if candidate.dispatch is not Dispatch.SENT or completion is None or completion.code != 0:
        return (
            FileFailureReason.CARRIER_DISPATCH
            if candidate.dispatch is Dispatch.NOT_SENT
            else FileFailureReason.TERMINATION
        )
    return runtime_reason


def _mutation_failure_reason(
    candidate: _Candidate,
    *,
    helper_reason: FileFailureReason | None,
    deadline_exceeded: bool,
    fallback: FileFailureReason,
) -> FileFailureReason:
    """Keep a specific helper cause ahead of later host/custody state."""
    runtime_reason = _runtime_reason(candidate.runtime_prerequisite)
    if runtime_reason not in {None, FileFailureReason.RUNTIME_UNKNOWN}:
        return runtime_reason
    if helper_reason is not None:
        return helper_reason
    candidate_reason = _candidate_failure_reason(candidate)
    if candidate_reason is not None:
        return candidate_reason
    if deadline_exceeded:
        return FileFailureReason.DEADLINE
    return fallback


def _require_owned_read[T: _Candidate](
    outcome: OwnedFileOutcome[T],
    phase: FileOperationPhase,
    *,
    entity_kind: str,
    entity_name: str,
) -> T:
    candidate = outcome.result
    if candidate is not None:
        _require_candidate(candidate, phase, entity_kind=entity_kind, entity_name=entity_name)
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        _raise_reason(phase, FileFailureReason.COORDINATION, entity_kind=entity_kind, entity_name=entity_name)
    if outcome.requires_owner_retention:
        _raise_reason(phase, FileFailureReason.CLEANUP, entity_kind=entity_kind, entity_name=entity_name)
    if candidate is None:
        reason = FileFailureReason.DEADLINE if outcome.deadline_exceeded else FileFailureReason.INCOMPLETE_RESPONSE
        _raise_reason(phase, reason, entity_kind=entity_kind, entity_name=entity_name)
    return candidate


def _raise_late_read_deadline[T](
    outcome: OwnedFileOutcome[T],
    phase: FileOperationPhase,
    *,
    entity_kind: str,
    entity_name: str,
) -> None:
    if outcome.deadline_exceeded:
        _raise_reason(phase, FileFailureReason.DEADLINE, entity_kind=entity_kind, entity_name=entity_name)


def _read_failure_reason(failure: FileReadFailure) -> FileFailureReason:
    return {
        FileReadFailure.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FileReadFailure.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FileReadFailure.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
        FileReadFailure.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
        FileReadFailure.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
        FileReadFailure.ROOT_REFUSED: FileFailureReason.REFUSED,
        FileReadFailure.UNSUPPORTED_OBJECT: FileFailureReason.UNSUPPORTED,
        FileReadFailure.LIMIT: FileFailureReason.LIMIT,
        FileReadFailure.CONFLICT: FileFailureReason.CONFLICT,
        FileReadFailure.DEADLINE: FileFailureReason.DEADLINE,
        FileReadFailure.IO: FileFailureReason.IO,
    }[failure]


def _ownership_failure_reason(failure: FileOwnershipFailure) -> FileFailureReason:
    return {
        FileOwnershipFailure.MISSING_OWNER: FileFailureReason.MISSING_OWNER,
        FileOwnershipFailure.MISSING_GROUP: FileFailureReason.MISSING_GROUP,
        FileOwnershipFailure.LOOKUP: FileFailureReason.IO,
        FileOwnershipFailure.RUNTIME: FileFailureReason.RUNTIME_UNUSABLE,
        FileOwnershipFailure.OVERSIZED: FileFailureReason.INVALID_RESPONSE,
        FileOwnershipFailure.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
    }[failure]


def reduce_file_stat(
    outcome: OwnedFileOutcome[FileObjectCandidateResult],
    *,
    entity_kind: str,
    entity_name: str,
) -> FileMetadata | None:
    """Reduce one metadata-only object observation."""
    candidate = _require_owned_read(
        outcome, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
    )
    observation = candidate.observation
    if observation is None:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
        )
        _raise_reason(
            FileOperationPhase.OBSERVATION,
            FileFailureReason.INCOMPLETE_RESPONSE,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    if observation.state is FileObjectObservationState.ABSENT:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
        )
        return None
    if observation.state is FileObjectObservationState.PRESENT and observation.revision is not None:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
        )
        return _metadata(observation.revision)
    if observation.failure is None:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
        )
    _raise_object_observation(
        observation, FileOperationPhase.OBSERVATION, entity_kind=entity_kind, entity_name=entity_name
    )


def reduce_file_inventory(
    outcome: OwnedFileOutcome[FileInventoryCandidateResult],
    *,
    entity_kind: str,
    entity_name: str,
) -> tuple[DirectoryEntry, ...]:
    """Reduce one complete bounded inventory; absence is a failure."""
    candidate = _require_owned_read(
        outcome, FileOperationPhase.INVENTORY, entity_kind=entity_kind, entity_name=entity_name
    )
    observation = candidate.observation
    if observation is None:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.INVENTORY, entity_kind=entity_kind, entity_name=entity_name
        )
        _raise_reason(
            FileOperationPhase.INVENTORY,
            FileFailureReason.INCOMPLETE_RESPONSE,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    if observation.state is FileInventoryObservationState.PRESENT and observation.entries is not None:
        _raise_late_read_deadline(
            outcome, FileOperationPhase.INVENTORY, entity_kind=entity_kind, entity_name=entity_name
        )
        return tuple(_entry(entry) for entry in observation.entries)
    if observation.state is FileInventoryObservationState.NOT_FOUND:
        _raise_reason(
            FileOperationPhase.INVENTORY, FileFailureReason.NOT_FOUND, entity_kind=entity_kind, entity_name=entity_name
        )
    if observation.failure is not None:
        reason = {
            FileInventoryFailureCode.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileInventoryFailureCode.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileInventoryFailureCode.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
            FileInventoryFailureCode.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
            FileInventoryFailureCode.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
            FileInventoryFailureCode.ROOT_REFUSED: FileFailureReason.REFUSED,
            FileInventoryFailureCode.TARGET_REFUSED: FileFailureReason.REFUSED,
            FileInventoryFailureCode.UNSUPPORTED_OBJECT: FileFailureReason.UNSUPPORTED,
            FileInventoryFailureCode.LIMIT: FileFailureReason.LIMIT,
            FileInventoryFailureCode.CONFLICT: FileFailureReason.CONFLICT,
            FileInventoryFailureCode.DEADLINE: FileFailureReason.DEADLINE,
            FileInventoryFailureCode.IO: FileFailureReason.IO,
        }[observation.failure]
        _raise_reason(FileOperationPhase.INVENTORY, reason, entity_kind=entity_kind, entity_name=entity_name)
    _raise_late_read_deadline(outcome, FileOperationPhase.INVENTORY, entity_kind=entity_kind, entity_name=entity_name)
    reason = (
        FileFailureReason.INCOMPLETE_RESPONSE
        if observation.state is FileInventoryObservationState.INCOMPLETE
        else FileFailureReason.INVALID_RESPONSE
    )
    _raise_reason(FileOperationPhase.INVENTORY, reason, entity_kind=entity_kind, entity_name=entity_name)


def reduce_file_remove(
    outcome: OwnedFileOutcome[FileObjectCandidateResult],
    *,
    entity_kind: str,
    entity_name: str,
) -> MutationResult:
    """Reduce one conditional removal and preserve mutation uncertainty."""
    candidate = outcome.result
    dispatch = None if candidate is None else candidate.dispatch
    observation = None if candidate is None else candidate.observation
    if observation is not None and observation.state in {
        FileObjectObservationState.CHANGED,
        FileObjectObservationState.UNCHANGED,
    }:
        assert candidate is not None
        change = Change.CHANGED if observation.state is FileObjectObservationState.CHANGED else Change.UNCHANGED
        candidate_reason = _candidate_failure_reason(candidate)
        if candidate_reason is not None:
            _raise_reason(
                FileOperationPhase.REMOVAL,
                candidate_reason,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        if outcome.deadline_exceeded:
            _raise_reason(
                FileOperationPhase.REMOVAL,
                FileFailureReason.DEADLINE,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        if outcome.pending_remote_effects or outcome.coordination_uncertain or outcome.requires_owner_retention:
            _raise_reason(
                FileOperationPhase.CLEANUP,
                FileFailureReason.COORDINATION,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        return MutationResult(change, None)
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        failure = None if observation is None else observation.failure
        effect = (
            Change.CHANGED
            if observation is not None and observation.state is FileObjectObservationState.CHANGED
            else None
        )
        reason = (
            FileFailureReason.DEADLINE
            if candidate is None and outcome.deadline_exceeded
            else FileFailureReason.COORDINATION
            if candidate is None
            else _mutation_failure_reason(
                candidate,
                helper_reason=None if failure is None else _object_failure_reason(failure),
                deadline_exceeded=outcome.deadline_exceeded,
                fallback=FileFailureReason.COORDINATION,
            )
        )
        _raise_uncertain(
            _object_phase(failure),
            reason,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=dispatch,
            effect=effect,
        )
    if candidate is None:
        reason = FileFailureReason.DEADLINE if outcome.deadline_exceeded else FileFailureReason.INCOMPLETE_RESPONSE
        _raise_reason(FileOperationPhase.REMOVAL, reason, entity_kind=entity_kind, entity_name=entity_name)
    observation = candidate.observation
    if observation is not None and observation.state is FileObjectObservationState.UNCERTAIN:
        failure = observation.failure
        reason = _mutation_failure_reason(
            candidate,
            helper_reason=None if failure is None else _object_failure_reason(failure),
            deadline_exceeded=outcome.deadline_exceeded,
            fallback=FileFailureReason.INVALID_RESPONSE,
        )
        _raise_uncertain(
            _object_phase(failure),
            reason,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=candidate.dispatch,
        )
    _require_candidate(candidate, FileOperationPhase.REMOVAL, entity_kind=entity_kind, entity_name=entity_name)
    if observation is not None and observation.state is FileObjectObservationState.REFUSED:
        _raise_object_observation(
            observation, FileOperationPhase.REMOVAL, entity_kind=entity_kind, entity_name=entity_name
        )
    if outcome.deadline_exceeded:
        _raise_uncertain(
            FileOperationPhase.REMOVAL,
            FileFailureReason.DEADLINE,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=candidate.dispatch,
        )
    if observation is None:
        _raise_uncertain(
            FileOperationPhase.REMOVAL,
            FileFailureReason.INCOMPLETE_RESPONSE,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=candidate.dispatch,
        )
    _raise_object_observation(observation, FileOperationPhase.REMOVAL, entity_kind=entity_kind, entity_name=entity_name)


def _raise_object_observation(
    observation: FileObjectObservation, phase: FileOperationPhase, *, entity_kind: str, entity_name: str
) -> Never:
    failure = observation.failure
    if failure is not None:
        _raise_reason(
            _object_phase(failure, fallback=phase),
            _object_failure_reason(failure),
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    state = observation.state
    reason = (
        FileFailureReason.INCOMPLETE_RESPONSE
        if state is FileObjectObservationState.INCOMPLETE
        else FileFailureReason.INVALID_RESPONSE
    )
    _raise_reason(phase, reason, entity_kind=entity_kind, entity_name=entity_name)


def _object_phase(
    failure: FileObjectFailureControl | None,
    *,
    fallback: FileOperationPhase = FileOperationPhase.REMOVAL,
) -> FileOperationPhase:
    private = None if failure is None else failure.phase
    if private is None:
        return fallback
    return {
        FileObjectPhase.OBSERVATION: FileOperationPhase.OBSERVATION,
        FileObjectPhase.CONDITION: FileOperationPhase.CONDITION,
        FileObjectPhase.REMOVAL: FileOperationPhase.REMOVAL,
    }[private]


def _object_failure_reason(failure: FileObjectFailureControl) -> FileFailureReason:
    code = failure.code
    if code is not FileObjectFailureCode.OBJECT:
        return {
            FileObjectFailureCode.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileObjectFailureCode.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileObjectFailureCode.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
            FileObjectFailureCode.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
            FileObjectFailureCode.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
            FileObjectFailureCode.ROOT_REFUSED: FileFailureReason.REFUSED,
            FileObjectFailureCode.PARENT_REFUSED: FileFailureReason.REFUSED,
        }[code]
    if failure.kind is None:
        return FileFailureReason.INVALID_RESPONSE
    return {
        FileObjectFailureKind.UNSUPPORTED: FileFailureReason.UNSUPPORTED,
        FileObjectFailureKind.CONFLICT: FileFailureReason.CONFLICT,
        FileObjectFailureKind.DEADLINE: FileFailureReason.DEADLINE,
        FileObjectFailureKind.IO: FileFailureReason.IO,
        FileObjectFailureKind.UNCERTAIN: FileFailureReason.IO,
    }[failure.kind]


def reduce_file_metadata(
    outcome: OwnedFileOutcome[FileMetadataCandidateResult],
    *,
    entity_kind: str,
    entity_name: str,
) -> MutationResult:
    """Reduce ownership lookup plus one metadata mutation attempt."""
    if outcome.result is None:
        return _reduce_ownership_stop(outcome, entity_kind=entity_kind, entity_name=entity_name)
    candidate = outcome.result
    observation = candidate.observation
    failure = None if observation is None else observation.failure
    dispatch = candidate.dispatch
    if (
        observation is not None
        and observation.state in {FileMetadataObservationState.CHANGED, FileMetadataObservationState.UNCHANGED}
        and observation.revision is not None
    ):
        change = Change.CHANGED if observation.state is FileMetadataObservationState.CHANGED else Change.UNCHANGED
        candidate_reason = _candidate_failure_reason(candidate)
        if candidate_reason is not None:
            _raise_reason(
                FileOperationPhase.METADATA,
                candidate_reason,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        if outcome.deadline_exceeded:
            _raise_reason(
                FileOperationPhase.METADATA,
                FileFailureReason.DEADLINE,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        if outcome.pending_remote_effects or outcome.coordination_uncertain or outcome.requires_owner_retention:
            _raise_reason(
                FileOperationPhase.CLEANUP,
                FileFailureReason.COORDINATION,
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=change,
            )
        return MutationResult(change, _revision_from_file_revision(observation.revision))
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        completed = () if failure is None else failure.completed_steps
        attempted = None if failure is None else failure.attempted_step
        effect = (
            Change.CHANGED
            if observation is not None and observation.state is FileMetadataObservationState.CHANGED
            else None
        )
        reason = _mutation_failure_reason(
            candidate,
            helper_reason=None if failure is None else _metadata_failure_reason(failure),
            deadline_exceeded=outcome.deadline_exceeded,
            fallback=FileFailureReason.COORDINATION,
        )
        _raise_uncertain(
            _metadata_phase(failure),
            reason,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=dispatch,
            completed_steps=completed,
            attempted_step=attempted,
            effect=effect,
        )
    if observation is not None and observation.state is FileMetadataObservationState.UNCERTAIN:
        completed = () if failure is None else failure.completed_steps
        attempted = None if failure is None else failure.attempted_step
        reason = _mutation_failure_reason(
            candidate,
            helper_reason=None if failure is None else _metadata_failure_reason(failure),
            deadline_exceeded=outcome.deadline_exceeded,
            fallback=FileFailureReason.IO,
        )
        _raise_uncertain(
            _metadata_phase(failure),
            reason,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=dispatch,
            completed_steps=completed,
            attempted_step=attempted,
        )
    if observation is not None and observation.state is FileMetadataObservationState.PARTIAL and failure is not None:
        reason = _mutation_failure_reason(
            candidate,
            helper_reason=_metadata_failure_reason(failure),
            deadline_exceeded=outcome.deadline_exceeded,
            fallback=FileFailureReason.IO,
        )
        raise PartialMutationError(
            "file metadata mutation stopped after completing some target steps",
            completed_steps=failure.completed_steps,
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint="Inspect the target before retrying the operation.",
            details=_details(_metadata_phase(failure), reason),
        ) from None
    _require_candidate(candidate, FileOperationPhase.METADATA, entity_kind=entity_kind, entity_name=entity_name)
    if observation is not None and observation.state is FileMetadataObservationState.REFUSED and failure is not None:
        _raise_reason(
            _metadata_phase(failure),
            _metadata_failure_reason(failure),
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    if outcome.deadline_exceeded and dispatch is not Dispatch.NOT_SENT:
        _raise_uncertain(
            FileOperationPhase.METADATA,
            FileFailureReason.DEADLINE,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=dispatch,
        )
    if outcome.deadline_exceeded:
        _raise_reason(
            FileOperationPhase.METADATA, FileFailureReason.DEADLINE, entity_kind=entity_kind, entity_name=entity_name
        )
    if observation is None:
        _raise_uncertain(
            FileOperationPhase.METADATA,
            FileFailureReason.INCOMPLETE_RESPONSE,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=dispatch,
        )
    if failure is not None:
        _raise_reason(
            _metadata_phase(failure),
            _metadata_failure_reason(failure),
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    reason = (
        FileFailureReason.INCOMPLETE_RESPONSE
        if observation.state is FileMetadataObservationState.INCOMPLETE
        else FileFailureReason.INVALID_RESPONSE
    )
    _raise_reason(FileOperationPhase.METADATA, reason, entity_kind=entity_kind, entity_name=entity_name)


def _reduce_ownership_stop(
    outcome: OwnedFileOutcome[FileMetadataCandidateResult], *, entity_kind: str, entity_name: str
) -> Never:
    result = outcome.ownership_result
    if result is None:
        reason = FileFailureReason.DEADLINE if outcome.deadline_exceeded else FileFailureReason.INCOMPLETE_RESPONSE
        _raise_reason(FileOperationPhase.OWNERSHIP_LOOKUP, reason, entity_kind=entity_kind, entity_name=entity_name)
    _require_candidate(result, FileOperationPhase.OWNERSHIP_LOOKUP, entity_kind=entity_kind, entity_name=entity_name)
    observation = result.observation
    if (
        observation is not None
        and observation.state is AccountObservationState.REFUSED
        and observation.failure is not None
    ):
        reason = _ownership_failure_reason(observation.failure)
        _raise_reason(FileOperationPhase.OWNERSHIP_LOOKUP, reason, entity_kind=entity_kind, entity_name=entity_name)
    if outcome.deadline_exceeded:
        _raise_reason(
            FileOperationPhase.OWNERSHIP_LOOKUP,
            FileFailureReason.DEADLINE,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    if outcome.pending_remote_effects or outcome.coordination_uncertain or outcome.requires_owner_retention:
        _raise_reason(
            FileOperationPhase.OWNERSHIP_LOOKUP,
            FileFailureReason.COORDINATION,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    reason = (
        FileFailureReason.INCOMPLETE_RESPONSE
        if observation is None or observation.state is AccountObservationState.INCOMPLETE
        else FileFailureReason.INVALID_RESPONSE
    )
    _raise_reason(FileOperationPhase.OWNERSHIP_LOOKUP, reason, entity_kind=entity_kind, entity_name=entity_name)


def _metadata_phase(failure: FileMetadataFailureControl | None) -> FileOperationPhase:
    private = None if failure is None else failure.phase
    return {
        None: FileOperationPhase.METADATA,
        MetadataPhase.OBSERVATION: FileOperationPhase.OBSERVATION,
        MetadataPhase.CREATION: FileOperationPhase.CREATION,
        MetadataPhase.OWNERSHIP: FileOperationPhase.METADATA,
        MetadataPhase.MODE: FileOperationPhase.METADATA,
        MetadataPhase.VERIFICATION: FileOperationPhase.METADATA,
    }[private]


def _metadata_failure_reason(failure: FileMetadataFailureControl) -> FileFailureReason:
    code = failure.code
    if code is not FileMetadataFailureCode.METADATA:
        return {
            FileMetadataFailureCode.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileMetadataFailureCode.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
            FileMetadataFailureCode.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
            FileMetadataFailureCode.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
            FileMetadataFailureCode.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
            FileMetadataFailureCode.ROOT_REFUSED: FileFailureReason.REFUSED,
            FileMetadataFailureCode.PARENT_REFUSED: FileFailureReason.REFUSED,
        }[code]
    if failure.kind is None:
        return FileFailureReason.INVALID_RESPONSE
    return {
        MetadataFailureKind.UNSUPPORTED: FileFailureReason.UNSUPPORTED,
        MetadataFailureKind.CONFLICT: FileFailureReason.CONFLICT,
        MetadataFailureKind.DEADLINE: FileFailureReason.DEADLINE,
        MetadataFailureKind.METADATA: FileFailureReason.REFUSED,
        MetadataFailureKind.IO: FileFailureReason.IO,
    }[failure.kind]
