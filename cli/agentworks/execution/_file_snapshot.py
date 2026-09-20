"""Cooperative POSIX regular-file snapshots from a caller-owned root descriptor.

Linux lookup uses ``openat2`` confinement. Other POSIX platforms retain a
separate component walk and do not claim equivalent mount-boundary enforcement.
This does not provide malicious same-user protection or hard cancellation of a
blocked filesystem call.
Callers own their root and writer lock.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from ._file_paths import (
    ConfinedOpenError,
    ConfinedOpenFailure,
    open_linux_confined,
)
from ._file_stat import FileRevision, FileStat

_READ_CHUNK_BYTES = 64 * 1024
_DESCRIPTOR_OPERATIONS_AVAILABLE = (
    os.name == "posix"
    and bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


class SnapshotFailureKind(Enum):
    """Closed operational failures that reveal no filesystem input."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    LIMIT = "limit"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class SnapshotReadError(Exception):
    """A snapshot refusal carrying only its closed failure kind."""

    def __init__(self, kind: SnapshotFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


@dataclass(frozen=True)
class FileSnapshot:
    """Immutable bytes and the observations that bind them to one object."""

    data: bytes = field(repr=False)
    revision: FileRevision

    @property
    def stat(self) -> FileStat:
        return self.revision.stat

    @property
    def digest(self) -> bytes:
        digest = self.revision.digest
        assert digest is not None
        return digest


@dataclass(frozen=True, slots=True, repr=False)
class _HeldRegularFile:
    """One confined source descriptor and its stable initial observation."""

    descriptor: int
    parent_fd: int
    leaf_name: str
    stat: FileStat

    def read(self, maximum: int, *, expires_at: float | None) -> bytes:
        _check_deadline(expires_at)
        result = _read(self.descriptor, maximum)
        _check_deadline(expires_at)
        return result

    def verify(self, *, expires_at: float | None) -> None:
        _check_deadline(expires_at)
        held = _snapshot_stat(_fstat(self.descriptor))
        named = _path_snapshot_stat(self.parent_fd, self.leaf_name)
        if held != self.stat or named != self.stat:
            raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
        _check_deadline(expires_at)


def read_snapshot(
    trusted_root_fd: int,
    relative_path: str,
    max_bytes: int,
    *,
    expires_at: float | None = None,
) -> FileSnapshot | None:
    """Read one bounded regular file relative to a borrowed directory descriptor.

    ``None`` means absence during initial traversal. Later observed changes are
    conflicts. The byte bound does not prevent a filesystem read from blocking.
    """
    components = _validate_inputs(relative_path, max_bytes, expires_at)
    result = _read_regular_file(
        trusted_root_fd,
        components,
        max_bytes=max_bytes,
        include_digest=True,
        collect_content=True,
        expires_at=expires_at,
    )
    assert result is None or isinstance(result, FileSnapshot)
    return result


def read_revision(
    trusted_root_fd: int,
    relative_path: str,
    *,
    include_digest: bool,
    expires_at: float | None = None,
) -> FileRevision | None:
    """Observe one regular file without retaining its bytes.

    A stat-only observation performs no content read. A digest observation
    hashes bounded chunks and checks the deadline between filesystem calls.
    """
    components = _validate_inputs(relative_path, 1, expires_at)
    if type(include_digest) is not bool:
        raise ValueError("Revision digest selection must be a boolean")
    result = _read_regular_file(
        trusted_root_fd,
        components,
        max_bytes=None,
        include_digest=include_digest,
        collect_content=False,
        expires_at=expires_at,
    )
    assert result is None or isinstance(result, FileRevision)
    return result


def _read_regular_file(
    trusted_root_fd: int,
    components: tuple[str, ...],
    *,
    max_bytes: int | None,
    include_digest: bool,
    collect_content: bool,
    expires_at: float | None,
) -> FileSnapshot | FileRevision | None:
    stat_only = not include_digest and not collect_content
    with _hold_regular_file(
        trusted_root_fd,
        components,
        max_bytes=max_bytes,
        stat_only=stat_only,
        expires_at=expires_at,
    ) as held:
        if held is None:
            return None

        content = bytearray() if collect_content else None
        content_hash = hashlib.sha256() if include_digest else None
        observed_size = 0
        if include_digest or collect_content:
            while True:
                request_size = _READ_CHUNK_BYTES
                if max_bytes is not None:
                    request_size = min(request_size, max_bytes - observed_size + 1)
                chunk = held.read(request_size, expires_at=expires_at)
                if not chunk:
                    break
                observed_size += len(chunk)
                if max_bytes is not None and observed_size > max_bytes:
                    raise SnapshotReadError(SnapshotFailureKind.LIMIT)
                if content is not None:
                    content.extend(chunk)
                if content_hash is not None:
                    content_hash.update(chunk)

        held.verify(expires_at=expires_at)
        if include_digest and observed_size != held.stat.size:
            raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
        revision = FileRevision(held.stat, None if content_hash is None else content_hash.digest())
        if content is None:
            return revision
        return FileSnapshot(data=bytes(content), revision=revision)


@contextmanager
def _hold_regular_file(
    trusted_root_fd: int,
    components: tuple[str, ...],
    *,
    max_bytes: int | None,
    stat_only: bool,
    expires_at: float | None,
) -> Iterator[_HeldRegularFile | None]:
    """Borrow one stable confined regular leaf while owning descendant descriptors."""
    if not _DESCRIPTOR_OPERATIONS_AVAILABLE:
        raise SnapshotReadError(SnapshotFailureKind.IO)

    _check_deadline(expires_at)
    root_stat = _fstat(trusted_root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
    root_device = root_stat.st_dev
    parent_fd = trusted_root_fd
    parent_owned = False
    leaf_fd: int | None = None
    try:
        for component in components[:-1]:
            _check_deadline(expires_at)
            child_fd = _open_at(parent_fd, component, directory=True)
            if child_fd is None:
                yield None
                return
            previous_fd = parent_fd if parent_owned else None
            parent_fd = child_fd
            parent_owned = True
            if previous_fd is not None:
                _close(previous_fd)
            child_stat = _fstat(parent_fd)
            if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_dev != root_device:
                raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)

        leaf_name = components[-1]
        expected = _path_snapshot_stat(parent_fd, leaf_name)
        if expected is None:
            yield None
            return
        if not _is_supported_leaf(expected, root_device):
            raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
        if max_bytes is not None and expected.size > max_bytes:
            raise SnapshotReadError(SnapshotFailureKind.LIMIT)
        if stat_only:
            leaf_fd = _open_at(parent_fd, leaf_name, directory=False, stat_only=True)
        else:
            leaf_fd = _open_at(parent_fd, leaf_name, directory=False)
        if leaf_fd is None:
            raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
        before = _snapshot_stat(_fstat(leaf_fd))
        if not _is_supported_leaf(before, root_device):
            raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
        if before != expected or _path_snapshot_stat(parent_fd, leaf_name) != before:
            raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
        yield _HeldRegularFile(leaf_fd, parent_fd, leaf_name, before)
    finally:
        if leaf_fd is not None:
            _close(leaf_fd)
        if parent_owned:
            _close(parent_fd)


def _validate_inputs(relative_path: str, max_bytes: int, expires_at: float | None) -> tuple[str, ...]:
    if type(relative_path) is not str or not relative_path or "\x00" in relative_path:
        raise ValueError("Snapshot path must be a nonempty relative string")
    if relative_path.startswith("/"):
        raise ValueError("Snapshot path must be relative")
    components = tuple(relative_path.split("/"))
    if any(component in ("", ".", "..") for component in components):
        raise ValueError("Snapshot path contains an invalid component")
    try:
        os.fsencode(relative_path)
    except UnicodeEncodeError:
        encoding_failed = True
    else:
        encoding_failed = False
    if encoding_failed:
        raise ValueError("Snapshot path is not encodable")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Snapshot byte bound must be a positive integer")
    if expires_at is not None and (type(expires_at) not in {int, float} or expires_at != expires_at):
        raise ValueError("Snapshot deadline must be a monotonic timestamp or None")
    return components


def _open_at(parent_fd: int, name: str, *, directory: bool, stat_only: bool = False) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    elif stat_only and sys.platform == "linux":
        path_only = getattr(os, "O_PATH", 0)
        if not path_only:
            raise SnapshotReadError(SnapshotFailureKind.IO)
        flags &= ~os.O_RDONLY
        flags |= path_only
    else:
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)

    if sys.platform == "linux":
        try:
            return open_linux_confined(parent_fd, name, flags)
        except ConfinedOpenError as error:
            if error.kind is ConfinedOpenFailure.CONFLICT:
                raise SnapshotReadError(SnapshotFailureKind.CONFLICT) from None
            if error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT:
                raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT) from None
            raise SnapshotReadError(SnapshotFailureKind.IO) from None

    error_number: int | None = None
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if error_number == errno.ENOENT:
        return None
    if error_number in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV, errno.EXDEV}:
        raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
    raise SnapshotReadError(SnapshotFailureKind.IO)


