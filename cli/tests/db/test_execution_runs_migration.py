"""Migration coverage for private durable managed-run records."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import LATEST_VERSION, Database, open_database_safely, prepare_database_open
from tests.database_support import build_schema

if TYPE_CHECKING:
    from pathlib import Path


def _build_v39_with_session(path: Path) -> str:
    build_schema(path, 39)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm', 'lima-local', 'vm-host', 'admin')"
    )
    connection.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('workspace', 'vm', '/work', 'workspace-group')"
    )
    session_uuid = str(uuid4())
    connection.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, session_uuid) "
        "VALUES ('session', 'workspace', 'default', 'admin', ?)",
        (session_uuid,),
    )
    connection.commit()
    connection.close()
    return session_uuid


def test_v39_migration_adds_empty_execution_runs_without_backfilling_sessions(tmp_path: Path) -> None:
    path = tmp_path / "v39.db"
    session_uuid = _build_v39_with_session(path)

    database = Database(path)
    version = database._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    session = database._conn.execute("SELECT session_uuid, run_id FROM sessions").fetchone()
    run_count = database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    database.close()

    assert version == LATEST_VERSION == 40
    assert tuple(session) == (session_uuid, None)
    assert run_count == 0


def test_pre_migration_backup_and_live_upgrade_fabricate_no_run_records(tmp_path: Path) -> None:
    path = tmp_path / "v39.db"
    session_uuid = _build_v39_with_session(path)

    opened = open_database_safely(path, prepare_database_open(path), create_backup=True)
    assert opened.backup is not None
    live_session = opened.database._conn.execute("SELECT session_uuid, run_id FROM sessions").fetchone()
    live_run_count = opened.database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    opened.database.close()

    backup = sqlite3.connect(opened.backup.path)
    backup_session = backup.execute("SELECT session_uuid, run_id FROM sessions").fetchone()
    backup_run_table = backup.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'execution_runs'"
    ).fetchone()
    backup.close()

    assert tuple(live_session) == (session_uuid, None)
    assert live_run_count == 0
    assert backup_session == (session_uuid, None)
    assert backup_run_table is None
