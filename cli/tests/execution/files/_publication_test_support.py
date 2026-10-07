"""Shared setup for publication receipt filesystem tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from agentworks.execution._publication_receipt import (
    PublicationRecordAcquisition,
    PublicationStageOwnership,
    admit_publication_stage,
    record_publication_stage,
)
from agentworks.execution._scratch import (
    ReadyScratchReference,
    begin_scratch,
    verify_scratch,
    write_scratch_chunk,
)
from agentworks.execution._scratch_receipt import ScratchOperation, current_receipt_context


def open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def ready_scratch(parent_fd: int, token: bytes, content: bytes = b"content") -> ReadyScratchReference:
    reference = begin_scratch(
        parent_fd,
        len(content),
        token,
        current_receipt_context(ScratchOperation.STAGE),
    )
    if content:
        write_scratch_chunk(parent_fd, reference, 0, content, hashlib.sha256(content).digest())
    return verify_scratch(parent_fd, reference, hashlib.sha256(content).digest())


def record_stage(
    scratch_parent_fd: int,
    ready: ReadyScratchReference,
    publication_parent_fd: int,
) -> tuple[PublicationStageOwnership, int]:
    admission = admit_publication_stage(scratch_parent_fd, ready, publication_parent_fd)
    stage_fd = os.open(
        admission.stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=publication_parent_fd,
    )
    try:
        ownership = record_publication_stage(
            admission,
            publication_parent_fd,
            stage_fd,
            PublicationRecordAcquisition(),
        )
    finally:
        close_error = admission.close()
        assert close_error is None
    return ownership, stage_fd
