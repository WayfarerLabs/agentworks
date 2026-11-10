"""Runtime intent persistence, atomic migration, and replacement protection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentworks.db import (
    LATEST_VERSION,
    ConsoleRow,
    Database,
    SessionMode,
    SessionRow,
    VMRow,
    create_manual_backup,
    restore_backup,
    validate_restore_source,
)
from agentworks.db import (
    DesiredRuntimeState as Target,
)
from agentworks.errors import StateError
from tests.database_support import build_schema


def _family(db: Database):
    vm = db.insert_vm("box", "local", "box")
    db.insert_workspace("work", workspace_path="/work", vm_name="box", linux_group="ws-work", template="default")
    session = db.insert_session("run", "work", "default", SessionMode.ADMIN, run_id=str(uuid4()))
    console = db.insert_console("view", "box", admin_shell=True)
    db.add_console_session("view", "run", [])
    return vm, session, console


def test_fresh_targets_uuids_and_json_are_stable(db: Database) -> None:
    vm, session, console = _family(db)
    for row in (vm, session, console):
        assert row.desired_runtime_state is Target.RUNNING
        assert json.loads(json.dumps(asdict(row)))["desired_runtime_state"] == "running"
    assert str(UUID(vm.vm_uuid)) == vm.vm_uuid
    assert db.get_vm("box") == vm
    db.set_vm_desired_runtime_state("box", Target.ON_DEMAND, expected_vm_uuid=vm.vm_uuid)
    current = db.get_vm("box")
    assert current is not None and current.vm_uuid == vm.vm_uuid


@pytest.mark.parametrize("kind", ["vm", "session", "console"])
def test_intent_only_writes_and_idempotence(db: Database, kind: str) -> None:
    vm, session, console = _family(db)
    if kind == "vm":

        def read() -> VMRow | SessionRow | ConsoleRow:
            row = db.get_vm("box")
            assert row is not None
            return row

        def write(state: Target) -> None:
            return db.set_vm_desired_runtime_state("box", state, expected_vm_uuid=vm.vm_uuid)
    elif kind == "session":
        db._conn.execute("UPDATE sessions SET updated_at = '2000-01-01', last_started_at = 'start'")
        db._conn.commit()

        def read() -> VMRow | SessionRow | ConsoleRow:
            row = db.get_session("run")
            assert row is not None
            return row

        def write(state: Target) -> None:
            return db.set_session_desired_runtime_state("run", state, expected_session_uuid=session.session_uuid)
    else:
        db._conn.execute("UPDATE consoles SET updated_at = '2000-01-01', last_started_at = 'start'")
        db._conn.commit()

        def read() -> VMRow | SessionRow | ConsoleRow:
            row = db.get_console("view")
            assert row is not None
            return row

        def write(state: Target) -> None:
            return db.set_console_desired_runtime_state("view", state)

    before = asdict(read())
    write(Target.RUNNING)
    assert asdict(read()) == before
    write(Target.STOPPED)
    after = asdict(read())
    assert after["desired_runtime_state"] is Target.STOPPED
    assert {k: v for k, v in before.items() if k not in {"updated_at", "desired_runtime_state"}} == {
        k: v for k, v in after.items() if k not in {"updated_at", "desired_runtime_state"}
    }
    if kind != "vm":
        assert after["updated_at"] != before["updated_at"]
    with pytest.raises(RuntimeError), db.transaction():
        write(Target.RUNNING)
        raise RuntimeError("abort")
    assert asdict(read()) == after


def test_expected_identity_refuses_replaced_or_deleted_rows(db: Database) -> None:
    vm, session, _ = _family(db)
    db.delete_session("run")
    db.insert_session("run", "work", "default", SessionMode.ADMIN)
    with pytest.raises(StateError):
        db.set_session_desired_runtime_state("run", Target.STOPPED, expected_session_uuid=session.session_uuid)
    db._conn.execute("UPDATE vms SET vm_uuid = ?", (str(uuid4()),))
    db._conn.commit()
    with pytest.raises(StateError):
        db.set_vm_desired_runtime_state("box", Target.STOPPED, expected_vm_uuid=vm.vm_uuid)
    with pytest.raises(StateError):
        db.set_console_desired_runtime_state("gone", Target.RUNNING)


@pytest.mark.parametrize("kind", ["vm", "session", "console"])
@pytest.mark.parametrize("raw", [None, "unknown", "sensitive-value", 1])
def test_database_input_rejects_invalid_target_without_echo(db: Database, kind: str, raw: object) -> None:
    vm, session, _ = _family(db)
    with pytest.raises(StateError) as error:
        if kind == "vm":
            db.set_vm_desired_runtime_state("box", raw, expected_vm_uuid=vm.vm_uuid)
        elif kind == "session":
            db.set_session_desired_runtime_state("run", raw, expected_session_uuid=session.session_uuid)
        else:
            db.set_console_desired_runtime_state("view", raw)
    if raw == "sensitive-value":
        assert raw not in str(error.value)


@pytest.mark.parametrize("table", ["sessions", "consoles"])
def test_workloads_reject_on_demand_in_sql_and_boundary(db: Database, table: str) -> None:
    _, session, _ = _family(db)
    with pytest.raises(StateError):
        if table == "sessions":
            db.set_session_desired_runtime_state("run", Target.ON_DEMAND, expected_session_uuid=session.session_uuid)
        else:
            db.set_console_desired_runtime_state("view", Target.ON_DEMAND)
    with pytest.raises(sqlite3.IntegrityError), db._conn:
        db._conn.execute(f"UPDATE {table} SET desired_runtime_state = 'on-demand'")
    db._conn.execute("PRAGMA ignore_check_constraints = ON")
    db._conn.execute(f"UPDATE {table} SET desired_runtime_state = 'on-demand'")
    db._conn.commit()
    with pytest.raises(StateError):
        db.list_sessions() if table == "sessions" else db.list_consoles()


def _legacy(path: Path) -> dict[str, list[dict]]:
    build_schema(path, 38)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        for name, stopped in [("box", 0), ("manual", 1)]:
            conn.execute(
                "INSERT INTO vms (name, site, hostname, operator_stopped, template, admin_template, "
                "extra_packages, provisioning_status, init_status, ssh_public_key, tailscale_host, "
                "cpus, memory_gib, disk_gib, swap_gib, admin_username, platform_metadata, created_at, "
                "last_seen_at, debian_release, debian_release_observed_at, last_started_at) "
                "VALUES (?, 'local', 'host', ?, 'template', 'admin', '[\"git\"]', 'complete', 'complete', "
                "'ssh-key', 'tail', 4, 8, 30, 2, 'operator', '{\"id\":\"opaque\"}', 'created', "
                "'seen', 'trixie', 'release', 'started')",
                (name, stopped),
            )
        conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('work','box','/work','ws-work')"
        )
        conn.execute(
            "INSERT INTO sessions (name,workspace_name,template,mode,session_uuid,run_id,socket_path,pid,boot_id,"
            "tmux_server_start_ticks,harness_integration_state,last_started_at) "
            "VALUES ('run','work','default','agent',?,?,'/sock',42,'boot',99,"
            '\'{"shell":{"id":"opaque"}}\',\'started\')',
            (str(uuid4()), str(uuid4())),
        )
        conn.execute("INSERT INTO consoles (name,vm_name,admin_shell) VALUES ('view','box',1)")
        conn.execute("INSERT INTO console_sessions VALUES ('view','run',0,'[{\"cwd\":null,\"admin\":true}]')")
        conn.execute("INSERT INTO vm_events(vm_name,event,detail) VALUES ('box','create','details')")
        return {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("vms", "sessions", "consoles", "console_sessions", "workspaces", "vm_events")
        }


def test_populated_migration_preserves_facts_relationships_and_restore(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    before = _legacy(path)
    database = Database(path)
    assert [r.desired_runtime_state for r in database.list_vms()] == [Target.ON_DEMAND, Target.STOPPED]
    migrated_session = database.get_session("run")
    migrated_console = database.get_console("view")
    assert migrated_session is not None and migrated_session.desired_runtime_state is None
    assert migrated_console is not None and migrated_console.desired_runtime_state is None
    identities = [r.vm_uuid for r in database.list_vms()]
    assert len(set(identities)) == 2
    database.close()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        for table, original in before.items():
            after = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            assert [{k: v for k, v in r.items() if k not in {"vm_uuid", "desired_runtime_state"}} for r in after] == [
                {k: v for k, v in r.items() if k != "operator_stopped"} for r in original
            ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    backup = create_manual_backup(path)
    assert validate_restore_source(backup) == LATEST_VERSION
    restored_path = tmp_path / "restored.db"
    restore_backup(backup, restored_path)
    restored = Database(restored_path)
    assert [r.vm_uuid for r in restored.list_vms()] == identities
    restored_session = restored.get_session("run")
    assert restored_session is not None and restored_session.session_uuid == before["sessions"][0]["session_uuid"]
    assert restored.list_console_sessions("view")[0].position == 0
    restored.close()


@pytest.mark.parametrize("failure", ["uuid", "flag", "foreign-key", "checkpoint"])
def test_failed_migration_rolls_back_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    path = tmp_path / "legacy.db"
    _legacy(path)
    if failure == "flag":
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE vms SET operator_stopped = 'invalid'")
    elif failure == "foreign-key":
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE workspaces SET vm_name = 'missing'")
    elif failure == "checkpoint":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_version BEFORE INSERT ON schema_version "
                "WHEN NEW.version = 39 BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
            )
    with monkeypatch.context() as patch:
        if failure == "uuid":

            def interrupted():
                raise RuntimeError("interrupted")

            patch.setattr("agentworks.db.migrations.uuid4", interrupted)
        with pytest.raises((StateError, RuntimeError, sqlite3.IntegrityError)):
            Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 38
        assert "operator_stopped" in {r[1] for r in conn.execute("PRAGMA table_info(vms)")}
        assert "desired_runtime_state" not in {r[1] for r in conn.execute("PRAGMA table_info(consoles)")}
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'vms_new'").fetchone() is None
        if failure == "flag":
            conn.execute("UPDATE vms SET operator_stopped = 0")
        elif failure == "foreign-key":
            conn.execute("UPDATE workspaces SET vm_name = 'box'")
        elif failure == "checkpoint":
            conn.execute("DROP TRIGGER reject_version")
    repaired = Database(path)
    assert len(repaired.list_vms()) == 2
    repaired.close()


@pytest.mark.parametrize("raw", ["", "invalid", "sensitive-value"])
def test_malformed_stored_vm_uuid_is_not_reminted(db: Database, raw: str) -> None:
    db.insert_vm("box", "local", "box")
    db._conn.execute("UPDATE vms SET vm_uuid = ?", (raw,))
    db._conn.commit()
    with pytest.raises(StateError) as error:
        db.get_vm("box")
    assert "sensitive-value" not in str(error.value)
    assert db._conn.execute("SELECT vm_uuid FROM vms").fetchone()[0] == raw


def test_vm_uuid_unique_and_null_target_refused(db: Database) -> None:
    vm = db.insert_vm("box", "local", "box", desired_runtime_state=Target.ON_DEMAND)
    assert vm.desired_runtime_state is Target.ON_DEMAND
    with pytest.raises(sqlite3.IntegrityError), db._conn:
        db.insert_vm("replacement", "local", "replacement", vm_uuid=vm.vm_uuid)
    with pytest.raises(sqlite3.IntegrityError), db._conn:
        db._conn.execute("UPDATE vms SET desired_runtime_state = NULL")
    db._conn.execute("PRAGMA ignore_check_constraints = ON")
    db._conn.execute("UPDATE vms SET desired_runtime_state = 'sensitive-value'")
    db._conn.commit()
    with pytest.raises(StateError) as error:
        db.get_vm("box")
    assert "sensitive-value" not in str(error.value)


@pytest.mark.parametrize("column", ["vm_uuid", "desired_runtime_state"])
def test_restore_requires_new_vm_fields(tmp_path: Path, column: str) -> None:
    path = tmp_path / "broken.db"
    build_schema(path, LATEST_VERSION)
    with sqlite3.connect(path) as conn:
        conn.execute(f"ALTER TABLE vms RENAME COLUMN {column} TO wrong_column")
    with pytest.raises(StateError):
        validate_restore_source(path)
