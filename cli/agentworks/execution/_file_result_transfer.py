"""Reduce private transfer and structured-file outcomes to public results."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ._file_download import FileDownloadFailure, FileDownloadOutcome, FileDownloadStatus
from ._file_json import FileJsonChange, FileJsonFailure, FileJsonOutcome, FileJsonStatus
from ._file_publication import PublicationFailureKind, PublicationPhase
from ._file_publication_protocol import FilePublicationFailureCode
from ._file_result import (
    _metadata,
    _object_failure_reason,
    _raise_reason,
    _raise_uncertain,
    _read_failure_reason,
    _runtime_reason,
)
from ._file_spool import SpoolSnapshotFailureKind
from ._file_upload import FileUploadFailure, FileUploadOutcome, FileUploadStatus
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
    if outcome.deadline_exceeded:
        reason = FileFailureReason.DEADLINE
    elif outcome.coordination_uncertain or outcome.pending_remote_effects:
        reason = FileFailureReason.COORDINATION
    elif outcome.requires_owner_retention or outcome.cleanup_debt is not None or outcome.snapshot_ownership_uncertain:
        reason = FileFailureReason.CLEANUP
    else:
        reason = {
            None: FileFailureReason.INCOMPLETE_RESPONSE,
            FileDownloadFailure.DEADLINE: FileFailureReason.DEADLINE,
            FileDownloadFailure.SINK: FileFailureReason.SINK,
            FileDownloadFailure.SINK_CONTRACT: FileFailureReason.SINK_CONTRACT,
            FileDownloadFailure.SNAPSHOT: _snapshot_reason(outcome),
            FileDownloadFailure.CLEANUP: FileFailureReason.CLEANUP,
            FileDownloadFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
            FileDownloadFailure.RUNTIME_PREREQUISITE: _optional_runtime_reason(outcome.runtime_prerequisite),
            FileDownloadFailure.TERMINATION: FileFailureReason.TERMINATION,
            FileDownloadFailure.INTEGRITY: FileFailureReason.INTEGRITY,
        }[outcome.failure]
    _raise_reason(FileOperationPhase.TRANSFER, reason, entity_kind=entity_kind, entity_name=entity_name)


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
    if outcome.status is FileUploadStatus.COMPLETE and _upload_success(outcome):
        assert outcome.revision is not None
        return MutationResult(Change.CHANGED, _revision_from_file_revision(outcome.revision))
    if outcome.publication_uncertain or outcome.publication_ownership_uncertain:
        _raise_uncertain(
            FileOperationPhase.PUBLICATION,
            _upload_reason(outcome),
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=None,
        )
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        # Upload does not retain which concrete exchange owns this uncertainty.
        # Only a publication-specific fact can make it target uncertainty.
        if outcome.publication_confirmed:
            _raise_reason(
                FileOperationPhase.CLEANUP,
                _upload_reason(outcome),
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=Change.CHANGED,
            )
        _raise_reason(
            FileOperationPhase.TRANSFER,
            FileFailureReason.COORDINATION,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    effect = Change.CHANGED if outcome.publication_confirmed else None
    phase = FileOperationPhase.CLEANUP if effect is not None else _upload_phase(outcome)
    _raise_reason(phase, _upload_reason(outcome), entity_kind=entity_kind, entity_name=entity_name, effect=effect)


def _upload_success(outcome: FileUploadOutcome) -> bool:
    return (
        outcome.publication_confirmed
        and outcome.revision is not None
        and outcome.revision.digest is not None
        and outcome.revision.stat.size == outcome.binding.expected_size
        and outcome.bytes_consumed == outcome.binding.expected_size
        and outcome.staged_bytes == outcome.binding.expected_size
        and outcome.content_digest == outcome.revision.digest
        and not outcome.deadline_exceeded
        and outcome.failure is None
        and outcome.scratch_cleanup_debt is None
        and outcome.publication_cleanup_debt is None
        and not outcome.stage_ownership_uncertain
        and not outcome.publication_ownership_uncertain
        and not outcome.pending_remote_effects
        and not outcome.coordination_uncertain
        and not outcome.requires_owner_retention
    )


def _upload_reason(outcome: FileUploadOutcome) -> FileFailureReason:
    if outcome.deadline_exceeded:
        return FileFailureReason.DEADLINE
    failure = outcome.failure
    if failure is FileUploadFailure.PUBLICATION and outcome.publication_failure is not None:
        publication = outcome.publication_failure
        if publication.code is FilePublicationFailureCode.PUBLICATION and publication.publication_kind is not None:
            return {
                PublicationFailureKind.UNSUPPORTED: FileFailureReason.UNSUPPORTED,
                PublicationFailureKind.CONFLICT: FileFailureReason.CONFLICT,
                PublicationFailureKind.WRITE_AUTHORITY: FileFailureReason.REFUSED,
                PublicationFailureKind.METADATA: FileFailureReason.REFUSED,
                PublicationFailureKind.DEADLINE: FileFailureReason.DEADLINE,
                PublicationFailureKind.IO: FileFailureReason.IO,
                PublicationFailureKind.UNCERTAIN: FileFailureReason.IO,
            }[publication.publication_kind]
    return {
        None: FileFailureReason.INCOMPLETE_RESPONSE,
        FileUploadFailure.DEADLINE: FileFailureReason.DEADLINE,
        FileUploadFailure.SOURCE: FileFailureReason.SOURCE,
        FileUploadFailure.SOURCE_CONTRACT: FileFailureReason.SOURCE_CONTRACT,
        FileUploadFailure.STAGE: FileFailureReason.REFUSED,
        FileUploadFailure.PUBLICATION: FileFailureReason.REFUSED,
        FileUploadFailure.CLEANUP: FileFailureReason.CLEANUP,
        FileUploadFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
        FileUploadFailure.RUNTIME_PREREQUISITE: _optional_runtime_reason(outcome.runtime_prerequisite),
        FileUploadFailure.TERMINATION: FileFailureReason.TERMINATION,
    }[failure]


def _upload_phase(outcome: FileUploadOutcome) -> FileOperationPhase:
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
    if outcome.status is FileJsonStatus.COMPLETE and _json_success(outcome):
        change = Change.CHANGED if outcome.change is FileJsonChange.CHANGED else Change.UNCHANGED
        revision = None if outcome.revision is None else _revision_from_file_revision(outcome.revision)
        return MutationResult(change, revision)
    upload = outcome.upload_outcome
    if upload is not None and (upload.publication_uncertain or upload.publication_ownership_uncertain):
        _raise_uncertain(
            FileOperationPhase.PUBLICATION,
            _upload_reason(upload),
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=None,
        )
    if outcome.pending_remote_effects or outcome.coordination_uncertain:
        if upload is not None and upload.publication_confirmed:
            _raise_reason(
                FileOperationPhase.CLEANUP,
                _json_reason(outcome),
                entity_kind=entity_kind,
                entity_name=entity_name,
                effect=Change.CHANGED,
            )
        _raise_reason(
            FileOperationPhase.TRANSFER,
            FileFailureReason.COORDINATION,
            entity_kind=entity_kind,
            entity_name=entity_name,
        )
    effect = Change.CHANGED if upload is not None and upload.publication_confirmed else None
    _raise_reason(
        _json_phase(outcome), _json_reason(outcome), entity_kind=entity_kind, entity_name=entity_name, effect=effect
    )


def _json_success(outcome: FileJsonOutcome) -> bool:
    return (
        outcome.change is not None
        and (outcome.change is FileJsonChange.UNCHANGED or outcome.revision is not None)
        and outcome.failure is None
        and not outcome.deadline_exceeded
        and not outcome.pending_remote_effects
        and not outcome.coordination_uncertain
        and not outcome.requires_owner_retention
    )


def _json_reason(outcome: FileJsonOutcome) -> FileFailureReason:
    if outcome.deadline_exceeded:
        return FileFailureReason.DEADLINE
    if outcome.failure is FileJsonFailure.READ and outcome.read_failure is not None:
        return _read_failure_reason(outcome.read_failure)
    if outcome.failure is FileJsonFailure.OBJECT and outcome.object_failure is not None:
        return _object_failure_reason(outcome.object_failure)
    if outcome.failure is FileJsonFailure.PUBLICATION and outcome.upload_outcome is not None:
        return _upload_reason(outcome.upload_outcome)
    return {
        None: FileFailureReason.INCOMPLETE_RESPONSE,
        FileJsonFailure.DEADLINE: FileFailureReason.DEADLINE,
        FileJsonFailure.ABSENT: FileFailureReason.NOT_FOUND,
        FileJsonFailure.OBJECT: FileFailureReason.REFUSED,
        FileJsonFailure.READ: FileFailureReason.REFUSED,
        FileJsonFailure.EXISTING_VALIDATION: FileFailureReason.EXISTING_CONTENT,
        FileJsonFailure.TRANSFORM: FileFailureReason.TRANSFORM,
        FileJsonFailure.PUBLICATION: FileFailureReason.REFUSED,
        FileJsonFailure.CONFLICT: FileFailureReason.CONFLICT,
        FileJsonFailure.CLEANUP: FileFailureReason.CLEANUP,
        FileJsonFailure.OBSERVATION: FileFailureReason.INVALID_RESPONSE,
        FileJsonFailure.RUNTIME_PREREQUISITE: _optional_runtime_reason(outcome.runtime_prerequisite),
        FileJsonFailure.TERMINATION: FileFailureReason.TERMINATION,
    }[outcome.failure]


def _json_phase(outcome: FileJsonOutcome) -> FileOperationPhase:
    if outcome.failure in {FileJsonFailure.EXISTING_VALIDATION, FileJsonFailure.TRANSFORM}:
        return FileOperationPhase.TRANSFORM
    if outcome.failure is FileJsonFailure.CLEANUP:
        return FileOperationPhase.CLEANUP
    if outcome.failure is FileJsonFailure.PUBLICATION:
        return FileOperationPhase.PUBLICATION
    return FileOperationPhase.OBSERVATION
