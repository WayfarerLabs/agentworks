"""Admission for the fixed Linux scratch parent."""

from __future__ import annotations

import os
import stat
import sys
import time
from contextlib import suppress
from enum import Enum

from ._file_paths import ConfinedOpenError, ConfinedOpenFailure, open_linux_root

_LINUX_SCRATCH_ROOT = "/tmp"
_EXPECTED_OWNER_UID = 0
_EXPECTED_MODE = 0o1777


class ScratchRootFailureKind(Enum):
    """Closed scratch-root failures that reveal no filesystem details."""

    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    UNSAFE = "unsafe"
    DEADLINE = "deadline"
    IO = "io"


class ScratchRootError(Exception):
    """A fixed scratch-root refusal carrying only its closed failure kind."""

    def __init__(self, kind: ScratchRootFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


def open_scratch_root(*, expires_at: float | None) -> int:
    """Open and validate the existing fixed Linux scratch parent.

    The caller owns the returned descriptor. Selection creates and repairs
    nothing, and its cooperative deadline does not cancel filesystem calls.
    """
    if sys.platform != "linux":
        raise ScratchRootError(ScratchRootFailureKind.UNSUPPORTED)
    _raise_if_expired(expires_at)

    descriptor: int | None = None
    failure: ScratchRootFailureKind | None = None
    try:
        try:
            descriptor = open_linux_root(_LINUX_SCRATCH_ROOT)
        except ConfinedOpenError as error:
            failure = _open_failure(error.kind)
        else:
            if descriptor is None:
                failure = ScratchRootFailureKind.MISSING
            else:
                try:
                    observed = os.fstat(descriptor)
                except OSError:
                    failure = ScratchRootFailureKind.IO
                else:
                    if not _safe_scratch_root(observed):
                        failure = ScratchRootFailureKind.UNSAFE
    except BaseException:
        _close(descriptor)
        raise

    if failure is not None:
        _close(descriptor)
        _raise_if_expired(expires_at)
        raise ScratchRootError(failure) from None

    assert descriptor is not None
    try:
        _raise_if_expired(expires_at)
    except BaseException:
        _close(descriptor)
        raise
    return descriptor


def _open_failure(kind: ConfinedOpenFailure) -> ScratchRootFailureKind:
    if kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT:
        return ScratchRootFailureKind.UNSUPPORTED
    return ScratchRootFailureKind.IO


def _safe_scratch_root(observed: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == _EXPECTED_OWNER_UID
        and stat.S_IMODE(observed.st_mode) == _EXPECTED_MODE
    )


def _raise_if_expired(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise ScratchRootError(ScratchRootFailureKind.DEADLINE)


def _close(descriptor: int | None) -> None:
    if descriptor is not None:
        with suppress(OSError):
            os.close(descriptor)
