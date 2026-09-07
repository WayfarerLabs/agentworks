"""Focused contracts for direct SQLite backup and restore."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.db import (
    BACKUP_DEADLINE_SECONDS,
    LATEST_VERSION,
    MIGRATIONS,
    SCHEMA_SENTINELS,
    Database,
    MigrationContext,
    backup_directory,
    create_manual_backup,
    create_pre_migration_backup,
    prepare_restore,
    restore_backup,
    validate_restore_source,
)
from agentworks.errors import BackupError, NotFoundError, StateError, ValidationError


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
    _build_schema(path, version)

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
    connection.close()

    assert validate_restore_source(path) == version


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


@pytest.mark.parametrize("version", [1, LATEST_VERSION])
def test_restore_refuses_declared_foreign_key_violations_before_destination_mutation(
    tmp_path: Path,
    version: int,
) -> None:
    source = tmp_path / f"v{version}-orphan.db"
    live = tmp_path / "live.db"
    _build_schema(source, version)
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
    _build_schema(backup, 1)
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
    _build_schema(partial, 1)
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
    _build_schema(partial, 3)
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
    _build_schema(incomplete, 2)
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


def test_restore_rejects_identical_paths(tmp_path: Path) -> None:
    path = tmp_path / "same.db"
    Database(path).close()

    with pytest.raises(ValidationError, match="must be different"):
        restore_backup(path, path)


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
    _build_schema(source, 12)

    with pytest.raises(StateError, match="expected 11, found 12"):
        create_pre_migration_backup(source, 11)

    assert not backup_directory(source).exists()
    backup = create_pre_migration_backup(source, 12).path
    assert backup.name.endswith("-v12.db")


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
        _build_schema(source, version)
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
    _build_schema(final_source, 4)
    final = create_pre_migration_backup(final_source, 4)

    assert not created[0].exists()
    assert not created[1].exists()
    assert all(path.exists() for path in created[2:])
    assert final.path.exists()
    assert manual.exists() and unrelated.exists() and lookalike.exists()
    assert final.cleanup_failures == ()


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
    monkeypatch.setattr("agentworks.db.backup._online_copy_from_connection", fail_copy)
    with pytest.raises(BackupError, match="forced"):
        restore_backup(selected, absent)
    assert not absent.exists()
    writer = sqlite3.connect(selected, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.rollback()
    writer.close()


@pytest.mark.parametrize("operation", ["manual", "automatic", "absent-restore"])
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

    copy_target = (
        "agentworks.db.backup._online_copy_from_connection"
        if operation == "absent-restore"
        else "agentworks.db.backup._online_copy"
    )
    monkeypatch.setattr(copy_target, interrupt_copy)

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


def test_restore_held_destination_lock_honors_fixed_deadline(tmp_path: Path) -> None:
    source = tmp_path / "selected.db"
    live = tmp_path / "live.db"
    Database(source).close()
    Database(live).close()
    blocker = sqlite3.connect(live)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(BackupError, match="within 5 seconds"):
            restore_backup(source, live)
    finally:
        blocker.rollback()
        blocker.close()
    elapsed = time.monotonic() - started
    assert 4.5 <= elapsed < 8
