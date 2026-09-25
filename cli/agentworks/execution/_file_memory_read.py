"""Private bounded in-memory reads over owned snapshot downloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.execution._file_download import FileDownloadOutcome, FileDownloadStatus

if TYPE_CHECKING:
    from agentworks.execution._file_operation import FileOperation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline


@dataclass(frozen=True, slots=True, repr=False)
class FileMemoryReadOutcome:
    """Original download evidence and complete in-memory data when available."""

    download: FileDownloadOutcome
    data: bytes | None = field(default=None, repr=False)


class _MemorySink:
    def __init__(self) -> None:
        self.data = bytearray()

    def try_write(self, data: memoryview) -> int:
        self.data.extend(data)
        return len(data)

    def clear(self) -> None:
        self.data.clear()


def read_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    operation: FileOperation,
) -> FileMemoryReadOutcome:
    """Materialize one complete verified download within the caller's bound."""
    sink = _MemorySink()
    try:
        download = operation.download(
            carrier,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            sink=sink,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        )
        data = bytes(sink.data) if download.status is FileDownloadStatus.COMPLETE else None
        return FileMemoryReadOutcome(download, data)
    finally:
        sink.clear()
