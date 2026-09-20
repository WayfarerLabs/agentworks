"""Cooperative POSIX regular-file snapshots from a caller-owned root descriptor.

This does not provide ``openat2`` confinement, malicious same-user protection,
or a hard read deadline; ``st_dev`` cannot detect same-filesystem bind mounts.
Callers own their root and writer lock.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum

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
    IO = "io"


class SnapshotReadError(Exception):
    """A snapshot refusal carrying only its closed failure kind."""

    def __init__(self, kind: SnapshotFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


@dataclass(frozen=True)
class SnapshotStat:
    device: int
    inode: int
    mode: int
    link_count: int
    uid: int
    gid: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    """Immutable bytes and the observations that bind them to one object."""

    data: bytes = field(repr=False)
    stat: SnapshotStat
    digest: bytes = field(repr=False)


def read_snapshot(trusted_root_fd: int, relative_path: str, max_bytes: int) -> FileSnapshot | None:
    """Read one bounded regular file relative to a borrowed directory descriptor.

    ``None`` means absence during initial traversal. Later observed changes are
    conflicts. The byte bound does not prevent a filesystem read from blocking.
    """
    components = _validate_inputs(relative_path, max_bytes)
    if not _DESCRIPTOR_OPERATIONS_AVAILABLE:
        raise SnapshotReadError(SnapshotFailureKind.IO)

    root_stat = _fstat(trusted_root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
    root_device = root_stat.st_dev

    parent_fd = trusted_root_fd
    parent_owned = False
    try:
        for component in components[:-1]:
            child_fd = _open_at(parent_fd, component, directory=True)
            if child_fd is None:
                return None
            try:
                child_stat = _fstat(child_fd)
            except SnapshotReadError:
                _close(child_fd)
                raise
            if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_dev != root_device:
                _close(child_fd)
                raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
            if parent_owned:
                _close(parent_fd)
            parent_fd = child_fd
            parent_owned = True

        leaf_fd = _open_at(parent_fd, components[-1], directory=False)
        if leaf_fd is None:
            return None
        try:
            return _snapshot_open_leaf(
                leaf_fd,
                parent_fd=parent_fd,
                leaf_name=components[-1],
                root_device=root_device,
                max_bytes=max_bytes,
            )
        finally:
            _close(leaf_fd)
    finally:
        if parent_owned:
            _close(parent_fd)


def _validate_inputs(relative_path: str, max_bytes: int) -> tuple[str, ...]:
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
    return components


def _open_at(parent_fd: int, name: str, *, directory: bool) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)

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


def _snapshot_open_leaf(
    leaf_fd: int,
    *,
    parent_fd: int,
    leaf_name: str,
    root_device: int,
    max_bytes: int,
) -> FileSnapshot:
    before = _snapshot_stat(_fstat(leaf_fd))
    if not stat.S_ISREG(before.mode) or before.link_count != 1 or before.device != root_device or before.size < 0:
        raise SnapshotReadError(SnapshotFailureKind.UNSUPPORTED_OBJECT)
    if _path_snapshot_stat(parent_fd, leaf_name) != before:
        raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
    if before.size > max_bytes:
        raise SnapshotReadError(SnapshotFailureKind.LIMIT)

    content = bytearray()
    content_hash = hashlib.sha256()
    while True:
        request_size = min(_READ_CHUNK_BYTES, max_bytes - len(content) + 1)
        chunk = _read(leaf_fd, request_size)
        if not chunk:
            break
        if len(chunk) > max_bytes - len(content):
            raise SnapshotReadError(SnapshotFailureKind.LIMIT)
        content.extend(chunk)
        content_hash.update(chunk)

    after = _snapshot_stat(_fstat(leaf_fd))
    named_after = _path_snapshot_stat(parent_fd, leaf_name)
    if after != before or named_after != before or len(content) != before.size:
        raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
    return FileSnapshot(data=bytes(content), stat=before, digest=content_hash.digest())


def _snapshot_stat(observed: os.stat_result) -> SnapshotStat:
    return SnapshotStat(
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


def _path_snapshot_stat(parent_fd: int, leaf_name: str) -> SnapshotStat | None:
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
