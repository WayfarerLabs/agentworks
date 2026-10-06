"""Private host publication of one core-owned, verified download."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.execution._file_download import FileDownloadControlFact, FileDownloadOutcome, FileDownloadStatus
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution._local_download_publication_macos import MacOSLocalDownloadPublication
from agentworks.execution._local_download_publication_windows import WindowsLocalDownloadPublication
from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupError,
    LocalDownloadStage,
    LocalDownloadUnsupportedError,
)
from agentworks.execution.files import Create, Replace

_CREATE = Create()

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._file_operation import FileOperation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline


@dataclass(frozen=True, slots=True, repr=False)
class FileLocalDownloadOutcome:
    """Remote evidence and independent workstation publication facts."""

    download: FileDownloadOutcome | None
    published: bool = False
    publication_uncertain: bool = False
    cleanup_uncertain: bool = False
    cleanup_failed: bool = False
    deadline_exceeded: bool = False
    unfinished_stage: LocalDownloadStage | None = field(default=None, repr=False)


class FileLocalDownloadControlFact(Exception):
    """Safe local and remote facts attached to exceptional control flow."""

    def __init__(self, outcome: FileLocalDownloadOutcome) -> None:
        self.outcome = outcome
        super().__init__("private local download stopped with retained operation state")


def _ready_to_publish(download: FileDownloadOutcome, deadline: Deadline) -> bool:
    return download.status is FileDownloadStatus.COMPLETE and not download.deadline_exceeded and not deadline.expired


def _publisher_for_host(destination: Path, condition: Create | Replace) -> LocalDownloadStage:
    if sys.platform == "linux":
        return LocalDownloadPublication(destination, condition=condition)
    if sys.platform == "darwin":
        return MacOSLocalDownloadPublication(destination, condition=condition)
    if sys.platform == "win32":
        return WindowsLocalDownloadPublication(destination, condition=condition)
    raise LocalDownloadUnsupportedError(f"Local download publication is unsupported on {sys.platform}")


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
        return FileLocalDownloadOutcome(None, deadline_exceeded=True)

    writer: LocalDownloadStage | None = None
    download: FileDownloadOutcome | None = None
    control: BaseException | None = None
    cleanup_failed = False
    construction_cleanup_uncertain = False
    construction_cleanup_failed = False
    try:
        writer = _publisher_for_host(destination, condition)
        if not deadline.expired:
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
                revision = download.source_revision
                assert revision is not None and revision.digest is not None
                writer.commit(
                    verified_complete=True,
                    size=download.accepted_bytes,
                    sha256=revision.digest.hex(),
                    deadline=deadline,
                )
    except BaseException as exc:
        control = exc
        if isinstance(exc.__cause__, FileDownloadControlFact):
            download = exc.__cause__.outcome
        if writer is None:
            construction_cleanup_uncertain = bool(getattr(exc, "cleanup_uncertain", False))
            if isinstance(exc, LocalDownloadCleanupError) and exc.unfinished_stage is not None:
                writer = exc.unfinished_stage
                construction_cleanup_failed = True
                cleanup_failed = True
    finally:
        if writer is not None and not construction_cleanup_failed:
            try:
                writer.abort()
            except BaseException as exc:
                cleanup_failed = True
                if control is None:
                    control = exc

    outcome = FileLocalDownloadOutcome(
        download,
        published=writer.published if writer is not None else False,
        publication_uncertain=writer.publication_uncertain if writer is not None else False,
        cleanup_uncertain=writer.cleanup_uncertain if writer is not None else construction_cleanup_uncertain,
        cleanup_failed=cleanup_failed,
        deadline_exceeded=deadline.expired or (download.deadline_exceeded if download is not None else False),
        unfinished_stage=writer if cleanup_failed else None,
    )
    if control is not None:
        fact = FileLocalDownloadControlFact(outcome)
        fact.__cause__ = control.__cause__
        raise control from fact
    return outcome
