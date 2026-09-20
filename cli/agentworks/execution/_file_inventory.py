"""Bounded Linux directory inventory beneath a borrowed root descriptor.

The caller owns the trusted inventory-root descriptor and cooperating-writer
lock. Inventory is a bounded set of metadata observations, not a transaction
against noncooperating writers. This module reads no file content and creates
no filesystem state.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ._file_objects import (
    FileKind,
    FileObjectError,
    FileObjectFailureKind,
    revision_kind,
    stat_file_object,
)
from ._file_paths import ConfinedOpenError, ConfinedOpenFailure, open_linux_confined

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Protocol

    from ._file_stat import FileRevision, FileStat

    class _ScandirIterator(Protocol):
        def __iter__(self) -> Iterator[os.DirEntry[str]]: ...

        def __next__(self) -> os.DirEntry[str]: ...

        def close(self) -> None: ...


_MAX_NAME_BYTES = 255
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class FileInventoryFailureKind(Enum):
    """Closed inventory failures that reveal no filesystem names or metadata."""

    UNSUPPORTED = "unsupported"
    LIMIT = "limit"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class FileInventoryError(Exception):
    """A closed inventory refusal."""

    def __init__(self, kind: FileInventoryFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


@dataclass(frozen=True, slots=True, repr=False)
class FileInventoryEntry:
    """One relative UTF-8 path and its metadata-only revision."""

    relative_path: str
    revision: FileRevision


@dataclass
class _InventoryState:
    entries: list[FileInventoryEntry]
    encoded_bytes: int
    max_entries: int
    max_encoded_bytes: int
    root_device: int
    expires_at: float | None


def inventory_directory(
    root_fd: int,
    *,
    max_entries: int,
    max_depth: int,
    max_encoded_bytes: int,
    expires_at: float | None,
) -> tuple[FileInventoryEntry, ...]:
    """Return a complete bounded inventory beneath a borrowed directory.

    Numeric limits and the finite-or-unbounded guest-monotonic expiry are
    typed, already-validated interior values. Depth one reports immediate
    children. Directories at the requested boundary are observed but never
    enumerated.
    """
    if sys.platform != "linux":
        raise FileInventoryError(FileInventoryFailureKind.UNSUPPORTED)
    _raise_if_expired(expires_at)
    root = _fstat(root_fd)
    if not stat.S_ISDIR(root.st_mode):
        raise FileInventoryError(FileInventoryFailureKind.UNSUPPORTED)

    state = _InventoryState(
        entries=[],
        encoded_bytes=2,
        max_entries=max_entries,
        max_encoded_bytes=max_encoded_bytes,
        root_device=root.st_dev,
        expires_at=expires_at,
    )
    if state.encoded_bytes > max_encoded_bytes:
        raise FileInventoryError(FileInventoryFailureKind.LIMIT)
    _walk_directory(root_fd, "", 1, max_depth, state)
    _raise_if_expired(expires_at)
    state.entries.sort(key=lambda entry: entry.relative_path.encode("utf-8"))
    _raise_if_expired(expires_at)
    return tuple(state.entries)


def encode_inventory(entries: tuple[FileInventoryEntry, ...]) -> bytes:
    """Encode inventory as one compact canonical JSON array.

    Each record contains the relative path and all nine ``FileStat`` fields.
    Array brackets and comma framing are part of the encoded byte count used by
    :func:`inventory_directory`.
    """
    return b"[" + b",".join(_encode_record(entry) for entry in entries) + b"]"


def _walk_directory(
    directory_fd: int,
    prefix: str,
    depth: int,
    max_depth: int,
    state: _InventoryState,
) -> None:
    _raise_if_expired(state.expires_at)
    iterator = _scandir(directory_fd)
    try:
        while True:
            _raise_if_expired(state.expires_at)
            scanned = _next_entry(iterator)
            if scanned is None:
                return
            name = scanned.name
            name_bytes = _name_bytes(name)
            if len(name_bytes) > _MAX_NAME_BYTES:
                raise FileInventoryError(FileInventoryFailureKind.LIMIT)
            relative_path = name if not prefix else f"{prefix}/{name}"
            revision = _observe(directory_fd, name, state.expires_at)
            entry = FileInventoryEntry(relative_path, revision)
            _append_bounded(state, entry)

            if revision_kind(revision) is FileKind.DIRECTORY and depth < max_depth:
                _walk_child_directory(directory_fd, name, relative_path, depth, max_depth, revision, state)
    finally:
        _close_scandir(iterator)


def _walk_child_directory(
    parent_fd: int,
    name: str,
    relative_path: str,
    depth: int,
    max_depth: int,
    observed: FileRevision,
    state: _InventoryState,
) -> None:
    _raise_if_expired(state.expires_at)
    child_fd = _open_directory(parent_fd, name)
    if child_fd is None:
        raise FileInventoryError(FileInventoryFailureKind.CONFLICT)
    try:
        _require_directory_identity(child_fd, observed, state.root_device)
        _require_named_identity(parent_fd, name, observed, state.expires_at)
        _walk_directory(child_fd, relative_path, depth + 1, max_depth, state)
        _require_directory_identity(child_fd, observed, state.root_device)
        _require_named_identity(parent_fd, name, observed, state.expires_at)
    finally:
        _close(child_fd)


def _append_bounded(state: _InventoryState, entry: FileInventoryEntry) -> None:
    if len(state.entries) >= state.max_entries:
        raise FileInventoryError(FileInventoryFailureKind.LIMIT)
    record_size = len(_encode_record(entry))
    framed_size = state.encoded_bytes + record_size + (1 if state.entries else 0)
    if framed_size > state.max_encoded_bytes:
        raise FileInventoryError(FileInventoryFailureKind.LIMIT)
    state.entries.append(entry)
    state.encoded_bytes = framed_size


def _encode_record(entry: FileInventoryEntry) -> bytes:
    observed = entry.revision.stat
    record = {
        "changed_ns": observed.changed_ns,
        "device": observed.device,
        "gid": observed.gid,
        "inode": observed.inode,
        "link_count": observed.link_count,
        "mode": observed.mode,
        "modified_ns": observed.modified_ns,
        "relative_path": entry.relative_path,
        "size": observed.size,
        "uid": observed.uid,
    }
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _observe(parent_fd: int, name: str, expires_at: float | None) -> FileRevision:
    try:
        revision = stat_file_object(parent_fd, name, expires_at=expires_at)
    except FileObjectError as error:
        raise FileInventoryError(_object_failure(error.kind)) from None
    if revision is None:
        raise FileInventoryError(FileInventoryFailureKind.CONFLICT)
    return revision


def _require_named_identity(
    parent_fd: int,
    name: str,
    expected: FileRevision,
    expires_at: float | None,
) -> None:
    current = _observe(parent_fd, name, expires_at)
    if _identity(current.stat) != _identity(expected.stat) or revision_kind(current) is not FileKind.DIRECTORY:
        raise FileInventoryError(FileInventoryFailureKind.CONFLICT)


def _require_directory_identity(descriptor: int, expected: FileRevision, root_device: int) -> None:
    current = _fstat(descriptor)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != root_device
        or (current.st_dev, current.st_ino) != _identity(expected.stat)
    ):
        raise FileInventoryError(FileInventoryFailureKind.CONFLICT)


def _open_directory(parent_fd: int, name: str) -> int | None:
    try:
        return open_linux_confined(parent_fd, name, _DIRECTORY_FLAGS)
    except ConfinedOpenError as error:
        if error.kind is ConfinedOpenFailure.CONFLICT:
            kind = FileInventoryFailureKind.CONFLICT
        elif error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT:
            kind = FileInventoryFailureKind.UNSUPPORTED
        else:
            kind = FileInventoryFailureKind.IO
        raise FileInventoryError(kind) from None


def _scandir(directory_fd: int) -> _ScandirIterator:
    try:
        return os.scandir(directory_fd)
    except OSError:
        raise FileInventoryError(FileInventoryFailureKind.IO) from None


def _next_entry(iterator: Iterator[os.DirEntry[str]]) -> os.DirEntry[str] | None:
    try:
        return next(iterator, None)
    except OSError:
        raise FileInventoryError(FileInventoryFailureKind.IO) from None


def _name_bytes(name: str) -> bytes:
    if type(name) is not str or not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise FileInventoryError(FileInventoryFailureKind.UNSUPPORTED)
    try:
        return name.encode("utf-8")
    except UnicodeEncodeError:
        raise FileInventoryError(FileInventoryFailureKind.UNSUPPORTED) from None


def _identity(observed: FileStat) -> tuple[int, int]:
    return observed.device, observed.inode


def _object_failure(kind: FileObjectFailureKind) -> FileInventoryFailureKind:
    if kind is FileObjectFailureKind.UNSUPPORTED:
        return FileInventoryFailureKind.UNSUPPORTED
    if kind is FileObjectFailureKind.CONFLICT:
        return FileInventoryFailureKind.CONFLICT
    if kind is FileObjectFailureKind.DEADLINE:
        return FileInventoryFailureKind.DEADLINE
    return FileInventoryFailureKind.IO


def _fstat(descriptor: int) -> os.stat_result:
    observed: os.stat_result | None = None
    with suppress(OSError):
        observed = os.fstat(descriptor)
    if observed is None:
        raise FileInventoryError(FileInventoryFailureKind.IO)
    return observed


def _raise_if_expired(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise FileInventoryError(FileInventoryFailureKind.DEADLINE)


def _close_scandir(iterator: _ScandirIterator) -> None:
    with suppress(OSError):
        iterator.close()


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
