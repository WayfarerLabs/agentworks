"""Private Linux filesystem-object observation and conditional removal.

The caller owns the trusted parent descriptor and cooperating-writer lock,
and supplies an already-validated single leaf name.
Linux ``openat2`` confines the exact leaf without requiring read authority.
External writers can still race the final check and removal syscall; this
module makes no adversarial compare-and-swap claim.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import NoReturn

from ._file_paths import ConfinedOpenError, ConfinedOpenFailure, open_linux_confined
from ._file_snapshot import (
    SnapshotFailureKind,
    SnapshotReadError,
    _snapshot_stat,
    read_revision,
)
from ._file_stat import FileRevision


class FileKind(StrEnum):
    """Closed object kinds supported by the private file layer."""

    REGULAR = "regular"
    DIRECTORY = "directory"
    SOCKET = "socket"


class FileObjectFailureKind(Enum):
    """Closed failures that reveal no filesystem names or metadata."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"
    UNCERTAIN = "uncertain"


class FileObjectPhase(Enum):
    """The fixed object operation in which work stopped."""

    OBSERVATION = "observation"
    CONDITION = "condition"
    REMOVAL = "removal"


class FileObjectError(Exception):
    """A closed object-operation failure."""

    def __init__(self, kind: FileObjectFailureKind, phase: FileObjectPhase) -> None:
        self.kind = kind
        self.phase = phase
        super().__init__(kind.value, phase.value)


@dataclass(frozen=True, repr=False)
class _ObservedObject:
    descriptor: int
    kind: FileKind
    revision: FileRevision


def stat_file_object(
    parent_fd: int,
    leaf_name: str,
    *,
    expires_at: float | None,
) -> FileRevision | None:
    """Return metadata-only revision evidence for one supported exact leaf."""
    _require_linux(FileObjectPhase.OBSERVATION)
    _raise_if_expired(expires_at, FileObjectPhase.OBSERVATION)
    observed = _open_observed(parent_fd, leaf_name, FileObjectPhase.OBSERVATION, expires_at)
    if observed is None:
        return None
    try:
        return observed.revision
    finally:
        _close(observed.descriptor)


def revision_kind(revision: FileRevision) -> FileKind:
    """Derive a supported object kind from private revision evidence."""
    return _kind_from_mode(revision.stat.mode, FileObjectPhase.OBSERVATION)


def remove_file_object(
    parent_fd: int,
    leaf_name: str,
    *,
    expected_kind: FileKind,
    expected: FileRevision,
    expires_at: float | None,
) -> bool:
    """Remove the exact matching leaf, or return ``False`` when initially absent."""
    _require_linux(FileObjectPhase.CONDITION)
    _raise_if_expired(expires_at, FileObjectPhase.CONDITION)

    observed = _open_observed(parent_fd, leaf_name, FileObjectPhase.CONDITION, expires_at)
    if observed is None:
        return False
    attempted = False
    removed = False
    failure: FileObjectError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    try:
        try:
            if observed.kind is not expected_kind or observed.revision.stat != expected.stat:
                raise FileObjectError(FileObjectFailureKind.CONFLICT, FileObjectPhase.CONDITION)
            if expected.digest is not None:
                if expected_kind is not FileKind.REGULAR:
                    raise FileObjectError(FileObjectFailureKind.CONFLICT, FileObjectPhase.CONDITION)
                current = _read_digest_revision(parent_fd, leaf_name, expires_at)
                if current != expected:
                    raise FileObjectError(FileObjectFailureKind.CONFLICT, FileObjectPhase.CONDITION)
            _revalidate(parent_fd, leaf_name, observed, expected, expires_at)
            _raise_if_expired(expires_at, FileObjectPhase.REMOVAL)
            attempted = True
            _remove_named(parent_fd, leaf_name, expected_kind)
            removed = True
        except FileObjectError as error:
            failure = error
        except BaseException as error:
            control = error
    finally:
        try:
            _close(observed.descriptor)
        except BaseException as error:
            close_control = error

    if close_control is not None:
        control = close_control
    if control is not None:
        prior = failure
        if attempted:
            prior = FileObjectError(FileObjectFailureKind.UNCERTAIN, FileObjectPhase.REMOVAL)
        _raise_control(control, prior)
    if failure is not None:
        raise failure
    return removed


def _open_observed(
    parent_fd: int,
    leaf_name: str,
    phase: FileObjectPhase,
    expires_at: float | None,
) -> _ObservedObject | None:
    parent = _fstat(parent_fd, phase)
    if not stat.S_ISDIR(parent.st_mode):
        raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, phase)
    _raise_if_expired(expires_at, phase)
    named_before = _stat_named(parent_fd, leaf_name, phase)
    if named_before is None:
        _raise_if_expired(expires_at, phase)
        return None
    kind = _require_supported(named_before, parent.st_dev, phase)
    _raise_if_expired(expires_at, phase)
    descriptor = _open_path(parent_fd, leaf_name, phase)
    if descriptor is None:
        raise FileObjectError(FileObjectFailureKind.CONFLICT, phase)
    try:
        held = _fstat(descriptor, phase)
        held_kind = _require_supported(held, parent.st_dev, phase)
        named_after = _stat_named(parent_fd, leaf_name, phase)
        if (
            held_kind is not kind
            or named_after is None
            or _snapshot_stat(held) != _snapshot_stat(named_before)
            or _snapshot_stat(named_after) != _snapshot_stat(held)
        ):
            raise FileObjectError(FileObjectFailureKind.CONFLICT, phase)
        _raise_if_expired(expires_at, phase)
        return _ObservedObject(descriptor, kind, FileRevision(_snapshot_stat(held)))
    except BaseException:
        _close(descriptor)
        raise


