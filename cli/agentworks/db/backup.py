"""Online backup and restore for the Agentworks SQLite state database."""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from agentworks.db.migrations import LATEST_VERSION, SCHEMA_SENTINELS
from agentworks.errors import BackupError, BusyStateError, NotFoundError, StateError, ValidationError
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from agentworks.db.database import Database

BACKUP_DIRECTORY_NAME = "database-backups"
AUTOMATIC_BACKUP_LIMIT = 5
BACKUP_DEADLINE_SECONDS = 5.0
MIGRATION_LOCK_NAME = "agentworks-migration.lock"
MIGRATION_LOCK_TIMEOUT_SECONDS = 30.0

_BACKUP_PAGES = 256
_BACKUP_SLEEP_SECONDS = 0.05
_BACKUP_CONNECTION_TIMEOUT_SECONDS = 0.1
_COMPLETION_TIMEOUT_SECONDS = 0.1
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


class SchemaState(Enum):
    """Non-migrating classification of one state database path."""

    ABSENT = auto()
    STALE = auto()
    CURRENT = auto()
    FUTURE = auto()
    BUSY = auto()
    MALFORMED = auto()


@dataclass(frozen=True)
class SchemaInspection:
    """One WAL-aware schema observation and its SQLite schema cookie."""

    state: SchemaState
    current_version: int
    latest_version: int
    schema_cookie: int | None
    error_message: str | None = None


@dataclass(frozen=True)
class DatabaseOpenPlan:
    """A safe-open decision, including a qualified stale baseline."""

    inspection: SchemaInspection
    stale_baseline: tuple[int, int] | None = None


@dataclass(frozen=True)
class SafeOpenResult:
    """A writable database plus its optional completed recovery snapshot."""

    database: Database
    backup: AutomaticBackupResult | None = None


def backup_directory(database_path: Path) -> Path:
    """Return the dedicated backup directory beside ``database_path``."""
    return database_path.parent / BACKUP_DIRECTORY_NAME


def _connect_ro(uri: str, *, timeout: float | None) -> sqlite3.Connection:
    """Open a read-only SQLite URI connection, passing ``timeout`` through
    only when the caller supplies one.

    Omitting the keyword entirely, rather than passing ``timeout=None``
    through to ``sqlite3.connect``, is what leaves the driver's own
    multi-second default wait untouched for callers that do not opt into a
    bound; ``sqlite3.connect``'s ``timeout`` parameter has no ``None``
    meaning of its own, so this can't be collapsed into a single call.
    """
    if timeout is not None:
        return sqlite3.connect(uri, uri=True, timeout=timeout)
    return sqlite3.connect(uri, uri=True)


def inspect_schema(database_path: Path, *, timeout: float | None = None) -> SchemaInspection:
    """Inspect schema version and cookie without creating or migrating state.

    ``timeout`` bounds how long SQLite retries against a busy database
    (``sqlite3.connect``'s own ``timeout`` parameter) before giving up.
    Omit it (the default) to keep the driver's own default wait; callers
    that must not block a fast path (completion) pass an explicit, tight
    bound. A database still busy when the wait expires is reported as
    :attr:`SchemaState.BUSY`, a distinct, transient classification from
    :attr:`SchemaState.MALFORMED`: retrying once the other connection
    finishes is expected to succeed, unlike genuine corruption.
    """
    if not database_path.exists():
        return SchemaInspection(SchemaState.ABSENT, 0, LATEST_VERSION, None)
    connection: sqlite3.Connection | None = None
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        connection = _connect_ro(uri, timeout=timeout)
        entry = connection.execute("SELECT type FROM sqlite_master WHERE name = 'schema_version'").fetchone()
        if entry is None:
            version = 0
        elif entry[0] != "table":
            raise StateError("state database is not an Agentworks state database")
        else:
            version_row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            claimed_version = version_row[0]
            if claimed_version is None:
                version = 0
            elif type(claimed_version) is int and claimed_version >= 1:
                version = claimed_version
            else:
                raise StateError("state database schema version is invalid")
        cookie_row = connection.execute("PRAGMA schema_version").fetchone()
        cookie = cookie_row[0]
        if type(cookie) is not int or cookie < 0:
            raise StateError("state database schema cookie is invalid")
    except (OSError, sqlite3.DatabaseError, StateError) as error:
        if isinstance(error, StateError) and "schema version is invalid" in str(error):
            return SchemaInspection(SchemaState.MALFORMED, 0, LATEST_VERSION, None, str(error))
        if _is_busy(error):
            return SchemaInspection(SchemaState.BUSY, 0, LATEST_VERSION, None)
        return SchemaInspection(
            SchemaState.MALFORMED,
            0,
            LATEST_VERSION,
            None,
            "state database schema is unavailable or malformed",
        )
    finally:
        if connection is not None:
            connection.close()
    if version < LATEST_VERSION:
        state = SchemaState.STALE
    elif version == LATEST_VERSION:
        state = SchemaState.CURRENT
    else:
        state = SchemaState.FUTURE
    return SchemaInspection(state, version, LATEST_VERSION, cookie)


