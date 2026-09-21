"""Shared test support for the private whole-file upload composition."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind, OperationScope
from agentworks.execution._file_publication import Create, CreateMetadata
from agentworks.execution._file_upload import FileUploadOutcome, upload_file
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.execution._file_publication import Match, Replace
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import ByteSource, Carrier


class BytesSource:
    def __init__(self, data: bytes, *, pieces: tuple[int, ...] = ()) -> None:
        self.data = data
        self.pieces = pieces
        self.offset = 0
        self.calls = 0
        self.limits: list[int] = []
        self.closed = False

    def try_read(self, limit: int) -> bytes:
        self.calls += 1
        self.limits.append(limit)
        if self.offset == len(self.data):
            return b""
        piece = self.pieces[self.calls - 1] if self.calls <= len(self.pieces) else limit
        amount = min(limit, piece, len(self.data) - self.offset)
        result = self.data[self.offset : self.offset + amount]
        self.offset += amount
        return result

    def close(self) -> None:
        self.closed = True


def owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "upload-vm"),
        "file-upload",
    )


def upload(
    operation_owner: OperationOwner,
    root: Path,
    source: ByteSource,
    size: int,
    plan: IdentityPlan,
    *,
    carrier: Carrier | None = None,
    condition: Create | Replace | Match | None = None,
    deadline: Deadline | None = None,
    selected_runtime: RuntimeSelection | None = None,
) -> FileUploadOutcome:
    return upload_file(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        source=source,
        size=size,
        condition=condition or Create(),
        create_metadata=CreateMetadata(os.geteuid(), os.getegid(), 0o640),
        plan=plan,
        deadline=deadline or Deadline.after(30),
        runtime_selection=selected_runtime or runtime_selection(sys.executable),
        owner=operation_owner,
    )
