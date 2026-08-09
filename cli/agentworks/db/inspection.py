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
from agentworks.errors import DatabaseInspectionUnavailable, StateError

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


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or not value:
        raise DatabaseInspectionUnavailable
    return value


def _source_flags() -> int:
    """Return the complete source-acquisition flags or fail closed."""
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | _required_open_flag("O_NONBLOCK")
        | _required_open_flag("O_NOFOLLOW")
    )


def _directory_flags() -> int:
    """Return flags that acquire only a real directory descriptor."""
    return _source_flags() | _required_open_flag("O_DIRECTORY")


def _require_snapshot_protocol() -> None:
    """Reject hosts lacking any primitive required by safe acquisition."""
    _directory_flags()
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    try:
        has_openat = os.open in supports_dir_fd
    except TypeError:
        has_openat = False
    if not has_openat:
        raise DatabaseInspectionUnavailable


def _raise_if_unsupported_operation(error: OSError) -> None:
    unavailable_errnos = {errno.ENOSYS, errno.EOPNOTSUPP}
    if hasattr(errno, "ENOTSUP"):
        unavailable_errnos.add(errno.ENOTSUP)
    if error.errno in unavailable_errnos:
        raise DatabaseInspectionUnavailable from None


def _open_source(name: str, directory_fd: int) -> int:
    """Open one directory entry without following its final symlink."""
    try:
        return os.open(name, _source_flags(), dir_fd=directory_fd)
    except (NotImplementedError, TypeError):
        raise DatabaseInspectionUnavailable from None
    except OSError as error:
        _raise_if_unsupported_operation(error)
        if error.errno == errno.ENOENT:
            raise FileNotFoundError from None
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV}:
            raise _UnsupportedSnapshotEntry from None
        raise


def _regular_file_stat(descriptor: int) -> os.stat_result:
    try:
        source_stat = os.fstat(descriptor)
    except (NotImplementedError, TypeError):
        raise DatabaseInspectionUnavailable from None
    except OSError as error:
        _raise_if_unsupported_operation(error)
        raise
    if not stat.S_ISREG(source_stat.st_mode):
        raise _UnsupportedSnapshotEntry
    return source_stat


def _opened_directory(descriptor: int) -> int:
    """Validate an acquired descriptor, closing it on classification failure."""
    try:
        source_stat = os.fstat(descriptor)
    except (NotImplementedError, TypeError):
        os.close(descriptor)
        raise DatabaseInspectionUnavailable from None
    except OSError as error:
        os.close(descriptor)
        _raise_if_unsupported_operation(error)
        raise
    if not stat.S_ISDIR(source_stat.st_mode):
        os.close(descriptor)
        raise _UnsupportedSnapshotEntry
    return descriptor


def _open_directory_anchor(anchor: str) -> int:
    """Open the trusted filesystem anchor that starts a descriptor walk."""
    try:
        return _opened_directory(os.open(anchor, _directory_flags()))
    except (NotImplementedError, TypeError):
        raise DatabaseInspectionUnavailable from None
    except OSError as error:
        _raise_if_unsupported_operation(error)
        raise


def _open_directory_component(name: str, parent_fd: int) -> int:
    """Open one real directory relative to its already pinned parent."""
    try:
        return _opened_directory(os.open(name, _directory_flags(), dir_fd=parent_fd))
    except (NotImplementedError, TypeError):
        raise DatabaseInspectionUnavailable from None
    except OSError as error:
        _raise_if_unsupported_operation(error)
        raise


def _rooted_lexical_requested_path(requested_path: Path) -> Path:
    """Root the requested path without resolving its final component."""
    if requested_path.drive and not requested_path.root:
        raise _UnsupportedSnapshotEntry
    rooted_path = requested_path if requested_path.is_absolute() else Path.cwd() / requested_path
    if not rooted_path.anchor:
        raise _UnsupportedSnapshotEntry
    return rooted_path