def prepare_database_open(database_path: Path) -> DatabaseOpenPlan:
    """Inspect state and qualify a stale observation under the migration lock."""
    initial = inspect_schema(database_path)
    _raise_if_unopenable(initial)
    if initial.state is not SchemaState.STALE:
        return DatabaseOpenPlan(initial)

    lock = _acquire_migration_lock(database_path, timeout=MIGRATION_LOCK_TIMEOUT_SECONDS)
    if lock is None:
        raise BusyStateError()
    try:
        qualified = inspect_schema(database_path)
        _raise_if_unopenable(qualified)
        if qualified.state is SchemaState.CURRENT:
            return DatabaseOpenPlan(qualified)
        initial_tokens = (initial.current_version, initial.schema_cookie)
        qualified_tokens = (qualified.current_version, qualified.schema_cookie)
        if qualified.state is SchemaState.STALE and qualified_tokens != initial_tokens:
            raise StateError(
                "state database changed before its stale state could be qualified under the migration lock",
                hint="Inspect the database with `agw doctor`, then retry the original command.",
            )
        if qualified.state is not SchemaState.STALE or qualified.schema_cookie is None:
            raise StateError("state database changed while its migration state was being qualified")
        if qualified.current_version > 0:
            connection = _validate_sqlite_file(database_path, source_kind="state database")
            try:
                _validate_canonical_schema(
                    connection,
                    qualified.current_version,
                    source_kind="state database",
                    hint=(
                        "Restore an unmodified backup captured after a completed Agentworks migration, "
                        "or repair the partial schema before retrying."
                    ),
                )
            except sqlite3.DatabaseError as error:
                _raise_sqlite_error(error, source_kind="state database")
            finally:
                connection.close()
        return DatabaseOpenPlan(
            qualified,
            stale_baseline=(qualified.current_version, qualified.schema_cookie),
        )
    finally:
        _release_migration_lock(lock)


def open_database_safely(
    database_path: Path,
    plan: DatabaseOpenPlan,
    *,
    create_backup: bool,
) -> SafeOpenResult:
    """Open writable state, serializing and protecting a stale migration."""
    if plan.inspection.state in (SchemaState.ABSENT, SchemaState.CURRENT):
        return SafeOpenResult(_construct_writable_database(database_path))
    if plan.inspection.state is not SchemaState.STALE or plan.stale_baseline is None:
        raise StateError("state database open plan is invalid")

    lock = _acquire_migration_lock(database_path, timeout=MIGRATION_LOCK_TIMEOUT_SECONDS)
    if lock is None:
        raise BusyStateError()
    backup: AutomaticBackupResult | None = None
    try:
        current = inspect_schema(database_path)
        _raise_if_unopenable(current)
        if current.state is SchemaState.CURRENT:
            return SafeOpenResult(_construct_writable_database(database_path))
        current_tokens = (current.current_version, current.schema_cookie)
        if current.state is not SchemaState.STALE or current_tokens != plan.stale_baseline:
            raise StateError(
                "state database changed during migration interaction and remains outdated",
                hint="Inspect the database with `agw doctor` before retrying.",
            )
        # Version zero is an uninitialized database, not a restorable
        # historical Agentworks schema. It is still qualified and rechecked
        # under the migration lock, but must never produce a fake backup.
        if create_backup and current.current_version > 0:
            backup = create_pre_migration_backup(database_path, current.current_version)
        try:
            database = _construct_writable_database(database_path)
        except BaseException as error:
            hint = _migration_failure_hint(backup)
            if isinstance(error, Exception):
                raise StateError(
                    "state database migration failed; the live database may be partially changed",
                    hint=hint,
                ) from error
            # Standard exception notes are rendered with an unhandled
            # KeyboardInterrupt/SystemExit traceback while preserving the
            # original exception object and control-flow semantics.
            error.add_note(f"Agentworks migration recovery: {hint}")
            raise
        return SafeOpenResult(database, backup)
    finally:
        _release_migration_lock(lock)


