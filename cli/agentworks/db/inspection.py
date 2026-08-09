"""Read-only, fail-closed snapshots of the Agentworks state database."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.db.migrations import LATEST_VERSION
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db.database import Database


_INSPECTION_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_CHUNK_SIZE = 1024 * 1024
_SNAPSHOT_ERROR = "state database inspection snapshot could not be created"


class _SnapshotChanged(Exception):
    """The source file set changed while an inspection copy was made."""


class _UnsupportedSnapshotEntry(Exception):
    """A source entry is not a regular file that can be copied safely."""


@dataclass(frozen=True)
class _FileFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    digest: bytes


def _metadata(source_stat: os.stat_result) -> tuple[int, int, int, int]:
    return source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns


def _metadata_tuple(fingerprint: _FileFingerprint) -> tuple[int, int, int, int]:
    return fingerprint.device, fingerprint.inode, fingerprint.size, fingerprint.mtime_ns


def _source_flags() -> int:
    """Return nonblocking, no-follow flags available on this host."""
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_source(directory: Path, name: str, directory_fd: int | None) -> int:
    """Open one directory entry without following its final symlink."""
    try:
        if directory_fd is not None:
            return os.open(name, _source_flags(), dir_fd=directory_fd)
        return os.open(directory / name, _source_flags())
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise FileNotFoundError from None
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP}:
            raise _UnsupportedSnapshotEntry from None
        raise


def _regular_file_stat(descriptor: int) -> os.stat_result:
    source_stat = os.fstat(descriptor)
    if not stat.S_ISREG(source_stat.st_mode):
        raise _UnsupportedSnapshotEntry
    return source_stat


@contextmanager
def _pinned_directory(directory: Path) -> Iterator[int | None]:
    """Pin the resolved parent directory when the platform supports openat."""
    directory_fd: int | None = None
    if os.open in os.supports_dir_fd:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise _UnsupportedSnapshotEntry
            yield directory_fd
        finally:
            os.close(directory_fd)
        return
    yield None


def _file_metadata(directory: Path, name: str, directory_fd: int | None) -> tuple[int, int, int, int] | None:
    """Acquire one entry first, then classify and record its descriptor."""
    try:
        descriptor = _open_source(directory, name, directory_fd)
    except FileNotFoundError:
        return None
    try:
        return _metadata(_regular_file_stat(descriptor))
    finally:
        os.close(descriptor)


def _fingerprint_stream(
    directory: Path,
    name: str,
    directory_fd: int | None,
    *,
    expected: tuple[int, int, int, int],
    destination: Path | None = None,
) -> _FileFingerprint:
    """Hash one acquired regular file, optionally copying exactly its size."""
    try:
        descriptor = _open_source(directory, name, directory_fd)
    except FileNotFoundError:
        raise _SnapshotChanged from None

    destination_file = None
    try:
        descriptor_before = _regular_file_stat(descriptor)
        if _metadata(descriptor_before) != expected:
            raise _SnapshotChanged

        digest = hashlib.sha256()
        if destination is not None:
            destination_file = destination.open("xb")

        remaining = descriptor_before.st_size
        while remaining:
            chunk = os.read(descriptor, min(_SNAPSHOT_CHUNK_SIZE, remaining))
            if not chunk:
                raise _SnapshotChanged
            digest.update(chunk)
            if destination_file is not None:
                destination_file.write(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _SnapshotChanged

        descriptor_after = _regular_file_stat(descriptor)
        if _metadata(descriptor_after) != expected:
            raise _SnapshotChanged
        return _FileFingerprint(*expected, digest.digest())
    finally:
        if destination_file is not None:
            destination_file.close()
        os.close(descriptor)


def _current_metadata(
    directory: Path,
    names: tuple[str, str, str],
    directory_fd: int | None,
) -> tuple[tuple[int, int, int, int] | None, ...]:
    return tuple(_file_metadata(directory, name, directory_fd) for name in names)


def _copy_verified_file_set(source_db: Path, snapshot_db: Path) -> None:
    """Copy one byte-stable database/WAL/SHM set or request a retry."""
    source_directory = source_db.parent
    source_names = (source_db.name, f"{source_db.name}-wal", f"{source_db.name}-shm")
    snapshot_paths = (
        snapshot_db,
        snapshot_db.with_name(f"{snapshot_db.name}-wal"),
        snapshot_db.with_name(f"{snapshot_db.name}-shm"),
    )

    with _pinned_directory(source_directory) as directory_fd:
        initial = _current_metadata(source_directory, source_names, directory_fd)
        if initial[0] is None:
            raise _SnapshotChanged

        copied: list[_FileFingerprint | None] = []
        for name, destination, expected in zip(source_names, snapshot_paths, initial, strict=True):
            if expected is None:
                copied.append(None)
                continue
            copied.append(
                _fingerprint_stream(
                    source_directory,
                    name,
                    directory_fd,
                    expected=expected,
                    destination=destination,
                )
            )
            if _current_metadata(source_directory, source_names, directory_fd) != initial:
                raise _SnapshotChanged

        verified: list[_FileFingerprint | None] = []
        for name, expected in zip(source_names, initial, strict=True):
            verified.append(
                None
                if expected is None
                else _fingerprint_stream(
                    source_directory,
                    name,
                    directory_fd,
                    expected=expected,
                )
            )
            if _current_metadata(source_directory, source_names, directory_fd) != initial:
                raise _SnapshotChanged
        if tuple(copied) != tuple(verified):
            raise _SnapshotChanged


def _requested_entry_exists(requested_path: Path) -> bool:
    """Distinguish an absent name from a broken or invalid directory entry."""
    try:
        requested_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise StateError(_SNAPSHOT_ERROR) from None
    return True


@contextmanager
def inspection_snapshot(
    database_type: type[Database],
    requested_path: Path,
) -> Iterator[tuple[bool, int, int, Database | None]]:
    """Open a private, non-migrating, verified snapshot for inspection."""
    if not _requested_entry_exists(requested_path):
        yield False, 0, LATEST_VERSION, None
        return

    temporary: tempfile.TemporaryDirectory[str] | None = None
    connection: sqlite3.Connection | None = None
    database: Database | None = None
    try:
        try:
            source_path = requested_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise StateError(_SNAPSHOT_ERROR) from None

        temporary = tempfile.TemporaryDirectory(prefix="agentworks-db-inspection-")
        for attempt in range(_INSPECTION_SNAPSHOT_ATTEMPTS):
            candidate_connection: sqlite3.Connection | None = None
            try:
                attempt_directory = Path(temporary.name) / str(attempt)
                attempt_directory.mkdir()
                candidate = attempt_directory / source_path.name
                _copy_verified_file_set(source_path, candidate)
                candidate_connection = sqlite3.connect(
                    f"{candidate.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
                if candidate_connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    candidate_connection.close()
                    continue
            except _SnapshotChanged:
                if candidate_connection is not None:
                    candidate_connection.close()
                continue
            except sqlite3.DatabaseError:
                if candidate_connection is not None:
                    candidate_connection.close()
                continue
            except (_UnsupportedSnapshotEntry, OSError):
                raise StateError(_SNAPSHOT_ERROR) from None
            connection = candidate_connection
            break
        else:
            raise StateError(_SNAPSHOT_ERROR)

        if connection is None:
            raise StateError(_SNAPSHOT_ERROR)
        try:
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
        except sqlite3.OperationalError:
            current = 0
        if current == LATEST_VERSION:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            database = database_type.__new__(database_type)
            database._conn = connection
            database._tx_depth = 0
            connection = None
        yield True, current, LATEST_VERSION, database
    finally:
        if database is not None:
            database.close()
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.cleanup()