def _nearest_existing_resolved_parent(rooted_path: Path) -> Path:
    """Resolve the closest existing ancestor of the requested parent."""
    candidate = rooted_path.parent
    while True:
        try:
            return candidate.resolve(strict=True)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise _UnsupportedSnapshotEntry from None
            candidate = parent
        except (OSError, RuntimeError):
            raise _UnsupportedSnapshotEntry from None


def _probe_resolved_directory_protocol(directory: Path) -> None:
    """Exercise directory-relative acquisition on one resolved directory."""
    with _pinned_directory(directory) as directory_fd:
        probe_fd = _open_directory_component(".", directory_fd)
        os.close(probe_fd)


def _preflight_snapshot_protocol(requested_path: Path) -> None:
    """Exercise acquisition on the nearest existing requested-path ancestor."""
    _require_snapshot_protocol()
    rooted_path = _rooted_lexical_requested_path(requested_path)
    _probe_resolved_directory_protocol(_nearest_existing_resolved_parent(rooted_path))


@contextmanager
def _pinned_directory(directory: Path) -> Iterator[int]:
    """Walk from a trusted anchor and pin the resolved parent directory."""
    anchor = directory.anchor
    components = directory.parts[1:]
    if not anchor or not directory.is_absolute() or any(component in {"", ".", ".."} for component in components):
        raise _UnsupportedSnapshotEntry

    directory_fd = _open_directory_anchor(anchor)
    try:
        for component in components:
            next_fd = _open_directory_component(component, directory_fd)
            previous_fd = directory_fd
            directory_fd = next_fd
            os.close(previous_fd)
        yield directory_fd
    finally:
        os.close(directory_fd)


def _file_metadata(name: str, directory_fd: int) -> tuple[int, int, int, int] | None:
    """Acquire one entry first, then classify and record its descriptor."""
    try:
        descriptor = _open_source(name, directory_fd)
    except FileNotFoundError:
        return None
    try:
        return _metadata(_regular_file_stat(descriptor))
    finally:
        os.close(descriptor)


def _fingerprint_stream(
    name: str,
    directory_fd: int,
    *,
    expected: tuple[int, int, int, int],
    destination: Path | None = None,
) -> _FileFingerprint:
    """Hash one acquired regular file, optionally copying exactly its size."""
    try:
        descriptor = _open_source(name, directory_fd)
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
    names: tuple[str, str, str],
    directory_fd: int,
) -> tuple[tuple[int, int, int, int] | None, ...]:
    return tuple(_file_metadata(name, directory_fd) for name in names)


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
        initial = _current_metadata(source_names, directory_fd)
        if initial[0] is None:
            raise _SnapshotChanged

        copied: list[_FileFingerprint | None] = []
        for name, destination, expected in zip(source_names, snapshot_paths, initial, strict=True):
            if expected is None:
                copied.append(None)
                continue
            copied.append(
                _fingerprint_stream(
                    name,
                    directory_fd,
                    expected=expected,
                    destination=destination,
                )
            )
            if _current_metadata(source_names, directory_fd) != initial:
                raise _SnapshotChanged

        verified: list[_FileFingerprint | None] = []
        for name, expected in zip(source_names, initial, strict=True):
            verified.append(
                None
                if expected is None
                else _fingerprint_stream(
                    name,
                    directory_fd,
                    expected=expected,
                )
            )
            if _current_metadata(source_names, directory_fd) != initial:
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
    try:
        _preflight_snapshot_protocol(requested_path)
    except DatabaseInspectionUnavailable:
        raise
    except (_UnsupportedSnapshotEntry, OSError):
        raise StateError(_SNAPSHOT_ERROR) from None

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
        try:
            _probe_resolved_directory_protocol(source_path.parent)
        except DatabaseInspectionUnavailable:
            raise
        except (_UnsupportedSnapshotEntry, OSError):
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
            current = _validated_schema_version(row[0])
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


def _validated_schema_version(value: object) -> int:
    """Accept only SQLite null or an exact nonnegative integer version."""
    if value is None:
        return 0
    if type(value) is int and value >= 0:
        return value
    raise StateError(_SNAPSHOT_ERROR)
