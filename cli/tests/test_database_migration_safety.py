"""Focused contracts for safe automatic SQLite migration opens."""

from __future__ import annotations

import ast
import multiprocessing
import os
import queue
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentworks.db import (
    LATEST_VERSION,
    MIGRATION_LOCK_NAME,
    MIGRATION_LOCK_TIMEOUT_SECONDS,
    MIGRATIONS,
    Database,
    MigrationContext,
    SchemaState,
    backup_directory,
    inspect_schema,
    open_completion_database,
    open_database_safely,
    prepare_database_open,
    render_restore_command,
)
from agentworks.errors import StateError
from tests.conftest import CapturedOutput


def _build_schema(path: Path, target_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    context = MigrationContext()
    for version in range(1, target_version + 1):
        step = MIGRATIONS[version]
        if callable(step):
            step(connection, context)
        else:
            for statement in step.split(";"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.commit()
    connection.close()


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    version = int(connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    connection.close()
    return version


def _serialized_open_worker(
    path: Path,
    plan: object,
    start: Any,
    outcomes: Any,
) -> None:
    from agentworks.db import DatabaseOpenPlan, open_database_safely

    assert isinstance(plan, DatabaseOpenPlan)
    start.wait()
    try:
        result = open_database_safely(path, plan, create_backup=True)
        result.database.close()
        outcomes.put(("ok", result.backup is not None))
    except Exception as error:  # pragma: no cover - returned to parent for assertion
        outcomes.put(("error", f"{type(error).__name__}: {error}"))


def _late_partial_writer(path: Path, start: Any, acquired: Any) -> None:
    from agentworks.db.backup import _acquire_migration_lock, _release_migration_lock

    start.wait()
    lock = _acquire_migration_lock(path, timeout=5.0)
    assert lock is not None
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE late_partial_change (value TEXT)")
    connection.commit()
    connection.close()
    acquired.set()
    time.sleep(0.25)
    _release_migration_lock(lock)


def _unchanged_lock_holder(path: Path, acquired: Any, release: Any) -> None:
    from agentworks.db.backup import _acquire_migration_lock, _release_migration_lock

    lock = _acquire_migration_lock(path, timeout=5.0)
    assert lock is not None
    acquired.set()
    try:
        assert release.wait(timeout=10)
    finally:
        _release_migration_lock(lock)


def _released_partial_writer(path: Path, start: Any, released: Any) -> None:
    from agentworks.db.backup import _acquire_migration_lock, _release_migration_lock

    start.wait()
    lock = _acquire_migration_lock(path, timeout=5.0)
    assert lock is not None
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE released_partial_change (value TEXT)")
    connection.commit()
    connection.close()
    _release_migration_lock(lock)
    released.set()


def _tainted_baseline_writer(path: Path, committed: Any, release: Any, released: Any) -> None:
    from agentworks.db.backup import _acquire_migration_lock, _release_migration_lock

    lock = _acquire_migration_lock(path, timeout=5.0)
    assert lock is not None
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE vms ADD COLUMN cpus INTEGER")
    connection.commit()
    connection.close()
    committed.set()
    assert release.wait(timeout=5)
    _release_migration_lock(lock)
    released.set()


def test_inspection_matrix_is_non_migrating_and_wal_aware(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    stale = tmp_path / "stale.db"
    current = tmp_path / "current.db"
    malformed = tmp_path / "malformed.db"
    _build_schema(stale, LATEST_VERSION - 1)
    Database(current).close()
    malformed.write_text("not sqlite")

    assert inspect_schema(absent).state is SchemaState.ABSENT
    assert inspect_schema(stale).state is SchemaState.STALE
    assert inspect_schema(current).state is SchemaState.CURRENT
    assert inspect_schema(malformed).state is SchemaState.MALFORMED
    assert _version(stale) == LATEST_VERSION - 1

    writer = sqlite3.connect(current)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
    writer.commit()
    assert current.with_name(f"{current.name}-wal").stat().st_size > 0
    assert inspect_schema(current).state is SchemaState.FUTURE
    writer.close()


def test_prepare_qualifies_stale_state_under_restrictive_persistent_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)

    plan = prepare_database_open(path)

    assert plan.inspection.state is SchemaState.STALE
    assert plan.stale_baseline is not None
    lock_path = tmp_path / MIGRATION_LOCK_NAME
    assert lock_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_two_process_safe_open_serializes_with_exactly_one_backup(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    plan = prepare_database_open(path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [context.Process(target=_serialized_open_worker, args=(path, plan, start, outcomes)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = sorted(outcomes.get(timeout=2) for _ in processes)
    assert results == [("ok", False), ("ok", True)]
    assert len(tuple(backup_directory(path).glob("*.db"))) == 1
    assert _version(path) == LATEST_VERSION


def test_prepare_waits_for_unchanged_lock_holder_and_qualifies_stale_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.db import backup as backup_module

    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    expected = inspect_schema(path)
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(target=_unchanged_lock_holder, args=(path, acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=5)
        real_acquire = backup_module._acquire_migration_lock
        acquisition_events: queue.Queue[tuple[str, float]] = queue.Queue()

        def _acquire_after_proving_busy(database_path: Path, *, timeout: float):
            if timeout != MIGRATION_LOCK_TIMEOUT_SECONDS:
                acquisition_events.put(("unexpected timeout", timeout))
                raise AssertionError(f"unexpected migration lock timeout: {timeout}")
            assert real_acquire(database_path, timeout=0.0) is None
            acquisition_events.put(("busy", timeout))
            return real_acquire(database_path, timeout=timeout)

        monkeypatch.setattr(backup_module, "_acquire_migration_lock", _acquire_after_proving_busy)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending_plan = executor.submit(prepare_database_open, path)
            assert acquisition_events.get(timeout=5) == ("busy", MIGRATION_LOCK_TIMEOUT_SECONDS)
            assert not pending_plan.done()
            release.set()
            plan = pending_plan.result(timeout=10)
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert holder.exitcode == 0
    assert plan.inspection == expected
    assert plan.stale_baseline == (expected.current_version, expected.schema_cookie)
    assert not backup_directory(path).exists()


def test_stale_inspector_waits_then_refuses_changed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.db import backup as backup_module

    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    acquired = context.Event()
    writer = context.Process(target=_late_partial_writer, args=(path, start, acquired))
    writer.start()
    real_inspect = backup_module.inspect_schema
    calls = 0

    def _inspect_then_wait(database_path: Path, *, immutable: bool = False):
        nonlocal calls
        result = real_inspect(database_path, immutable=immutable)
        calls += 1
        if calls == 1:
            start.set()
            assert acquired.wait(timeout=5)
        return result

    monkeypatch.setattr(backup_module, "inspect_schema", _inspect_then_wait)

    with pytest.raises(StateError, match="changed before its stale state could be qualified"):
        prepare_database_open(path)

    writer.join(timeout=10)
    assert writer.exitcode == 0
    assert _version(path) == LATEST_VERSION - 1
    assert not backup_directory(path).exists()


def test_prepare_refuses_changed_stale_state_when_lock_released_before_first_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import backup as backup_module

    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    released = context.Event()
    writer = context.Process(target=_released_partial_writer, args=(path, start, released))
    writer.start()
    real_inspect = backup_module.inspect_schema
    calls = 0

    def _inspect_then_release(database_path: Path, *, immutable: bool = False):
        nonlocal calls
        result = real_inspect(database_path, immutable=immutable)
        calls += 1
        if calls == 1:
            start.set()
            assert released.wait(timeout=5)
        return result

    monkeypatch.setattr(backup_module, "inspect_schema", _inspect_then_release)

    with pytest.raises(StateError, match="changed before its stale state could be qualified") as raised:
        prepare_database_open(path)

    writer.join(timeout=10)
    assert writer.exitcode == 0
    assert raised.value.hint == "Inspect the database with `agw doctor`, then retry the original command."
    assert _version(path) == LATEST_VERSION - 1
    assert not backup_directory(path).exists()


def test_prepare_refuses_identical_tainted_baseline_after_lock_released_before_first_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import backup as backup_module

    path = tmp_path / "state.db"
    _build_schema(path, 1)
    context = multiprocessing.get_context("spawn")
    committed = context.Event()
    release = context.Event()
    released = context.Event()
    writer = context.Process(target=_tainted_baseline_writer, args=(path, committed, release, released))
    writer.start()
    assert committed.wait(timeout=5)
    real_inspect = backup_module.inspect_schema
    observed_tokens: list[tuple[int, int | None]] = []

    def _inspect_then_release(database_path: Path, *, immutable: bool = False):
        result = real_inspect(database_path, immutable=immutable)
        observed_tokens.append((result.current_version, result.schema_cookie))
        if len(observed_tokens) == 1:
            release.set()
            assert released.wait(timeout=5)
        return result

    monkeypatch.setattr(backup_module, "inspect_schema", _inspect_then_release)

    with pytest.raises(StateError, match="unexpected columns for completed schema version 1: cpus") as raised:
        prepare_database_open(path)

    writer.join(timeout=10)
    assert writer.exitcode == 0
    assert len(observed_tokens) == 2
    assert observed_tokens[0] == observed_tokens[1]
    assert raised.value.hint == (
        "Restore an unmodified backup captured after a completed Agentworks migration, "
        "or repair the partial schema before retrying."
    )
    assert _version(path) == 1
    assert not backup_directory(path).exists()


def test_safe_open_completes_backup_before_database_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    plan = prepare_database_open(path)
    observed: list[Path] = []

    class _ConstructionWitness:
        def __init__(self, _path: Path) -> None:
            backups = tuple(backup_directory(path).glob("*.db"))
            assert len(backups) == 1
            assert _version(backups[0]) == LATEST_VERSION - 1
            observed.extend(backups)

    monkeypatch.setattr("agentworks.db.database.Database", _ConstructionWitness)

    result = open_database_safely(path, plan, create_backup=True)

    assert result.backup is not None
    assert observed == [result.backup.path]


def test_safe_open_migrates_stale_state_and_preserves_historical_backup(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)

    result = open_database_safely(path, prepare_database_open(path), create_backup=True)
    result.database.close()

    assert result.backup is not None
    assert _version(result.backup.path) == LATEST_VERSION - 1
    assert _version(path) == LATEST_VERSION


@pytest.mark.parametrize("shape", ["empty-file", "empty-version-table"])
def test_get_db_initializes_version_zero_without_interaction_or_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    shape: str,
) -> None:
    import agentworks.config as config_module
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers

    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    if shape == "empty-version-table":
        connection.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
        )
        connection.commit()
    connection.close()
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    monkeypatch.setattr(
        config_module,
        "load_database_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("focused config loaded")),
    )

    database = _helpers.get_db()
    database.close()

    assert _version(path) == LATEST_VERSION
    assert not backup_directory(path).exists()
    assert captured_output.notices == []


def test_safe_open_refuses_changed_but_still_stale_state(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    plan = prepare_database_open(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE interaction_change (value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="changed during migration interaction"):
        open_database_safely(path, plan, create_backup=True)

    assert not backup_directory(path).exists()
    assert _version(path) == LATEST_VERSION - 1


@pytest.mark.parametrize("kind", ["future", "malformed"])
def test_prepare_refuses_unopenable_state_before_writable_open(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "state.db"
    if kind == "future":
        Database(path).close()
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
        connection.commit()
        connection.close()
        match = "newer than"
    else:
        path.write_text("not sqlite")
        match = "malformed"

    with pytest.raises(StateError, match=match):
        prepare_database_open(path)

    assert not (tmp_path / MIGRATION_LOCK_NAME).exists()


def test_partial_migration_failure_reports_exact_backup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    monkeypatch.setitem(
        MIGRATIONS,
        LATEST_VERSION,
        "CREATE TABLE partial_failure_witness (value TEXT); SELECT * FROM missing_table",
    )

    with pytest.raises(StateError, match="migration failed") as raised:
        open_database_safely(path, prepare_database_open(path), create_backup=True)

    backups = tuple(backup_directory(path).glob("*.db"))
    assert len(backups) == 1
    assert raised.value.hint == f"Restore the pre-migration backup with: {render_restore_command(backups[0])}"
    assert _version(backups[0]) == LATEST_VERSION - 1


def test_partial_migration_failure_without_backup_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    monkeypatch.setitem(MIGRATIONS, LATEST_VERSION, "SELECT * FROM missing_table")

    with pytest.raises(StateError, match="migration failed") as raised:
        open_database_safely(path, prepare_database_open(path), create_backup=False)

    assert raised.value.hint is not None
    assert "No pre-migration backup was created" in raised.value.hint
    assert not backup_directory(path).exists()


@pytest.mark.parametrize("create_backup", [True, False])
def test_base_exception_migration_failure_preserves_identity_and_visible_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    create_backup: bool,
) -> None:
    import agentworks.config as config_module
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers
    from agentworks.db.backup import _acquire_migration_lock, _release_migration_lock

    path = tmp_path / "state.db"
    config_path = tmp_path / "config.toml"
    _build_schema(path, LATEST_VERSION - 1)
    if not create_backup:
        config_path.write_text("[database]\nauto_backup_before_migration = false\n")
    interruption = KeyboardInterrupt("operator interrupted")

    def _interrupt_after_ddl(connection: sqlite3.Connection, _context: MigrationContext) -> None:
        connection.execute("CREATE TABLE interrupted_partial_change (value TEXT)")
        raise interruption

    monkeypatch.setitem(MIGRATIONS, LATEST_VERSION, _interrupt_after_ddl)
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(output, "is_interactive", lambda: False)

    with pytest.raises(KeyboardInterrupt) as raised:
        _helpers.get_db()

    assert raised.value is interruption
    backups = tuple(backup_directory(path).glob("*.db"))
    if create_backup:
        assert len(backups) == 1
        recovery = f"Restore the pre-migration backup with: {render_restore_command(backups[0])}"
    else:
        assert backups == ()
        recovery = "No pre-migration backup was created. Repair or restore the database before retrying."
    assert captured_output.notices[-1] == f"Agentworks migration recovery: {recovery}"

    lock = _acquire_migration_lock(path, timeout=0.0)
    assert lock is not None
    _release_migration_lock(lock)


def test_completion_open_is_immutable_and_refuses_every_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()

    completion_database = open_completion_database(path)
    assert completion_database is not None
    completion_database.close()
    pristine = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()}

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        sidecar.write_bytes(b"sentinel")
        before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()}
        assert open_completion_database(path) is None
        assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()} == before
        sidecar.unlink()

    assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()} == pristine


def test_completion_probe_unavailable_state_fails_before_database_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentworks.db as db_module
    from agentworks.cli import _helpers

    path = tmp_path / "state.db"
    Database(path).close()
    path.with_name(f"{path.name}-journal").write_bytes(b"sentinel")
    before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()}
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(_helpers, "completion_mode_enabled", lambda: True)
    monkeypatch.setattr(
        Database,
        "list_vms",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("database caller ran")),
    )

    with pytest.raises(StateError, match="completion is unavailable"):
        _helpers.get_db()

    assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()} == before


def test_recovery_command_rendering_quotes_posix_and_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home with space"
    backup = home / "database-backups" / "operator's snapshot.db"
    backup.parent.mkdir(parents=True)
    backup.touch()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert render_restore_command(backup, platform="linux") == (
        "agw database restore $HOME/" + "'database-backups/operator'\"'\"'s snapshot.db'"
    )
    assert render_restore_command(backup, platform="win32") == (
        "agw database restore (Join-Path $HOME 'database-backups\\operator''s snapshot.db')"
    )


def test_database_refuses_future_schema_without_advancing_it(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="newer than"):
        Database(path)

    assert _version(path) == LATEST_VERSION + 1


def test_database_closes_connection_when_migration_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def _tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = cast("sqlite3.Connection", real_connect(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
    monkeypatch.setattr(Database, "_migrate", lambda _self: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        Database(tmp_path / "state.db")

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_production_has_one_writable_database_construction_site() -> None:
    package = Path(__file__).parents[1] / "agentworks"
    writable_calls: list[tuple[Path, int]] = []
    for source_path in package.rglob("*.py"):
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "Database":
                continue
            read_only = next((keyword.value for keyword in node.keywords if keyword.arg == "read_only"), None)
            if isinstance(read_only, ast.Constant) and read_only.value is True:
                continue
            writable_calls.append((source_path.relative_to(package), node.lineno))

    assert [path for path, _line in writable_calls] == [Path("db/backup.py")]


class _TerminalPromptStream:
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("answer", "backup_count"), [(True, 1), (False, 0)])
def test_get_db_interactive_default_yes_selection_and_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    answer: bool,
    backup_count: int,
) -> None:
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers

    path = tmp_path / "state.db"
    _build_schema(path, LATEST_VERSION - 1)
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "agentworks.cli._helpers.sys",
        SimpleNamespace(stderr=_TerminalPromptStream()),
    )
    captured_output.confirm_response = answer

    database = _helpers.get_db()
    database.close()

    assert _version(path) == LATEST_VERSION
    backups = tuple(backup_directory(path).glob("*.db"))
    assert len(backups) == backup_count
    assert any(f"version {LATEST_VERSION - 1} to {LATEST_VERSION}" in message for message in captured_output.notices)
    assert len(captured_output.notices) == (2 if answer else 1)
    if answer:
        from agentworks.path_rendering import format_host_path

        assert captured_output.notices[-1] == (
            f"Pre-migration database backup completed: {format_host_path(backups[0])}"
        )


@pytest.mark.parametrize(("setting", "backup_count"), [(None, 1), (False, 0)])
def test_get_db_noninteractive_uses_focused_default_or_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    setting: bool | None,
    backup_count: int,
) -> None:
    import agentworks.config as config_module
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers

    path = tmp_path / "state.db"
    config_path = tmp_path / "config.toml"
    _build_schema(path, LATEST_VERSION - 1)
    if setting is not None:
        config_path.write_text(f"[database]\nauto_backup_before_migration = {str(setting).lower()}\n")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full config loaded")),
    )

    database = _helpers.get_db()
    database.close()

    assert _version(path) == LATEST_VERSION
    assert len(tuple(backup_directory(path).glob("*.db"))) == backup_count
    assert captured_output.notices