def _construct_writable_database(database_path: Path) -> Database:
    """The single production construction site for writable ``Database``."""
    from agentworks.db.database import Database

    return Database(database_path)


def open_completion_database(database_path: Path) -> Database | None:
    """Open current state read-only, or return no database for completion.

    Uses an ordinary read-only open, not an immutable one: the state database
    runs in WAL mode, so `-wal`/`-shm` sidecars are the normal steady state of
    any live connection, not evidence of unavailable or damaged state.
    Immutable mode ignores the WAL and would silently serve stale rows
    whenever committed content still sits there uncheckpointed (issue #502).
    An ordinary read-only open reads the WAL correctly, so a concurrent
    writer is a non-issue rather than a veto.

    This read-only open still needs `-shm` to be creatable in the config
    directory (SQLite's shared-memory index for WAL readers). That directory
    being user-owned and writable is the normal supported configuration; if
    it is not (a read-only mount, a permission-damaged home), the probe
    refuses cleanly with no candidates rather than hanging or writing.

    Both connects use ``_COMPLETION_TIMEOUT_SECONDS`` rather than the
    driver's multi-second default: a completion probe backs a TAB press, so
    it must fail fast against a database another process holds locked
    (e.g. a concurrent writer inside `BEGIN EXCLUSIVE`) rather than freeze
    the shell for seconds.
    """
    from agentworks.db.database import Database

    inspection = inspect_schema(database_path, timeout=_COMPLETION_TIMEOUT_SECONDS)
    if inspection.state is not SchemaState.CURRENT:
        return None
    try:
        return Database(database_path, read_only=True, timeout=_COMPLETION_TIMEOUT_SECONDS)
    except StateError:
        # A lock acquired between the inspection above and this open (or one
        # still resolving within the bounded wait) is the same "unavailable
        # for completion" outcome as every other refusal here, not a reason
        # to raise past a TAB press.
        return None


def render_restore_command(backup_path: Path, *, platform: str | None = None) -> str:
    """Render an executable restore command for the current shell family."""
    platform_name = platform or sys.platform
    home = Path.home().resolve()
    resolved = backup_path.resolve()
    if platform_name == "win32":
        if resolved.is_relative_to(home):
            relative = resolved.relative_to(home).as_posix().replace("/", "\\").replace("'", "''")
            argument = f"(Join-Path $HOME '{relative}')"
        else:
            argument = "'" + str(resolved).replace("'", "''") + "'"
    elif resolved.is_relative_to(home):
        relative = resolved.relative_to(home).as_posix()
        argument = f"$HOME/{shlex.quote(relative)}"
    else:
        argument = shlex.quote(str(resolved))
    return f"agw database restore {argument}"


def _raise_if_busy(inspection: SchemaInspection) -> None:
    """Raise BusyStateError if BUSY; no-op otherwise. Shared by the two
    BUSY raise sites (_raise_if_unopenable below and Database.check_schema)
    so both construct the exact same exception rather than each carrying
    its own copy; BusyStateError takes no arguments, so this has no way to
    pass caller-suppliable text through either.
    """
    if inspection.state is SchemaState.BUSY:
        raise BusyStateError()