def _revalidate(
    parent_fd: int,
    leaf_name: str,
    observed: _ObservedObject,
    expected: FileRevision,
    expires_at: float | None,
) -> None:
    current = _verify_observed(parent_fd, leaf_name, observed, FileObjectPhase.CONDITION, expires_at)
    if current.stat != expected.stat:
        raise FileObjectError(FileObjectFailureKind.CONFLICT, FileObjectPhase.CONDITION)


def _verify_observed(
    parent_fd: int,
    leaf_name: str,
    observed: _ObservedObject,
    phase: FileObjectPhase,
    expires_at: float | None,
) -> FileRevision:
    """Re-observe one held object and its exact name without reopening it."""
    _raise_if_expired(expires_at, phase)
    held = _fstat(observed.descriptor, phase)
    held_revision = FileRevision(_snapshot_stat(held))
    named = _stat_named(parent_fd, leaf_name, phase)
    if named is None:
        raise FileObjectError(FileObjectFailureKind.CONFLICT, phase)
    if (held.st_dev, held.st_ino) != (observed.revision.stat.device, observed.revision.stat.inode) or (
        named.st_dev,
        named.st_ino,
    ) != (held.st_dev, held.st_ino):
        raise FileObjectError(FileObjectFailureKind.CONFLICT, phase)
    held_kind = _require_supported(held, observed.revision.stat.device, phase)
    named_kind = _require_supported(named, observed.revision.stat.device, phase)
    if held_kind is not observed.kind or named_kind is not observed.kind or _snapshot_stat(named) != held_revision.stat:
        raise FileObjectError(FileObjectFailureKind.CONFLICT, phase)
    _raise_if_expired(expires_at, phase)
    return held_revision


def _read_digest_revision(parent_fd: int, leaf_name: str, expires_at: float | None) -> FileRevision | None:
    try:
        return read_revision(parent_fd, leaf_name, include_digest=True, expires_at=expires_at)
    except SnapshotReadError as error:
        if error.kind is SnapshotFailureKind.UNSUPPORTED_OBJECT:
            kind = FileObjectFailureKind.UNSUPPORTED
        elif error.kind is SnapshotFailureKind.DEADLINE:
            kind = FileObjectFailureKind.DEADLINE
        elif error.kind in {SnapshotFailureKind.CONFLICT, SnapshotFailureKind.LIMIT}:
            kind = FileObjectFailureKind.CONFLICT
        else:
            kind = FileObjectFailureKind.IO
        raise FileObjectError(kind, FileObjectPhase.CONDITION) from None


def _require_supported(observed: os.stat_result, parent_device: int, phase: FileObjectPhase) -> FileKind:
    kind = _kind_from_mode(observed.st_mode, phase)
    if observed.st_dev != parent_device or (kind is FileKind.REGULAR and observed.st_nlink != 1):
        raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, phase)
    return kind


def _kind_from_mode(mode: int, phase: FileObjectPhase) -> FileKind:
    if stat.S_ISREG(mode):
        return FileKind.REGULAR
    if stat.S_ISDIR(mode):
        return FileKind.DIRECTORY
    if stat.S_ISSOCK(mode):
        return FileKind.SOCKET
    raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, phase)


def _open_path(parent_fd: int, leaf_name: str, phase: FileObjectPhase) -> int | None:
    path_only = getattr(os, "O_PATH", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not path_only or not no_follow:
        raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, phase)
    flags = path_only | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        return open_linux_confined(parent_fd, leaf_name, flags)
    except ConfinedOpenError as error:
        if error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT:
            kind = FileObjectFailureKind.UNSUPPORTED
        elif error.kind is ConfinedOpenFailure.CONFLICT:
            kind = FileObjectFailureKind.CONFLICT
        else:
            kind = FileObjectFailureKind.IO
        raise FileObjectError(kind, phase) from None


def _remove_named(parent_fd: int, leaf_name: str, kind: FileKind) -> None:
    error_number: int | None = None
    try:
        if kind is FileKind.DIRECTORY:
            os.rmdir(leaf_name, dir_fd=parent_fd)
        else:
            os.unlink(leaf_name, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if error_number is None:
        return
    if error_number in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
        raise FileObjectError(FileObjectFailureKind.CONFLICT, FileObjectPhase.REMOVAL)
    if error_number in {errno.EISDIR, errno.ENOTDIR, errno.ELOOP, errno.ENXIO, errno.ENODEV}:
        raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, FileObjectPhase.REMOVAL)
    raise FileObjectError(FileObjectFailureKind.UNCERTAIN, FileObjectPhase.REMOVAL)


def _stat_named(parent_fd: int, leaf_name: str, phase: FileObjectPhase) -> os.stat_result | None:
    error_number: int | None = None
    try:
        observed = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
        observed = None
    if error_number == errno.ENOENT:
        return None
    if error_number is not None or observed is None:
        raise FileObjectError(FileObjectFailureKind.IO, phase)
    return observed


def _fstat(descriptor: int, phase: FileObjectPhase) -> os.stat_result:
    observed: os.stat_result | None = None
    with suppress(OSError):
        observed = os.fstat(descriptor)
    if observed is None:
        raise FileObjectError(FileObjectFailureKind.IO, phase)
    return observed


def _require_linux(phase: FileObjectPhase) -> None:
    if sys.platform != "linux":
        raise FileObjectError(FileObjectFailureKind.UNSUPPORTED, phase)


def _raise_if_expired(expires_at: float | None, phase: FileObjectPhase) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise FileObjectError(FileObjectFailureKind.DEADLINE, phase)


def _raise_control(control: BaseException, prior: FileObjectError | None) -> NoReturn:
    if prior is None:
        raise control
    raise control from prior


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
