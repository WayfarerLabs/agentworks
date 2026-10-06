"""Private Linux publication of one core-owned, verified download."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.execution._file_download import FileDownloadControlFact, FileDownloadOutcome, FileDownloadStatus
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution.files import Create, Replace

_CREATE = Create()

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._file_operation import FileOperation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline


class LocalDownloadFailure(StrEnum):
    """Local work that prevented an ordinary published result."""

    DEADLINE = "deadline"
    STAGING = "staging"
    REMOTE_NOT_READY = "remote_not_ready"
    TRANSFER = "transfer"
    PUBLICATION = "publication"
    CLEANUP = "cleanup"


@dataclass(frozen=True, slots=True, repr=False)
class FileLocalDownloadOutcome:
    """Remote evidence and independent workstation publication facts."""

    download: FileDownloadOutcome | None
    published: bool = False
    publication_uncertain: bool = False
    cleanup_uncertain: bool = False
    cleanup_failed: bool = False
    deadline_exceeded: bool = False
    local_failure: LocalDownloadFailure | None = None
    unfinished_stage: LocalDownloadPublication | None = field(default=None, repr=False)


class FileLocalDownloadControlFact(Exception):
    """Safe local and remote facts attached to exceptional control flow."""

    def __init__(self, outcome: FileLocalDownloadOutcome) -> None:
        self.outcome = outcome
        super().__init__("private local download stopped with retained operation state")


def _ready_to_publish(download: FileDownloadOutcome, deadline: Deadline) -> bool:
    return (
        download.status is FileDownloadStatus.COMPLETE
        and download.stream_verified
        and download.source_revision is not None
        and download.source_revision.digest is not None
        and download.accepted_bytes == download.source_revision.stat.size
        and download.cleanup_debt is None
        and not download.snapshot_ownership_uncertain
        and not download.pending_remote_effects
        and not download.coordination_uncertain
        and not download.requires_owner_retention
        and not download.deadline_exceeded
        and not deadline.expired
    )


def download_to_local_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    destination: Path,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    operation: FileOperation,
    condition: Create | Replace = _CREATE,
) -> FileLocalDownloadOutcome:
    """Stage one download and publish only after remote cleanup and verification.

    The operation owns the single remote borrow. The returned unfinished_stage,
    when present, retains local cleanup custody for an explicit retry. Escaping
    exceptions carry a FileLocalDownloadControlFact as their cause.
    """
    if deadline.expired:
        return FileLocalDownloadOutcome(None, deadline_exceeded=True, local_failure=LocalDownloadFailure.DEADLINE)

    writer: LocalDownloadPublication | None = None
    download: FileDownloadOutcome | None = None
    control: BaseException | None = None
    failure: LocalDownloadFailure | None = None
    cleanup_failed = False
    construction_cleanup_uncertain = False
    phase = LocalDownloadFailure.STAGING
    try:
        writer = LocalDownloadPublication(destination, condition=condition)
        if deadline.expired:
            failure = LocalDownloadFailure.DEADLINE
        else:
            phase = LocalDownloadFailure.TRANSFER
            download = operation.download(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                sink=writer,
                max_bytes=max_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
            )
            if _ready_to_publish(download, deadline):
                phase = LocalDownloadFailure.PUBLICATION
                revision = download.source_revision
                assert revision is not None and revision.digest is not None
                writer.commit(
                    verified_complete=True,
                    size=download.accepted_bytes,
                    sha256=revision.digest.hex(),
                    deadline=deadline,
                )
            elif download.status is FileDownloadStatus.COMPLETE and (download.deadline_exceeded or deadline.expired):
                failure = LocalDownloadFailure.DEADLINE
            elif download.status is FileDownloadStatus.COMPLETE:
                failure = LocalDownloadFailure.REMOTE_NOT_READY
    except BaseException as exc:
        control = exc
        if isinstance(exc.__cause__, FileDownloadControlFact):
            download = exc.__cause__.outcome
        if writer is None:
            construction_cleanup_uncertain = bool(getattr(exc, "cleanup_uncertain", False))
        if writer is not None and writer.published:
            failure = LocalDownloadFailure.CLEANUP
        elif isinstance(exc, TimeoutError) and deadline.expired:
            failure = LocalDownloadFailure.DEADLINE
        else:
            failure = phase
    finally:
        if writer is not None:
            try:
                writer.abort()
            except BaseException as exc:
                cleanup_failed = True
                if control is None:
                    control = exc
                    failure = LocalDownloadFailure.CLEANUP

    outcome = FileLocalDownloadOutcome(
        download,
        published=writer.published if writer is not None else False,
        publication_uncertain=writer.publication_uncertain if writer is not None else False,
        cleanup_uncertain=writer.cleanup_uncertain if writer is not None else construction_cleanup_uncertain,
        cleanup_failed=cleanup_failed,
        deadline_exceeded=deadline.expired or (download.deadline_exceeded if download is not None else False),
        local_failure=failure or (LocalDownloadFailure.DEADLINE if deadline.expired else None),
        unfinished_stage=writer if cleanup_failed else None,
    )
    if control is not None:
        fact = FileLocalDownloadControlFact(outcome)
        fact.__cause__ = control.__cause__
        raise control from fact
    return outcome
