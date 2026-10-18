"""Bounded content mechanics for Darwin held-file replacement.

The owner admits the stage and target, verifies the complete stage, rechecks the
condition and marks possible mutation before calling ``copy_in_place``. Any
escaping write, truncate, flush or deadline failure retains that uncertainty.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time


class StageContentMismatch(Exception):
    """A complete stage no longer matches its admitted content contract."""


def verify_stage_content(descriptor: int, size: int, digest: bytes, expires_at: float | None) -> None:
    """Re-read the complete held stage before any public destination mutation."""
    actual = hashlib.sha256()
    offset = 0
    while offset < size:
        _check_deadline(expires_at)
        block = os.pread(descriptor, min(64 * 1024, size - offset), offset)
        if not block:
            raise StageContentMismatch
        actual.update(block)
        offset += len(block)
    _check_deadline(expires_at)
    if os.pread(descriptor, 1, size) or not hmac.compare_digest(actual.digest(), digest):
        raise StageContentMismatch


def copy_in_place(stage_fd: int, target_fd: int, size: int, expires_at: float | None) -> None:
    """Copy verified bytes without truncating the held target during open."""
    os.lseek(target_fd, 0, os.SEEK_SET)
    offset = 0
    while offset < size:
        _check_deadline(expires_at)
        block = os.pread(stage_fd, min(64 * 1024, size - offset), offset)
        if not block:
            raise OSError("Publication stage became short")
        copied = 0
        while copied < len(block):
            _check_deadline(expires_at)
            written = os.write(target_fd, memoryview(block)[copied:])
            if not 0 < written <= len(block) - copied:
                raise OSError("Publication write made no progress")
            copied += written
        offset += len(block)
    _check_deadline(expires_at)
    os.ftruncate(target_fd, size)
    os.fsync(target_fd)
    _check_deadline(expires_at)


def _check_deadline(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise TimeoutError("Publication deadline expired")
