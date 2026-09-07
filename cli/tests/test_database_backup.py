"""Focused contracts for direct SQLite backup and restore."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.db import (
    BACKUP_DEADLINE_SECONDS,
    FOREIGN_KEY_SENTINELS,
    LATEST_VERSION,
    SCHEMA_SENTINELS,
    Database,
    backup_directory,
    create_manual_backup,
    create_pre_migration_backup,
    prepare_restore,
    restore_backup,
    validate_restore_source,
)
from agentworks.errors import BackupError, BusyStateError, NotFoundError, StateError, ValidationError
from tests.database_support import build_schema


def _setting(path: Path, key: str) -> str | None:
    connection = sqlite3.connect(path)
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    connection.close()
    return None if row is None else str(row[0])


def _seed_orphan_workspace(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    available = {str(row[1]) for row in connection.execute("PRAGMA table_info(workspaces)")}
    if "type" in available:
        connection.execute(
            "INSERT INTO workspaces (name, type, vm_name, workspace_path) VALUES (?, ?, ?, ?)",
            ("orphan-workspace", "vm", "missing-vm", "/tmp/orphan-workspace"),
        )
    else:
        assert "linux_group" in available
        connection.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
            ("orphan-workspace", "missing-vm", "/tmp/orphan-workspace", "ws--orphan-workspace"),
        )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None
    connection.close()


@pytest.mark.parametrize("version", range(1, LATEST_VERSION + 1))
def test_schema_sentinels_match_every_historical_version(tmp_path: Path, version: int) -> None:
    path = tmp_path / f"v{version}.db"
    build_schema(path, version)

    connection = sqlite3.connect(path)
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = SCHEMA_SENTINELS[version]
    assert tables == set(expected)
    for table, columns in expected.items():
        actual = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
        assert actual == columns
        actual_foreign_keys = {
            (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]))
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        assert actual_foreign_keys == FOREIGN_KEY_SENTINELS[version].get(table, frozenset())
    connection.close()

    assert validate_restore_source(path) == version


@pytest.mark.windows
def test_manual_backup_reads_committed_wal_content(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    database = Database(source)
    database.close()

    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO settings (key, value) VALUES ('wal-only', 'present')")
    writer.commit()
    assert source.with_name(f"{source.name}-wal").stat().st_size > 0

    backup = create_manual_backup(source)
    assert _setting(backup, "wal-only") == "present"
    writer.close()


@pytest.mark.windows
def test_restore_copies_backup_into_live_database_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "selected.db"
    live = tmp_path / "live.db"
    for path, value in ((source, "selected"), (live, "live")):
        database = Database(path)
        database.set_setting("direction", value)
        database.close()

    restore_backup(source, live)

    assert _setting(source, "direction") == "selected"
    assert _setting(live, "direction") == "selected"


@pytest.mark.windows
@pytest.mark.parametrize("version", [1, LATEST_VERSION])
def test_restore_refuses_declared_foreign_key_violations_before_destination_mutation(
    tmp_path: Path,
    version: int,
) -> None:
    source = tmp_path / f"v{version}-orphan.db"
    live = tmp_path / "live.db"
    build_schema(source, version)
    _seed_orphan_workspace(source)
    live_database = Database(live)
    live_database.set_setting("restore-witness", "preserved")
    live_database.close()

    with pytest.raises(StateError) as validation_error:
        validate_restore_source(source)
    assert validation_error.value.entity_kind == "database"

    with pytest.raises(StateError) as restore_error:
        restore_backup(source, live)
    assert restore_error.value.entity_kind == "database"
    assert _setting(live, "restore-witness") == "preserved"


@pytest.mark.windows
def test_forced_prepared_restore_reports_and_copies_foreign_key_violations(tmp_path: Path) -> None:
    source = tmp_path / "orphan.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    _seed_orphan_workspace(source)

    with prepare_restore(source, live, allow_foreign_key_violations=True) as prepared:
        assert prepared.inspection.schema_version == LATEST_VERSION
        assert prepared.inspection.has_foreign_key_violations is True
        assert prepared.backup_path == source.resolve()
        assert prepared.database_path == live.resolve()
        prepared.apply()

    restored = sqlite3.connect(live)
    assert restored.execute("PRAGMA foreign_key_check").fetchone() is not None
    restored.close()


@pytest.mark.windows
@pytest.mark.parametrize("invalid_kind", ["malformed", "future", "schema-shape"])
def test_force_does_not_bypass_other_restore_validation(tmp_path: Path, invalid_kind: str) -> None:
    source = tmp_path / f"{invalid_kind}.db"
    live = tmp_path / "live.db"
    if invalid_kind == "malformed":
        source.write_text("not sqlite")
    else:
        Database(source).close()
        connection = sqlite3.connect(source)
        if invalid_kind == "future":
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
        else:
            connection.execute("ALTER TABLE settings ADD COLUMN unexpected TEXT")
        connection.commit()
        connection.close()

    with pytest.raises(StateError):
        prepare_restore(source, live, allow_foreign_key_violations=True)
    assert not live.exists()


@pytest.mark.windows
def test_force_does_not_bypass_missing_foreign_key_declaration(tmp_path: Path) -> None:
    source = tmp_path / "missing-foreign-key.db"
    live = tmp_path / "live.db"
    Database(source).close()
    live_database = Database(live)
    live_database.set_setting("restore-witness", "preserved")
    live_database.close()

    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "CREATE TABLE workspaces_new ("
        "name TEXT PRIMARY KEY, vm_name TEXT NOT NULL, template TEXT, "
        "workspace_path TEXT NOT NULL, linux_group TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    connection.execute("INSERT INTO workspaces_new SELECT * FROM workspaces")
    connection.execute("DROP TABLE workspaces")
    connection.execute("ALTER TABLE workspaces_new RENAME TO workspaces")
    connection.commit()
    connection.close()

    with pytest.raises(StateError):
        prepare_restore(source, live, allow_foreign_key_violations=True)

    assert _setting(live, "restore-witness") == "preserved"


@pytest.mark.windows
def test_prepared_restore_copies_the_snapshot_pinned_before_a_later_commit(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()
    _setting_connection = sqlite3.connect(source)
    _setting_connection.execute("INSERT INTO settings (key, value) VALUES ('snapshot', 'inspected')")
    _setting_connection.commit()
    _setting_connection.close()

    with prepare_restore(source, live) as prepared:
        writer = sqlite3.connect(source)
        writer.execute("UPDATE settings SET value = 'later' WHERE key = 'snapshot'")
        writer.commit()
        writer.close()
        prepared.apply()

    assert _setting(source, "snapshot") == "later"
    assert _setting(live, "snapshot") == "inspected"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not replace an open SQLite source path")
def test_prepared_restore_copies_the_open_snapshot_after_source_path_replacement(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    displaced = tmp_path / "displaced.db"
    live = tmp_path / "live.db"
    Database(source).close()
    _set_connection = sqlite3.connect(source)
    _set_connection.execute("PRAGMA journal_mode = DELETE")
    _set_connection.execute("INSERT INTO settings (key, value) VALUES ('snapshot', 'inspected')")
    _set_connection.commit()
    _set_connection.close()

    with prepare_restore(source, live) as prepared:
        source.replace(displaced)
        Database(source).close()
        replacement = sqlite3.connect(source)
        replacement.execute("INSERT INTO settings (key, value) VALUES ('snapshot', 'replacement')")
        replacement.commit()
        replacement.close()
        prepared.apply()

    assert _setting(source, "snapshot") == "replacement"
    assert _setting(live, "snapshot") == "inspected"


@pytest.mark.windows
def test_prepared_restore_context_closes_its_pinned_read_transaction(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()
    mode = sqlite3.connect(source)
    mode.execute("PRAGMA journal_mode = DELETE")
    mode.close()

    with prepare_restore(source, live):
        blocked = sqlite3.connect(source, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError):
                blocked.execute("BEGIN EXCLUSIVE")
        finally:
            blocked.close()

    writer = sqlite3.connect(source, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.rollback()
    writer.close()


@pytest.mark.windows
def test_prepared_restore_does_not_reserve_an_absent_destination_before_apply(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "new" / "live.db"
    Database(source).close()

    with prepare_restore(source, live):
        assert not live.exists()

    assert not live.exists()


@pytest.mark.windows
def test_declined_restore_does_not_remove_a_destination_created_after_preparation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()

    with prepare_restore(source, live):
        concurrent = Database(live)
        concurrent.set_setting("ownership", "concurrent")
        concurrent.close()

    assert _setting(live, "ownership") == "concurrent"


@pytest.mark.windows
def test_apply_refuses_a_destination_created_after_absent_preparation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()

    with prepare_restore(source, live) as prepared:
        concurrent = Database(live)
        concurrent.set_setting("ownership", "concurrent")
        concurrent.close()
        with pytest.raises(BackupError):
            prepared.apply()

    assert _setting(live, "ownership") == "concurrent"


@pytest.mark.windows
def test_interrupted_restore_staging_removes_new_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "new" / "live.db"
    Database(source).close()
    interruption = KeyboardInterrupt("destination preparation interrupted")
    monkeypatch.setattr(
        "agentworks.db.backup._require_restore_stage",
        lambda *_args: (_ for _ in ()).throw(interruption),
    )

    with prepare_restore(source, live) as prepared, pytest.raises(KeyboardInterrupt) as raised:
        prepared.apply()

    assert raised.value is interruption
    assert not live.exists()
    assert list(live.parent.iterdir()) == []


@pytest.mark.windows
def test_restore_stage_closes_descriptor_before_failure_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    real_mkstemp = tempfile.mkstemp
    real_remove = backup_module._remove_incomplete_if_same
    descriptor: int | None = None

    def remember_descriptor(*, prefix: str, suffix: str, dir: str | os.PathLike[str]) -> tuple[int, str]:
        nonlocal descriptor
        descriptor, path = real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        return descriptor, path

    def require_closed_then_remove(path: Path, path_identity: tuple[int, int]) -> None:
        assert descriptor is not None
        with pytest.raises(OSError):
            os.fstat(descriptor)
        real_remove(path, path_identity)

    monkeypatch.setattr(tempfile, "mkstemp", remember_descriptor)
    monkeypatch.setattr(
        backup_module,
        "_connect_restore_destination",
        lambda _path: (_ for _ in ()).throw(OSError("SQLite bind failed")),
    )
    monkeypatch.setattr(backup_module, "_remove_incomplete_if_same", require_closed_then_remove)

    with pytest.raises(BackupError):
        backup_module._create_restore_stage(tmp_path / "live.db")

    assert list(tmp_path.iterdir()) == []


def test_interrupted_database_use_lock_acquisition_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    interruption = KeyboardInterrupt("lock acquisition interrupted")

    class InterruptingConnection:
        calls = 0
        closed = False

        def execute(self, _statement: str) -> InterruptingConnection:
            self.calls += 1
            if self.calls == 2:
                raise interruption
            return self

        def fetchone(self) -> tuple[int]:
            return (0,)

        def close(self) -> None:
            self.closed = True

    connection = InterruptingConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(KeyboardInterrupt) as raised:
        backup_module._acquire_database_use_lock(
            tmp_path / "live.db",
            exclusive=True,
            timeout=0.2,
        )

    assert raised.value is interruption
    assert connection.closed is True


def test_interrupted_migration_lock_acquisition_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    interruption = KeyboardInterrupt("lock acquisition interrupted")

    class InterruptingConnection:
        closed = False

        def execute(self, _statement: str) -> None:
            raise interruption

        def close(self) -> None:
            self.closed = True

    connection = InterruptingConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(KeyboardInterrupt) as raised:
        backup_module._acquire_migration_lock(tmp_path / "live.db", timeout=0.2)

    assert raised.value is interruption
    assert connection.closed is True


@pytest.mark.skipif(os.name == "nt", reason="Windows does not replace an open SQLite destination path")
def test_prepared_restore_refuses_destination_path_replacement(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    displaced = tmp_path / "displaced.db"
    unrelated = tmp_path / "unrelated.db"
    for path, value in ((source, "selected"), (live, "live"), (unrelated, "unrelated")):
        database = Database(path)
        database.set_setting("identity", value)
        database.close()

    with prepare_restore(source, live) as prepared:
        live.replace(displaced)
        live.symlink_to(unrelated)
        with pytest.raises(BackupError):
            prepared.apply()

    assert _setting(displaced, "identity") == "live"
    assert _setting(unrelated, "identity") == "unrelated"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not replace an open destination path")
def test_destination_replacement_during_sqlite_bind_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    real_connect = backup_module._connect_restore_destination

    def replace_before_sqlite_bind(path: Path) -> sqlite3.Connection:
        path.unlink()
        replacement = Database(path)
        replacement.set_setting("identity", "replacement")
        replacement.close()
        return real_connect(path)

    monkeypatch.setattr(backup_module, "_connect_restore_destination", replace_before_sqlite_bind)

    with pytest.raises(BackupError):
        prepare_restore(source, live)

    assert _setting(live, "identity") == "replacement"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not replace an open destination path")
def test_destination_replacement_after_pre_copy_check_does_not_receive_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    displaced = tmp_path / "displaced.db"
    replacement = tmp_path / "replacement.db"
    for path, value in ((source, "source"), (live, "live"), (replacement, "replacement")):
        database = Database(path)
        database.set_setting("identity", value)
        database.close()
    real_copy = backup_module._online_copy_to_connection

    def replace_before_copy(source_connection: sqlite3.Connection, destination: sqlite3.Connection) -> None:
        live.replace(displaced)
        replacement.replace(live)
        real_copy(source_connection, destination)

    with prepare_restore(source, live) as prepared:
        monkeypatch.setattr(backup_module, "_online_copy_to_connection", replace_before_copy)
        with pytest.raises(BackupError):
            prepared.apply()

    assert _setting(displaced, "identity") == "live"
    assert _setting(live, "identity") == "replacement"


@pytest.mark.windows
def test_restore_refuses_existing_destination_with_active_wal_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentworks.db.backup.BACKUP_DEADLINE_SECONDS", 0.2)
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    source_database = Database(source)
    source_database.set_setting("identity", "source")
    source_database.close()
    live_database = Database(live)
    live_database.set_setting("identity", "live-initial")
    live_database.close()

    reader = sqlite3.connect(live)
    reader.execute("BEGIN")
    reader.execute("SELECT value FROM settings WHERE key = 'identity'").fetchone()
    writer = sqlite3.connect(live)
    writer.execute("UPDATE settings SET value = 'live-wal' WHERE key = 'identity'")
    writer.commit()
    writer.close()
    try:
        with pytest.raises(BackupError):
            restore_backup(source, live)
        assert _setting(live, "identity") == "live-wal"
    finally:
        reader.rollback()
        reader.close()


@pytest.mark.windows
def test_restore_excludes_agentworks_database_open_through_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DEADLINE_SECONDS", 0.2)
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    source_database = Database(source)
    source_database.set_setting("identity", "source")
    source_database.close()
    live_database = Database(live)
    live_database.set_setting("identity", "live")
    live_database.close()
    real_replace = os.replace

    def attempt_database_open_before_replace(staged: Path, destination: Path) -> None:
        with pytest.raises(BusyStateError):
            Database(live)
        real_replace(staged, destination)

    monkeypatch.setattr(os, "replace", attempt_database_open_before_replace)

    restore_backup(source, live)

    assert _setting(live, "identity") == "source"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires optional privileges")
def test_restore_use_lock_canonicalizes_a_writable_database_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DEADLINE_SECONDS", 0.2)
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    alias = tmp_path / "alias.db"
    source_database = Database(source)
    source_database.set_setting("identity", "source")
    source_database.close()
    live_database = Database(live)
    live_database.set_setting("identity", "live")
    live_database.close()
    alias.symlink_to(live)
    real_replace = os.replace

    def attempt_alias_open_before_replace(staged: Path, destination: Path) -> None:
        with pytest.raises(BusyStateError):
            Database(alias)
        real_replace(staged, destination)

    monkeypatch.setattr(os, "replace", attempt_alias_open_before_replace)

    restore_backup(source, alias)

    assert _setting(alias, "identity") == "source"


@pytest.mark.windows
def test_destination_becoming_a_source_hardlink_during_bind_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    real_open = os.open
    replaced = False

    def replace_before_open(path: Path, flags: int, mode: int = 0o777) -> int:
        nonlocal replaced
        if Path(path) == live and not replaced:
            replaced = True
            live.unlink()
            os.link(source, live)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(ValidationError):
        prepare_restore(source, live)

    assert os.path.samefile(source, live)


def test_restore_validation_closes_source_when_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    source.touch()
    interruption = KeyboardInterrupt("validation interrupted")

    class InterruptingConnection:
        closed = False

        def execute(self, _statement: str) -> None:
            raise interruption

        def close(self) -> None:
            self.closed = True

    connection = InterruptingConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(KeyboardInterrupt) as raised:
        validate_restore_source(source)

    assert raised.value is interruption
    assert connection.closed is True


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="platform has no nonblocking file-open flag")
def test_restore_source_probe_is_nonblocking_and_rejects_non_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    observed_flags: list[int] = []

    def open_non_regular(_path: Path, flags: int) -> int:
        observed_flags.append(flags)
        return 12345

    monkeypatch.setattr(os, "open", open_non_regular)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SQLite opened non-regular source")),
    )

    with pytest.raises(StateError):
        backup_module.validate_restore_source(tmp_path / "source")

    assert observed_flags[0] & os.O_NONBLOCK


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="platform has no nonblocking file-open flag")
def test_restore_destination_probe_is_nonblocking_and_rejects_non_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db.backup as backup_module

    observed_flags: list[int] = []

    def open_non_regular(_path: Path, flags: int) -> int:
        observed_flags.append(flags)
        return 12345

    monkeypatch.setattr(os, "open", open_non_regular)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr(
        backup_module,
        "_connect_restore_destination",
        lambda _path: (_ for _ in ()).throw(AssertionError("SQLite opened non-regular destination")),
    )

    with pytest.raises(BackupError):
        backup_module._prepare_restore_destination(
            tmp_path / "destination",
            source_path_identity=(1, 1),
        )

    assert observed_flags[0] & os.O_NONBLOCK


def test_restore_accepts_final_sqlite_done_after_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "selected.db"
    live = tmp_path / "live.db"
    for path, value in ((source, "selected"), (live, "replace-me")):
        database = Database(path)
        database.set_setting("deadline", value)
        database.close()
    times = iter((0.0, BACKUP_DEADLINE_SECONDS + 1.0))
    monkeypatch.setattr(
        "agentworks.db.backup.time",
        SimpleNamespace(monotonic=lambda: next(times)),
    )

    restore_backup(source, live)

    assert _setting(live, "deadline") == "selected"
    with pytest.raises(StopIteration):
        next(times)


def test_restore_keeps_historical_version_without_migrating(tmp_path: Path) -> None:
    backup = tmp_path / "v1.db"
    live = tmp_path / "live.db"
    build_schema(backup, 1)
    Database(live).close()

    restore_backup(backup, live)

    connection = sqlite3.connect(live)
    assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone() == (1,)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'settings'").fetchone() is None
    connection.close()


def test_restore_rejects_generic_sqlite_before_destination_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generic = tmp_path / "generic.db"
    sqlite3.connect(generic).close()
    live = tmp_path / "live.db"
    monkeypatch.setattr(
        "agentworks.db.backup._online_copy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("destination opened")),
    )

    with pytest.raises(StateError, match="not an Agentworks"):
        restore_backup(generic, live)

    assert not live.exists()


def test_restore_rejects_current_common_sentinel_lookalike(tmp_path: Path) -> None:
    lookalike = tmp_path / "lookalike.db"
    connection = sqlite3.connect(lookalike)
    connection.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    connection.execute("INSERT INTO schema_version VALUES (?, '')", (LATEST_VERSION,))
    connection.execute("CREATE TABLE vms (name TEXT)")
    connection.execute("CREATE TABLE workspaces (name TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="missing required"):
        restore_backup(lookalike, tmp_path / "live.db")


def test_restore_rejects_v1_with_first_committed_v2_ddl_before_destination_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = tmp_path / "partial-v2.db"
    live = tmp_path / "live.db"
    build_schema(partial, 1)
    connection = sqlite3.connect(partial)
    connection.execute("ALTER TABLE vms ADD COLUMN cpus INTEGER")
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        "agentworks.db.backup._online_copy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("destination opened")),
    )

    with pytest.raises(StateError, match="unexpected columns for completed schema version 1: cpus") as raised:
        restore_backup(partial, live)

    assert raised.value.hint == "Select an unmodified backup captured after a completed Agentworks migration."
    assert not live.exists()


def test_restore_rejects_v3_with_first_committed_v4_table_before_destination_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = tmp_path / "partial-v4.db"
    live = tmp_path / "live.db"
    build_schema(partial, 3)
    connection = sqlite3.connect(partial)
    connection.execute(
        "CREATE TABLE agents ("
        "name TEXT NOT NULL, "
        "workspace_name TEXT NOT NULL, "
        "linux_user TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), "
        "PRIMARY KEY (workspace_name, name), "
        "FOREIGN KEY (workspace_name) REFERENCES workspaces(name))"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        "agentworks.db.backup._online_copy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("destination opened")),
    )

    with pytest.raises(StateError, match="unexpected tables for completed schema version 3: agents") as raised:
        restore_backup(partial, live)

    assert raised.value.hint == "Select an unmodified backup captured after a completed Agentworks migration."
    assert not live.exists()


def test_restore_rejects_missing_column_before_destination_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = tmp_path / "incomplete-v2.db"
    live = tmp_path / "live.db"
    build_schema(incomplete, 2)
    connection = sqlite3.connect(incomplete)
    connection.execute("ALTER TABLE vms DROP COLUMN disk_gib")
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        "agentworks.db.backup._online_copy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("destination opened")),
    )

    with pytest.raises(StateError, match="table 'vms' is missing required columns: disk_gib") as raised:
        restore_backup(incomplete, live)

    assert raised.value.hint == "Select an unmodified backup captured after a completed Agentworks migration."
    assert not live.exists()


def test_restore_refuses_future_version_before_destination_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    future = tmp_path / "future.db"
    Database(future).close()
    connection = sqlite3.connect(future)
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
    connection.commit()
    connection.close()
    live = tmp_path / "live.db"
    monkeypatch.setattr(
        "agentworks.db.backup._online_copy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("destination opened")),
    )

    with pytest.raises(StateError, match="newer than"):
        restore_backup(future, live)

    assert not live.exists()


def test_manual_backup_preserves_future_schema(tmp_path: Path) -> None:
    source = tmp_path / "future.db"
    Database(source).close()
    connection = sqlite3.connect(source)
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION + 1,))
    connection.commit()
    connection.close()

    backup = create_manual_backup(source)

    connection = sqlite3.connect(backup)
    assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone() == (LATEST_VERSION + 1,)
    connection.close()


@pytest.mark.windows
def test_restore_rejects_identical_paths(tmp_path: Path) -> None:
    path = tmp_path / "same.db"
    Database(path).close()

    with pytest.raises(ValidationError, match="must be different"):
        restore_backup(path, path)


@pytest.mark.windows
def test_restore_rejects_hardlinked_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    live = tmp_path / "live.db"
    Database(source).close()
    os.link(source, live)

    with pytest.raises(ValidationError):
        restore_backup(source, live)


@pytest.mark.windows
def test_backup_names_are_disjoint_and_collisions_are_reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "live.db"
    Database(source).close()
    monkeypatch.setattr("agentworks.db.backup._utc_timestamp", lambda: "20260810T010203123456Z")

    first = create_manual_backup(source)
    second = create_manual_backup(source)
    automatic = create_pre_migration_backup(source, LATEST_VERSION).path

    assert first.name == "agentworks-manual-20260810T010203123456Z.db"
    assert second.name == "agentworks-manual-20260810T010203123456Z-1.db"
    assert automatic.name == f"agentworks-pre-migration-20260810T010203123456Z-v{LATEST_VERSION}.db"


def test_automatic_backup_refuses_a_version_that_does_not_match_its_source(tmp_path: Path) -> None:
    source = tmp_path / "v12.db"
    build_schema(source, 12)

    with pytest.raises(StateError, match="expected 11, found 12"):
        create_pre_migration_backup(source, 11)

    assert not backup_directory(source).exists()
    backup = create_pre_migration_backup(source, 12).path
    assert backup.name.endswith("-v12.db")


@pytest.mark.windows
def test_automatic_retention_uses_timestamp_and_ignores_manual_and_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamps = iter(
        [
            "20260810T000001000000Z",
            "20260810T000002000000Z",
            "20260810T000003000000Z",
            "20260810T000004000000Z",
            "20260810T000005000000Z",
            "20260810T000006000000Z",
            "20260810T000007000000Z",
        ]
    )
    monkeypatch.setattr("agentworks.db.backup._utc_timestamp", lambda: next(timestamps))
    versions = (LATEST_VERSION, 1, 30, 2, 29, 3)
    sources = []
    for version in versions:
        source = tmp_path / f"v{version}.db"
        build_schema(source, version)
        sources.append(source)
    created = [
        create_pre_migration_backup(source, version).path for source, version in zip(sources, versions, strict=True)
    ]
    directory = backup_directory(sources[0])
    manual = directory / "agentworks-manual-keep.db"
    unrelated = directory / "notes.txt"
    lookalike = directory / "agentworks-pre-migration-not-a-time-v1.db"
    for path in (manual, unrelated, lookalike):
        path.write_text("keep")

    final_source = tmp_path / "v4.db"
    build_schema(final_source, 4)
    final = create_pre_migration_backup(final_source, 4)

    assert not created[0].exists()
    assert not created[1].exists()
    assert all(path.exists() for path in created[2:])
    assert final.path.exists()
    assert manual.exists() and unrelated.exists() and lookalike.exists()
    assert final.cleanup_failures == ()


@pytest.mark.windows
def test_failed_backup_and_absent_restore_remove_incomplete_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    Database(source).close()

    def fail_copy(_source: object, destination: Path) -> None:
        destination.write_bytes(b"partial")
        destination.with_name(f"{destination.name}-wal").write_bytes(b"partial wal")
        destination.with_name(f"{destination.name}-shm").write_bytes(b"partial shm")
        raise BackupError("forced copy failure")

    monkeypatch.setattr("agentworks.db.backup._online_copy", fail_copy)
    with pytest.raises(BackupError, match="forced"):
        create_manual_backup(source)
    assert list(backup_directory(source).iterdir()) == []

    selected = tmp_path / "selected.db"
    Database(selected).close()
    journal = sqlite3.connect(selected)
    journal.execute("PRAGMA journal_mode = DELETE")
    journal.close()
    absent = tmp_path / "new" / "live.db"

    def fail_prepared_copy(_source: object, destination: sqlite3.Connection) -> None:
        destination_path = Path(destination.execute("PRAGMA database_list").fetchone()[2])
        fail_copy(_source, destination_path)

    monkeypatch.setattr("agentworks.db.backup._online_copy_to_connection", fail_prepared_copy)
    with pytest.raises(BackupError, match="forced"):
        restore_backup(selected, absent)
    assert not absent.exists()
    writer = sqlite3.connect(selected, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.rollback()
    writer.close()


@pytest.mark.parametrize("operation", ["manual", "automatic", "absent-restore"])
@pytest.mark.windows
def test_interrupted_copy_preserves_interrupt_and_removes_all_reserved_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = tmp_path / "source.db"
    Database(source).close()
    interruption = KeyboardInterrupt("copy interrupted")
    destinations: list[Path] = []

    def interrupt_copy(_source: object, destination: Path) -> None:
        destinations.append(destination)
        destination.write_bytes(b"partial")
        destination.with_name(f"{destination.name}-wal").write_bytes(b"partial wal")
        destination.with_name(f"{destination.name}-shm").write_bytes(b"partial shm")
        destination.with_name(f"{destination.name}-journal").write_bytes(b"partial journal")
        raise interruption

    if operation == "absent-restore":

        def interrupt_prepared_copy(_source: object, destination: sqlite3.Connection) -> None:
            destination_path = Path(destination.execute("PRAGMA database_list").fetchone()[2])
            interrupt_copy(_source, destination_path)

        monkeypatch.setattr("agentworks.db.backup._online_copy_to_connection", interrupt_prepared_copy)
    else:
        monkeypatch.setattr("agentworks.db.backup._online_copy", interrupt_copy)

    with pytest.raises(KeyboardInterrupt) as raised:
        if operation == "manual":
            create_manual_backup(source)
        elif operation == "automatic":
            create_pre_migration_backup(source, LATEST_VERSION)
        else:
            restore_backup(source, tmp_path / "absent" / "live.db")

    assert raised.value is interruption
    assert len(destinations) == 1
    destination = destinations[0]
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not destination.with_name(f"{destination.name}{suffix}").exists()


def test_retention_cleanup_failure_does_not_discard_completed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    Database(source).close()
    directory = backup_directory(source)
    directory.mkdir()
    old = directory / "agentworks-pre-migration-20260810T000001000000Z-v1.db"
    for index in range(1, 6):
        timestamp = f"20260810T00000{index + 1}000000Z"
        (directory / f"agentworks-pre-migration-{timestamp}-v{index + 1}.db").write_text("recognized")
    old.write_text("recognized")
    monkeypatch.setattr("agentworks.db.backup._utc_timestamp", lambda: "20260810T000007000000Z")
    real_unlink = Path.unlink

    def fail_old(path: Path, missing_ok: bool = False) -> None:
        if path == old:
            raise OSError("retention blocked")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_old)

    result = create_pre_migration_backup(source, LATEST_VERSION)

    assert result.path.exists()
    assert len(result.cleanup_failures) == 1
    assert result.cleanup_failures[0].path == old
    assert result.cleanup_failures[0].message == "retention blocked"


def test_missing_or_malformed_backup_source_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(NotFoundError):
        create_manual_backup(missing)
    assert not backup_directory(missing).exists()

    malformed = tmp_path / "broken.db"
    malformed.write_text("not sqlite")
    with pytest.raises(StateError, match="malformed"):
        create_manual_backup(malformed)
    assert not backup_directory(malformed).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not the Windows security boundary")
def test_new_backup_and_absent_restore_are_user_only(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    Database(source).close()
    backup = create_manual_backup(source)
    directory = backup_directory(source)

    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(backup.stat().st_mode) & 0o077 == 0

    restored = tmp_path / "restored" / "live.db"
    restore_backup(backup, restored)
    assert stat.S_IMODE(restored.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not the Windows security boundary")
def test_restore_preserves_existing_destination_mode(tmp_path: Path) -> None:
    source = tmp_path / "selected.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    live.chmod(0o640)

    restore_backup(source, live)

    assert stat.S_IMODE(live.stat().st_mode) == 0o640


@pytest.mark.windows
def test_restore_held_destination_lock_honors_fixed_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = 0.2
    monkeypatch.setattr("agentworks.db.backup.BACKUP_DEADLINE_SECONDS", deadline)
    source = tmp_path / "selected.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    blocker = sqlite3.connect(live)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(BackupError):
            restore_backup(source, live)
    finally:
        blocker.rollback()
        blocker.close()
    elapsed = time.monotonic() - started
    assert deadline * 0.75 <= elapsed < 2