def _raise_if_unopenable(inspection: SchemaInspection) -> None:
    if inspection.state is SchemaState.FUTURE:
        raise StateError(
            f"state database schema version {inspection.current_version} is newer than this "
            f"Agentworks release supports ({inspection.latest_version})",
            hint="Preserve it with `agw database backup`, then use a release that understands its schema.",
        )
    _raise_if_busy(inspection)
    if inspection.state is SchemaState.MALFORMED:
        raise StateError(
            inspection.error_message or "state database schema is unavailable or malformed",
            hint="Run `agw doctor` for a non-migrating database check.",
        )


def _migration_failure_hint(backup: AutomaticBackupResult | None) -> str:
    if backup is None:
        return "No pre-migration backup was created. Repair or restore the database before retrying."
    return f"Restore the pre-migration backup with: {render_restore_command(backup.path)}"


def _migration_lock_path(database_path: Path) -> Path:
    return database_path.parent / MIGRATION_LOCK_NAME


def _acquire_migration_lock(database_path: Path, *, timeout: float) -> sqlite3.Connection | None:
    lock_path = _migration_lock_path(database_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    except OSError as error:
        raise StateError(f"could not create the state database migration lock: {error}") from error
    else:
        os.close(descriptor)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(lock_path), timeout=timeout)
        connection.execute("BEGIN IMMEDIATE")
        return connection
    except sqlite3.DatabaseError as error:
        if connection is not None:
            connection.close()
        if _is_busy(error):
            return None
        raise StateError(f"state database migration lock is unavailable: {error}") from error


def _release_migration_lock(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    finally:
        connection.close()


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

        _validate_canonical_schema(
            connection,
            version,
            source_kind="database backup",
            hint="Select an unmodified backup captured after a completed Agentworks migration.",
        )
        return version
    except sqlite3.DatabaseError as error:
        _raise_sqlite_error(error, source_kind="database backup")
    finally:
        connection.close()


def _validate_canonical_schema(
    connection: sqlite3.Connection,
    version: int,
    *,
    source_kind: str,
    hint: str,
) -> None:
    """Require the exact completed Agentworks table and column shape."""
    expected_tables = SCHEMA_SENTINELS[version]
    actual_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_tables = sorted(set(expected_tables) - actual_tables)
    unexpected_tables = sorted(actual_tables - set(expected_tables))
    if missing_tables:
        raise StateError(
            f"{source_kind} is missing required Agentworks tables: {', '.join(missing_tables)}",
            hint=hint,
        )
    if unexpected_tables:
        raise StateError(
            f"{source_kind} has unexpected tables for completed schema version {version}: "
            f"{', '.join(unexpected_tables)}",
            hint=hint,
        )
    for table, expected_columns in expected_tables.items():
        columns = {str(column[1]) for column in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        missing_columns = sorted(expected_columns - columns)
        unexpected_columns = sorted(columns - expected_columns)
        if missing_columns:
            raise StateError(
                f"{source_kind} table '{table}' is missing required columns: {', '.join(missing_columns)}",
                hint=hint,
            )
        if unexpected_columns:
            raise StateError(
                f"{source_kind} table '{table}' has unexpected columns for completed schema version "
                f"{version}: {', '.join(unexpected_columns)}",
                hint=hint,
            )


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
        # Backup and restore sources may have committed WAL content, so immutable
        # mode is unsafe here. An ordinary read-only open can leave restrictive
        # SQLite coordination sidecars; retain them rather than racing SQLite or
        # another reader by deleting them.
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=_BACKUP_CONNECTION_TIMEOUT_SECONDS,
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


def _is_busy(error: BaseException) -> bool:
    """True for SQLITE_BUSY/SQLITE_LOCKED, masking off SQLite's extended
    result-code bits in ``sqlite_errorcode`` (the low byte is the primary
    code). Safe to call on any exception: one without that attribute (a
    non-SQLite error) simply is not busy."""
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _raise_sqlite_error(error: sqlite3.DatabaseError, *, source_kind: str) -> NoReturn:
    if _is_busy(error):
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
            timeout=_BACKUP_CONNECTION_TIMEOUT_SECONDS,
        )
        destination = sqlite3.connect(str(destination_path), timeout=_BACKUP_CONNECTION_TIMEOUT_SECONDS)
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