def test_get_db_invalid_focused_config_stops_before_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.config as config_module
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers
    from agentworks.errors import ConfigError

    path = tmp_path / "state.db"
    config_path = tmp_path / "config.toml"
    _build_schema(path, LATEST_VERSION - 1)
    config_path.write_text("[database]\nauto_backup_before_migration = 'sometimes'\n")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(output, "is_interactive", lambda: False)

    with pytest.raises(ConfigError, match="must be a boolean"):
        _helpers.get_db()

    assert _version(path) == LATEST_VERSION - 1
    assert not backup_directory(path).exists()


@pytest.mark.parametrize("interactive", [True, False])
def test_get_db_selected_backup_failure_prevents_migration_and_guides_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    interactive: bool,
) -> None:
    import agentworks.config as config_module
    import agentworks.db as db_module
    from agentworks import output
    from agentworks.cli import _helpers
    from agentworks.errors import BackupError

    path = tmp_path / "state.db"
    config_path = tmp_path / "config.toml"
    _build_schema(path, LATEST_VERSION - 1)
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(output, "is_interactive", lambda: interactive)
    monkeypatch.setattr(
        "agentworks.cli._helpers.sys",
        SimpleNamespace(stderr=_TerminalPromptStream()),
    )
    monkeypatch.setattr(
        "agentworks.db.backup.create_pre_migration_backup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("snapshot unavailable")),
    )
    captured_output.confirm_response = True

    with pytest.raises(BackupError, match="snapshot unavailable") as raised:
        _helpers.get_db()

    assert raised.value.hint is not None
    if interactive:
        assert "answer no" in raised.value.hint
    else:
        assert "auto_backup_before_migration = false" in raised.value.hint
    assert "Migration did not start" in raised.value.hint
    assert _version(path) == LATEST_VERSION - 1


def test_notice_survives_machine_presentation_suppression(captured_output: CapturedOutput) -> None:
    from agentworks import output

    with output.suppress_presentation():
        output.info("hidden")
        output.notice("required")

    assert captured_output.info == []
    assert captured_output.notices == ["required"]