def _check_deadline(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise SnapshotReadError(SnapshotFailureKind.DEADLINE)


def _is_supported_leaf(observed: FileStat, root_device: int) -> bool:
    return (
        stat.S_ISREG(observed.mode)
        and (observed.link_count, observed.device) == (1, root_device)
        and observed.size >= 0
    )


def _snapshot_stat(observed: os.stat_result) -> FileStat:
    return FileStat(
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
        link_count=observed.st_nlink,
        uid=observed.st_uid,
        gid=observed.st_gid,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _path_snapshot_stat(parent_fd: int, leaf_name: str) -> FileStat | None:
    error_number: int | None = None
    try:
        observed = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
    else:
        return _snapshot_stat(observed)
    if error_number == errno.ENOENT:
        return None
    raise SnapshotReadError(SnapshotFailureKind.IO)


def _fstat(descriptor: int) -> os.stat_result:
    failed = False
    try:
        observed = os.fstat(descriptor)
    except OSError:
        failed = True
        observed = None
    if failed or observed is None:
        raise SnapshotReadError(SnapshotFailureKind.IO)
    return observed


def _read(descriptor: int, size: int) -> bytes:
    failed = False
    try:
        content = os.read(descriptor, size)
    except OSError:
        failed = True
        content = b""
    if failed:
        raise SnapshotReadError(SnapshotFailureKind.IO)
    return content


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
