"""Durable session identity and independent managed-launch identity."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentworks.db import (
    LATEST_VERSION,
    Database,
    SessionMode,
    create_manual_backup,
    restore_backup,
    validate_restore_source,
)
from agentworks.errors import StateError
from tests.database_support import build_schema


def _parents(db: Database) -> None:
    db.insert_vm("box", site="local", hostname="box")
    db.insert_workspace("work", workspace_path="/work", vm_name="box", linux_group="ws-work", template="default")


def test_session_identity_is_stable_and_reusing_a_name_mints_another(db: Database) -> None:
    _parents(db)
    first = db.insert_session("run", "work", "default", SessionMode.ADMIN)
    assert str(UUID(first.session_uuid)) == first.session_uuid
    assert first.run_id is None
    assert db.get_session("run") == first
    assert db.list_sessions() == [first]
    db.delete_session("run")
    second = db.insert_session("run", "work", "default", SessionMode.ADMIN)
    assert second.session_uuid != first.session_uuid


def test_explicit_prospective_ids_are_preserved_and_session_uuid_is_unique(db: Database) -> None:
    _parents(db)
    session_uuid, run_id = str(uuid4()), str(uuid4())
    row = db.insert_session("first", "work", "default", SessionMode.ADMIN, session_uuid=session_uuid, run_id=run_id)
    assert (row.session_uuid, row.run_id) == (session_uuid, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_session("other", "work", "default", SessionMode.ADMIN, session_uuid=session_uuid)
    assert db.get_session("other") is None


@pytest.mark.parametrize("field", ["session_uuid", "run_id"])
@pytest.mark.parametrize("value", ["", "invalid", "../escape"])
def test_invalid_insert_id_never_writes_a_session(db: Database, field: str, value: str) -> None:
    _parents(db)
    with pytest.raises(ValueError):
        db.insert_session("bad", "work", "default", SessionMode.ADMIN, **{field: value})
    assert db.count_sessions() == 0


def test_recording_prospective_run_does_not_claim_runtime_start(db: Database) -> None:
    _parents(db)
    db.insert_session("run", "work", "default", SessionMode.ADMIN)
    db.update_session_runtime("run", socket_path="/socket", pid=123, boot_id="boot", tmux_server_start_ticks=10)
    db.record_session_started("run")
    before = db.get_session("run")
    assert before is not None
    run_id = str(uuid4())
    db.set_session_run_id("run", run_id)
    after = db.get_session("run")
    assert after is not None
    assert asdict(after) == {**asdict(before), "run_id": run_id}
    with pytest.raises(ValueError):
        db.set_session_run_id("run", "not-a-uuid")
    assert db.get_session("run") == after
    with pytest.raises(StateError):
        db.set_session_run_id("missing", str(uuid4()))


def test_run_identity_write_participates_in_callers_transaction(db: Database) -> None:
    _parents(db)
    original = db.insert_session("run", "work", "default", SessionMode.ADMIN)
    with pytest.raises(RuntimeError), db.transaction():
        db.set_session_run_id("run", str(uuid4()))
        raise RuntimeError("simulate launch cancellation")
    assert db.get_session("run") == original


def test_v37_migration_assigns_ids_once_and_preserves_existing_session_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    build_schema(path, 37)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO vms (name, site, hostname) VALUES ('box', 'local', 'box')")
        connection.execute(
            "INSERT INTO workspaces (name, vm_name, template, workspace_path, linux_group) "
            "VALUES ('work', 'box', 'default', '/work', 'ws-work')"
        )
        for name in ("first", "second"):
            connection.execute(
                "INSERT INTO sessions (name, workspace_name, template, mode, socket_path, pid, boot_id, "
                "tmux_server_start_ticks, last_started_at, harness_integration_state) "
                "VALUES (?, 'work', 'default', 'admin', '/socket', 42, 'boot', 500, '2026-01-01T00:00:00Z', ?) ",
                (name, '{"shell": {"key": "value"}}'),
            )
        connection.execute("INSERT INTO consoles (name, vm_name, admin_shell) VALUES ('view', 'box', 1)")
        connection.execute(
            "INSERT INTO console_sessions (console_name, session_name, position, shells) "
            "VALUES ('view', 'first', 0, '[]')"
        )
        connection.row_factory = sqlite3.Row
        before = [dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY name")]
    db = Database(path)
    rows = db.list_sessions()
    ids = [row.session_uuid for row in rows]
    assert len(set(ids)) == 2
    assert all(str(UUID(value)) == value for value in ids)
    assert all(row.run_id is None for row in rows)
    db.close()
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        after = [dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY name")]
        assert [
            {key: value for key, value in row.items() if key not in {"session_uuid", "run_id"}} for row in after
        ] == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT session_name FROM console_sessions").fetchone()[0] == "first"
    reopened = Database(path)
    assert reopened.list_sessions() == rows
    reopened.close()


def test_backup_restore_preserves_both_identities(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    db = Database(path)
    _parents(db)
    row = db.insert_session("run", "work", "default", SessionMode.ADMIN, run_id=str(uuid4()))
    db.close()
    backup = create_manual_backup(path)
    assert validate_restore_source(backup) == LATEST_VERSION
    restored_path = tmp_path / "restored.db"
    restore_backup(backup, restored_path)
    restored = Database(restored_path)
    assert restored.get_session("run") == row
    restored.close()


@pytest.mark.parametrize("column", ["session_uuid", "run_id"])
def test_backup_column_validation_requires_identity_fields(tmp_path: Path, column: str) -> None:
    path = tmp_path / "broken.db"
    build_schema(path, LATEST_VERSION)
    with sqlite3.connect(path) as connection:
        # Renaming preserves constraints while making the claimed schema incomplete.
        connection.execute(f'ALTER TABLE sessions RENAME COLUMN "{column}" TO "wrong_identity"')
    with pytest.raises(StateError):
        validate_restore_source(path)


def test_corrupt_stored_identity_is_not_reminted(db: Database) -> None:
    _parents(db)
    row = db.insert_session("run", "work", "default", SessionMode.ADMIN)
    with sqlite3.connect(db.path) as connection:
        connection.execute("UPDATE sessions SET session_uuid = 'invalid' WHERE name = 'run'")
    with pytest.raises(StateError):
        db.get_session("run")
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT session_uuid FROM sessions").fetchone()[0] == "invalid"
    assert row.run_id is None


def test_interrupted_identity_migration_rolls_back_its_schema_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "interrupted.db"
    build_schema(path, 37)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO vms (name, site, hostname) VALUES ('box', 'local', 'box')")
        connection.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('work', 'box', '/work', 'ws-work')"
        )
        connection.execute("INSERT INTO sessions (name, workspace_name, template) VALUES ('first', 'work', 'default')")

    def fail_uuid() -> UUID:
        raise RuntimeError("simulate interrupted migration")

    with monkeypatch.context() as patch:
        patch.setattr("agentworks.db.migrations.uuid4", fail_uuid)
        with pytest.raises(RuntimeError):
            Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 37
        assert connection.execute("SELECT name FROM sessions").fetchone()[0] == "first"
        assert "session_uuid" not in {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name = 'sessions_new'").fetchone() is None
    migrated = Database(path)
    row = migrated.get_session("first")
    assert row is not None and UUID(row.session_uuid)
    assert row.run_id is None
    migrated.close()
