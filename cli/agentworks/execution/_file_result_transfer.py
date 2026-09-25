"""Reduce private transfer and structured-file outcomes to public results."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ._file_download import (
    FileDownloadFailure,
    FileDownloadFailurePhase,
    FileDownloadOutcome,
    FileDownloadStatus,
)
from ._file_json import FileJsonChange, FileJsonFailure, FileJsonOutcome, FileJsonStatus
from ._file_publication import PublicationFailureKind, PublicationPhase
from ._file_publication_protocol import FilePublicationFailureCode, FilePublicationFailureControl
from ._file_result import (
    _carrier_reason,
    _metadata,
    _mutation_failure_reason,
    _object_failure_reason,
    _ownership_failure_reason,
    _raise_reason,
    _raise_uncertain,
    _read_failure_reason,
    _runtime_reason,
)
from ._file_spool import SpoolSnapshotFailureKind
from ._file_stage_protocol import FileStageFailureCode, FileStageFailureControl
from ._file_upload import FileUploadFailure, FileUploadFailurePhase, FileUploadOutcome, FileUploadStatus
from ._scratch import ScratchFailureKind
from .carrier import Dispatch
from .files import (
    Change,
    FileFailureReason,
    FileMetadata,
    FileOperationPhase,
    MutationResult,
    ReadResult,
    _revision_from_file_revision,
)

if TYPE_CHECKING:
    from ._file_memory_read import FileMemoryReadOutcome
    from ._runtime_prerequisite import RuntimePrerequisiteObservation


def reduce_file_download(
    outcome: FileDownloadOutcome,
    *,
    entity_kind: str,
    entity_name: str,
) -> FileMetadata | None:
    """Reduce remote snapshot/download facts to source metadata only."""
    if outcome.status is FileDownloadStatus.COMPLETE:
        if outcome.deadline_exceeded:
            _raise_download_failure(outcome, entity_kind=entity_kind, entity_name=entity_name)
        assert outcome.source_revision is not None
        return _metadata(outcome.source_revision)
    if outcome.status is FileDownloadStatus.ABSENT:
        if outcome.deadline_exceeded:
            _raise_download_failure(outcome, entity_kind=entity_kind, entity_name=entity_name)
        return None
    _raise_download_failure(outcome, entity_kind=entity_kind, entity_name=entity_name)


def reduce_file_memory_read(
    outcome: FileMemoryReadOutcome,
    *,
    entity_kind: str,
    entity_name: str,
) -> ReadResult | None:
    """Reduce a captured in-memory snapshot only when data and metadata agree."""
    metadata = reduce_file_download(outcome.download, entity_kind=entity_kind, entity_name=entity_name)
    if metadata is None:
        if outcome.data is not None:
            _raise_reason(
                FileOperationPhase.TRANSFER,
                FileFailureReason.INVALID_RESPONSE,
                entity_kind=entity_kind,
                entity_name=entity_name,
            )
        return None
    if outcome.data is None:
        _raise_reason(
            FileOperationPhase.TRANSFER,
            FileFailureReason.INCOMPLETE_RESPONSE,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    return ReadResult(outcome.data, metadata)


def _raise_download_failure(outcome: FileDownloadOutcome, *, entity_kind: str, entity_name: str) -> Never:
    _raise_reason(
        _download_phase(outcome),
        _download_reason(outcome),
        entity_kind=entity_kind,
        entity_name=entity_name,
    )


def _download_phase(outcome: FileDownloadOutcome) -> FileOperationPhase:
    if outcome.failure_phase is not None:
        return {
            FileDownloadFailurePhase.SNAPSHOT_BEGIN: FileOperationPhase.OBSERVATION,
            FileDownloadFailurePhase.SNAPSHOT_CHUNK: FileOperationPhase.TRANSFER,
            FileDownloadFailurePhase.SNAPSHOT_RECONCILE: FileOperationPhase.CLEANUP,
            FileDownloadFailurePhase.SNAPSHOT_CLEANUP: FileOperationPhase.CLEANUP,
        }[outcome.failure_phase]
    if outcome.failure is FileDownloadFailure.CLEANUP:
        return FileOperationPhase.CLEANUP
    return FileOperationPhase.TRANSFER


def _download_reason(outcome: FileDownloadOutcome) -> FileFailureReason:
    failure = outcome.failure
    if failure is not None:
        if failure is FileDownloadFailure.DEADLINE:
            return FileFailureReason.DEADLINE
        if failure is FileDownloadFailure.RUNTIME_PREREQUISITE:
            return _optional_runtime_reason(outcome.runtime_prerequisite)
        if failure is FileDownloadFailure.SNAPSHOT:
            return _snapshot_reason(outcome)
        if outcome.carrier_failure is not None:
            return _carrier_reason(outcome.carrier_failure)
        if failure is FileDownloadFailure.OBSERVATION and outcome.failure_dispatch is Dispatch.NOT_SENT:
            return FileFailureReason.CARRIER_DISPATCH
        return {
            FileDownloadFailure.SINK: FileFailureReason.SINK,
            FileDownloadFailure.SINK_CONTRACT: FileFailureReason.SINK_CONTRACT,
            FileDownloadFailure.CLEANUP: FileFailureReason.CLEANUP,
            FileDownloadFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
            FileDownloadFailure.TERMINATION: FileFailureReason.TERMINATION,
            FileDownloadFailure.INTEGRITY: FileFailureReason.INTEGRITY,
        }[failure]
    if outcome.deadline_exceeded:
        return FileFailureReason.DEADLINE
    if outcome.coordination_uncertain or outcome.pending_remote_effects:
        return FileFailureReason.COORDINATION
    if outcome.requires_owner_retention or outcome.cleanup_debt is not None or outcome.snapshot_ownership_uncertain:
        return FileFailureReason.CLEANUP
    return FileFailureReason.INCOMPLETE_RESPONSE


def _snapshot_reason(outcome: FileDownloadOutcome) -> FileFailureReason:
    failure = outcome.snapshot_failure
    if failure is None:
        return FileFailureReason.INVALID_RESPONSE
    if failure.spool_kind is not None:
        return {
            SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT: FileFailureReason.UNSUPPORTED,
            SpoolSnapshotFailureKind.LIMIT: FileFailureReason.LIMIT,
            SpoolSnapshotFailureKind.CONFLICT: FileFailureReason.CONFLICT,
            SpoolSnapshotFailureKind.INTEGRITY: FileFailureReason.INTEGRITY,
            SpoolSnapshotFailureKind.DEADLINE: FileFailureReason.DEADLINE,
            SpoolSnapshotFailureKind.IO: FileFailureReason.IO,
        }[failure.spool_kind]
    return FileFailureReason.REFUSED


def _optional_runtime_reason(observation: RuntimePrerequisiteObservation | None) -> FileFailureReason:
    if observation is None:
        return FileFailureReason.RUNTIME_UNKNOWN
    return _runtime_reason(observation) or FileFailureReason.RUNTIME_UNKNOWN


def reduce_file_upload(
    outcome: FileUploadOutcome,
    *,
    entity_kind: str,
    entity_name: str,
) -> MutationResult:
    """Reduce one upload without exposing private staging or cleanup state."""
    if outcome.status is FileUploadStatus.COMPLETE and not outcome.deadline_exceeded:
        assert outcome.revision is not None
        return MutationResult(Change.CHANGED, _revision_from_file_revision(outcome.revision))
    if outcome.publication_uncertain or outcome.publication_ownership_uncertain:
        phase = _upload_phase(outcome)
        _raise_uncertain(
            phase,
            _upload_reason(outcome),
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=outcome.failure_dispatch if phase is FileOperationPhase.PUBLICATION else None,
        )
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        _raise_reason(
            _upload_phase(outcome),
            _upload_reason(outcome),
            entity_kind=entity_kind,
            entity_name=entity_name,
            effect=Change.CHANGED if outcome.publication_confirmed else None,
        )
    effect = Change.CHANGED if outcome.publication_confirmed else None
    _raise_reason(
        _upload_phase(outcome),
        _upload_reason(outcome),
        entity_kind=entity_kind,
        entity_name=entity_name,
        effect=effect,
    )


def _upload_reason(outcome: FileUploadOutcome) -> FileFailureReason:
    failure = outcome.failure
    if failure is FileUploadFailure.OWNERSHIP:
        return _upload_ownership_reason(outcome)
    if failure is FileUploadFailure.DEADLINE:
        return FileFailureReason.DEADLINE
    if failure is FileUploadFailure.STAGE and outcome.stage_failure is not None:
        return _stage_reason(outcome.stage_failure)
    if failure is FileUploadFailure.PUBLICATION and outcome.publication_failure is not None:
        return _publication_reason(outcome.publication_failure)
    if failure is FileUploadFailure.RUNTIME_PREREQUISITE:
        runtime_reason = _optional_runtime_reason(outcome.runtime_prerequisite)
        if runtime_reason is not FileFailureReason.RUNTIME_UNKNOWN:
            return runtime_reason
    if outcome.carrier_failure is not None:
        return _carrier_reason(outcome.carrier_failure)
    if failure is FileUploadFailure.OBSERVATION and outcome.failure_dispatch is Dispatch.NOT_SENT:
        return FileFailureReason.CARRIER_DISPATCH
    if failure is not None:
        return {
            FileUploadFailure.SOURCE: FileFailureReason.SOURCE,
            FileUploadFailure.SOURCE_CONTRACT: FileFailureReason.SOURCE_CONTRACT,
            FileUploadFailure.STAGE: FileFailureReason.REFUSED,
            FileUploadFailure.PUBLICATION: FileFailureReason.REFUSED,
            FileUploadFailure.CLEANUP: FileFailureReason.CLEANUP,
            FileUploadFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
            FileUploadFailure.RUNTIME_PREREQUISITE: FileFailureReason.RUNTIME_UNKNOWN,
            FileUploadFailure.TERMINATION: FileFailureReason.TERMINATION,
        }[failure]
    if outcome.deadline_exceeded:
        return FileFailureReason.DEADLINE
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        return FileFailureReason.COORDINATION
    if outcome.requires_owner_retention:
        return FileFailureReason.CLEANUP
    return FileFailureReason.INCOMPLETE_RESPONSE


def _upload_ownership_reason(outcome: FileUploadOutcome) -> FileFailureReason:
    result = outcome.ownership_result
    if result is None:
        return FileFailureReason.DEADLINE if outcome.deadline_exceeded else FileFailureReason.INCOMPLETE_RESPONSE
    observation = result.observation
    helper_reason = (
        _ownership_failure_reason(observation.failure)
        if observation is not None and observation.failure is not None
        else None
    )
    return _mutation_failure_reason(
        result,
        helper_reason=helper_reason,
        deadline_exceeded=outcome.deadline_exceeded,
        fallback=FileFailureReason.INVALID_RESPONSE,
    )


def _scratch_reason(kind: ScratchFailureKind | None) -> FileFailureReason:
    if kind is None:
        return FileFailureReason.INVALID_RESPONSE
    return {
        ScratchFailureKind.UNSUPPORTED: FileFailureReason.UNSUPPORTED,
        ScratchFailureKind.CONFLICT: FileFailureReason.CONFLICT,
        ScratchFailureKind.LIMIT: FileFailureReason.LIMIT,
        ScratchFailureKind.INTEGRITY: FileFailureReason.INTEGRITY,
        ScratchFailureKind.DEADLINE: FileFailureReason.DEADLINE,
        ScratchFailureKind.IO: FileFailureReason.IO,
    }[kind]


def _stage_reason(failure: FileStageFailureControl) -> FileFailureReason:
    code = failure.code
    if code is FileStageFailureCode.SCRATCH:
        return _scratch_reason(failure.kind)
    return {
        FileStageFailureCode.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FileStageFailureCode.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FileStageFailureCode.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
        FileStageFailureCode.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
        FileStageFailureCode.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
        FileStageFailureCode.DEADLINE: FileFailureReason.DEADLINE,
        FileStageFailureCode.ROOT_REFUSED: FileFailureReason.REFUSED,
        FileStageFailureCode.PARENT_REFUSED: FileFailureReason.REFUSED,
    }[code]


def _publication_reason(failure: FilePublicationFailureControl) -> FileFailureReason:
    code = failure.code
    if code is FilePublicationFailureCode.SCRATCH:
        return _scratch_reason(failure.scratch_kind)
    if code is FilePublicationFailureCode.PUBLICATION and failure.publication_kind is not None:
        return {
            PublicationFailureKind.UNSUPPORTED: FileFailureReason.UNSUPPORTED,
            PublicationFailureKind.CONFLICT: FileFailureReason.CONFLICT,
            PublicationFailureKind.WRITE_AUTHORITY: FileFailureReason.REFUSED,
            PublicationFailureKind.METADATA: FileFailureReason.REFUSED,
            PublicationFailureKind.DEADLINE: FileFailureReason.DEADLINE,
            PublicationFailureKind.IO: FileFailureReason.IO,
            PublicationFailureKind.UNCERTAIN: FileFailureReason.IO,
        }[failure.publication_kind]
    return {
        FilePublicationFailureCode.INVALID_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FilePublicationFailureCode.OVERSIZED_REQUEST: FileFailureReason.INVALID_RESPONSE,
        FilePublicationFailureCode.NONCE_MISMATCH: FileFailureReason.INVALID_RESPONSE,
        FilePublicationFailureCode.IDENTITY_MISMATCH: FileFailureReason.REFUSED,
        FilePublicationFailureCode.UNSUPPORTED_RUNTIME: FileFailureReason.RUNTIME_UNSUPPORTED_VERSION,
        FilePublicationFailureCode.DEADLINE: FileFailureReason.DEADLINE,
        FilePublicationFailureCode.ROOT_REFUSED: FileFailureReason.REFUSED,
        FilePublicationFailureCode.PARENT_REFUSED: FileFailureReason.REFUSED,
        FilePublicationFailureCode.RECEIPT: FileFailureReason.INTEGRITY,
        FilePublicationFailureCode.PUBLICATION: FileFailureReason.INVALID_RESPONSE,
    }[code]


def _upload_phase(outcome: FileUploadOutcome) -> FileOperationPhase:
    if outcome.failure_phase is not None:
        return {
            FileUploadFailurePhase.OWNERSHIP: FileOperationPhase.OWNERSHIP_LOOKUP,
            FileUploadFailurePhase.STAGE_BEGIN: FileOperationPhase.TRANSFER,
            FileUploadFailurePhase.STAGE_CHUNK: FileOperationPhase.TRANSFER,
            FileUploadFailurePhase.PUBLICATION: FileOperationPhase.PUBLICATION,
            FileUploadFailurePhase.STAGE_CLEANUP: FileOperationPhase.CLEANUP,
        }[outcome.failure_phase]
    failure = outcome.publication_failure
    if failure is not None and failure.publication_phase is not None:
        return {
            PublicationPhase.STAGING: FileOperationPhase.TRANSFER,
            PublicationPhase.CONTENT: FileOperationPhase.TRANSFER,
            PublicationPhase.CONDITION: FileOperationPhase.CONDITION,
            PublicationPhase.METADATA: FileOperationPhase.METADATA,
            PublicationPhase.PUBLICATION: FileOperationPhase.PUBLICATION,
            PublicationPhase.CLEANUP: FileOperationPhase.CLEANUP,
        }[failure.publication_phase]
    if outcome.failure is FileUploadFailure.CLEANUP:
        return FileOperationPhase.CLEANUP
    if outcome.failure is FileUploadFailure.PUBLICATION:
        return FileOperationPhase.PUBLICATION
    return FileOperationPhase.TRANSFER


def reduce_file_json(
    outcome: FileJsonOutcome,
    *,
    entity_kind: str,
    entity_name: str,
) -> MutationResult:
    """Reduce a bounded JSON workflow while preserving publication evidence."""
    if outcome.status is FileJsonStatus.COMPLETE and not outcome.deadline_exceeded:
        change = Change.CHANGED if outcome.change is FileJsonChange.CHANGED else Change.UNCHANGED
        revision = None if outcome.revision is None else _revision_from_file_revision(outcome.revision)
        return MutationResult(change, revision)
    if outcome.failure is FileJsonFailure.UPLOAD and outcome.upload_outcome is not None:
        upload = outcome.upload_outcome
        if upload.publication_uncertain or upload.publication_ownership_uncertain:
            phase = _upload_phase(upload)
            _raise_uncertain(
                phase,
                _upload_reason(upload),
                entity_kind=entity_kind,
                entity_name=entity_name,
                dispatch=upload.failure_dispatch if phase is FileOperationPhase.PUBLICATION else None,
            )
        if outcome.pending_remote_effects or outcome.coordination_uncertain:
            _raise_reason(
                _upload_phase(upload),
                _upload_reason(upload),
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=Change.CHANGED if upload.publication_confirmed else None,
            )
        _raise_reason(
            _upload_phase(upload),
            _upload_reason(upload),
            entity_kind=entity_kind,
            entity_name=entity_name,
            effect=Change.CHANGED if upload.publication_confirmed else None,
        )
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        _raise_reason(
            _json_phase(outcome),
            _json_reason(outcome),
            entity_kind=entity_kind,
            entity_name=entity_name,
            effect=Change.CHANGED if outcome.change is FileJsonChange.CHANGED else None,
        )
    _raise_reason(
        _json_phase(outcome),
        _json_reason(outcome),
        entity_kind=entity_kind,
        entity_name=entity_name,
        effect=Change.CHANGED if outcome.change is FileJsonChange.CHANGED else None,
    )


def _json_reason(outcome: FileJsonOutcome) -> FileFailureReason:
    if outcome.failure is FileJsonFailure.UPLOAD:
        return FileFailureReason.INVALID_RESPONSE
    if outcome.failure is FileJsonFailure.READ and outcome.read_failure is not None:
        return _read_failure_reason(outcome.read_failure)
    if outcome.failure is FileJsonFailure.OBJECT and outcome.object_failure is not None:
        return _object_failure_reason(outcome.object_failure)
    if outcome.carrier_failure is not None:
        return _carrier_reason(outcome.carrier_failure)
    if outcome.failure is FileJsonFailure.OBSERVATION and outcome.failure_dispatch is Dispatch.NOT_SENT:
        return FileFailureReason.CARRIER_DISPATCH
    if outcome.failure is FileJsonFailure.DEADLINE:
        return FileFailureReason.DEADLINE
    if outcome.failure is None and outcome.deadline_exceeded:
        return FileFailureReason.DEADLINE
    return {
        None: FileFailureReason.INCOMPLETE_RESPONSE,
        FileJsonFailure.DEADLINE: FileFailureReason.DEADLINE,
        FileJsonFailure.ABSENT: FileFailureReason.NOT_FOUND,
        FileJsonFailure.OBJECT: FileFailureReason.REFUSED,
        FileJsonFailure.READ: FileFailureReason.REFUSED,
        FileJsonFailure.EXISTING_VALIDATION: FileFailureReason.EXISTING_CONTENT,
        FileJsonFailure.TRANSFORM: FileFailureReason.TRANSFORM,
        FileJsonFailure.UPLOAD: FileFailureReason.INVALID_RESPONSE,
        FileJsonFailure.CONFLICT: FileFailureReason.CONFLICT,
        FileJsonFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
        FileJsonFailure.RUNTIME_PREREQUISITE: _optional_runtime_reason(outcome.runtime_prerequisite),
        FileJsonFailure.TERMINATION: FileFailureReason.TERMINATION,
    }[outcome.failure]


def _json_phase(outcome: FileJsonOutcome) -> FileOperationPhase:
    if outcome.failure in {FileJsonFailure.EXISTING_VALIDATION, FileJsonFailure.TRANSFORM}:
        return FileOperationPhase.TRANSFORM
    return FileOperationPhase.OBSERVATION
