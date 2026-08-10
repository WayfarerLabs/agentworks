"""Online backup and restore for the Agentworks SQLite state database."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from agentworks.db.migrations import LATEST_VERSION, SCHEMA_SENTINELS
from agentworks.errors import BackupError, NotFoundError, StateError, ValidationError
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from pathlib import Path

BACKUP_DIRECTORY_NAME = "database-backups"
AUTOMATIC_BACKUP_LIMIT = 5
BACKUP_DEADLINE_SECONDS = 5.0

_BACKUP_PAGES = 256
_BACKUP_SLEEP_SECONDS = 0.05
_CONNECTION_TIMEOUT_SECONDS = 0.1
_AUTOMATIC_NAME = re.compile(
    r"^agentworks-pre-migration-(?P<timestamp>\d{8}T\d{12}Z)-v(?P<version>\d+)(?:-(?P<collision>\d+))?\.db$"
)


@dataclass(frozen=True)
class RetentionCleanupFailure:
    """One old automatic backup that could not be removed."""

    path: Path
    message: str


@dataclass(frozen=True)
class AutomaticBackupResult:
    """A completed automatic backup plus any non-fatal retention failures."""

    path: Path
    cleanup_failures: tuple[RetentionCleanupFailure, ...]


def backup_directory(database_path: Path) -> Path:
    """Return the dedicated backup directory beside ``database_path``."""
    return database_path.parent / BACKUP_DIRECTORY_NAME


def create_manual_backup(database_path: Path) -> Path:
    """Create an on-demand online snapshot without opening ``Database``."""
    validation = _validate_sqlite_file(database_path, source_kind="state database")
    validation.close()
    destination = _reserve_backup_path(database_path, automatic_version=None)
    try:
        _online_copy(database_path, destination)
    except BaseException:
        _remove_incomplete(destination)
        raise
    return destination


def create_pre_migration_backup(database_path: Path, current_version: int) -> AutomaticBackupResult:
    """Create an automatic snapshot and retain its five newest peers."""
    if type(current_version) is not int or current_version < 1:
        raise ValidationError("pre-migration backup requires a positive schema version")
    validation = _validate_sqlite_file(database_path, source_kind="state database")
    try:
        source_version = _read_schema_version(validation, source_kind="state database")
    except sqlite3.DatabaseError as error:
        _raise_sqlite_error(error, source_kind="state database")
    finally:
        validation.close()
    if source_version != current_version:
        raise StateError(
            f"state database schema version changed before backup: expected {current_version}, found {source_version}",
            hint="Retry the operation so Agentworks can inspect the current schema again.",
        )
    destination = _reserve_backup_path(database_path, automatic_version=source_version)
    try:
        _online_copy(database_path, destination)
    except BaseException:
        _remove_incomplete(destination)
        raise
    return AutomaticBackupResult(path=destination, cleanup_failures=_prune_automatic_backups(destination.parent))


def validate_restore_source(backup_path: Path) -> int:
    """Validate an Agentworks backup and return its supported schema version."""
    connection = _validate_sqlite_file(backup_path, source_kind="database backup")
    try:
        version = _read_schema_version(connection, source_kind="database backup")
        if version > LATEST_VERSION:
            raise StateError(
                f"database backup schema version {version} is newer than this Agentworks release supports "
                f"({LATEST_VERSION})",
                hint="Preserve this backup and restore it with a release that understands its schema.",
            )

        sentinels = SCHEMA_SENTINELS[version]
        table_types = dict(connection.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"))
        for table, required_columns in sentinels.items():
            if table_types.get(table) != "table":
                raise StateError(f"database backup is missing required Agentworks table '{table}'")
            columns = {
                str(column[1]) for column in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
            }
            missing = sorted(required_columns - columns)
            if missing:
                raise StateError(f"database backup table '{table}' is missing required columns: {', '.join(missing)}")
        return version
    except sqlite3.DatabaseError as error:
        _raise_sqlite_error(error, source_kind="database backup")
    finally:
        connection.close()


def _read_schema_version(connection: sqlite3.Connection, *, source_kind: str) -> int:
    """Return the claimed Agentworks schema version from an open source."""
    entry = connection.execute("SELECT type FROM sqlite_master WHERE name = 'schema_version'").fetchone()
    if entry is None or entry[0] != "table":
        raise StateError(f"{source_kind} is not an Agentworks state database")
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    version = row[0]
    if type(version) is not int or version < 1:
        raise StateError(f"{source_kind} schema version is invalid")
    return version


def restore_backup(backup_path: Path, database_path: Path) -> None:
    """Validate and copy ``backup_path`` into the live database path."""
    if backup_path.resolve() == database_path.resolve():
        raise ValidationError("database backup and live database paths must be different")

    validate_restore_source(backup_path)
    created_destination = False
    if not database_path.exists():
        try:
            database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created_destination = True
        except OSError as error:
            raise BackupError(f"could not create the live state database: {error}") from error

    try:
        _online_copy(backup_path, database_path)
    except BaseException:
        if created_destination:
            _remove_incomplete(database_path)
        raise


def _validate_sqlite_file(path: Path, *, source_kind: str) -> sqlite3.Connection:
    """Open and quick-check a SQLite source, leaving the connection open."""
    if not path.exists():
        raise NotFoundError(f"{source_kind} not found: {format_host_path(path)}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=_CONNECTION_TIMEOUT_SECONDS,
        )
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            connection.close()
            raise StateError(f"{source_kind} failed SQLite integrity validation")
        return connection
    except sqlite3.DatabaseError as error:
        if connection is not None:
            connection.close()
        _raise_sqlite_error(error, source_kind=source_kind)


def _raise_sqlite_error(error: sqlite3.DatabaseError, *, source_kind: str) -> NoReturn:
    code = getattr(error, "sqlite_errorcode", None)
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise BackupError(f"{source_kind} is busy; retry after other database users finish") from error
    raise StateError(f"{source_kind} is unavailable or malformed") from error


def _online_copy(source_path: Path, destination_path: Path) -> None:
    """Copy one SQLite database into another within a fixed deadline."""
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    deadline = time.monotonic() + BACKUP_DEADLINE_SECONDS

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline and _status != sqlite3.SQLITE_DONE:
            raise BackupError(
                f"database copy did not complete within {BACKUP_DEADLINE_SECONDS:g} seconds; "
                "retry after other database users finish"
            )

    try:
        source = sqlite3.connect(
            f"{source_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=_CONNECTION_TIMEOUT_SECONDS,
        )
        destination = sqlite3.connect(str(destination_path), timeout=_CONNECTION_TIMEOUT_SECONDS)
        source.backup(
            destination,
            pages=_BACKUP_PAGES,
            progress=progress,
            sleep=_BACKUP_SLEEP_SECONDS,
        )
    except BackupError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        raise BackupError(f"database copy failed: {error}") from error
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _reserve_backup_path(database_path: Path, *, automatic_version: int | None) -> Path:
    directory = backup_directory(database_path)
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as error:
        raise BackupError(f"could not create the database backup directory: {error}") from error

    timestamp = _utc_timestamp()
    if automatic_version is None:
        stem = f"agentworks-manual-{timestamp}"
    else:
        stem = f"agentworks-pre-migration-{timestamp}-v{automatic_version}"

    collision = 0
    while True:
        suffix = "" if collision == 0 else f"-{collision}"
        candidate = directory / f"{stem}{suffix}.db"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            collision += 1
            continue
        except OSError as error:
            raise BackupError(f"could not reserve a database backup file: {error}") from error
        os.close(descriptor)
        return candidate


def _prune_automatic_backups(directory: Path) -> tuple[RetentionCleanupFailure, ...]:
    recognized: list[tuple[str, int, Path]] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as error:
        return (RetentionCleanupFailure(directory, str(error)),)

    for path in entries:
        match = _AUTOMATIC_NAME.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        timestamp = match.group("timestamp")
        try:
            datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ")
        except ValueError:
            continue
        collision = int(match.group("collision") or 0)
        recognized.append((timestamp, collision, path))

    failures: list[RetentionCleanupFailure] = []
    for _timestamp, _collision, path in sorted(recognized)[:-AUTOMATIC_BACKUP_LIMIT]:
        try:
            path.unlink()
        except OSError as error:
            failures.append(RetentionCleanupFailure(path, str(error)))
    return tuple(failures)


def _remove_incomplete(path: Path) -> None:
    for artifact in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-journal"),
    ):
        with suppress(OSError):
            artifact.unlink(missing_ok=True)


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
