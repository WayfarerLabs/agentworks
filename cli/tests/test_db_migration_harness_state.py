"""Historical migration v29 (the original harness-state blob)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentworks.db import MIGRATIONS, MigrationContext

if TYPE_CHECKING:
    from pathlib import Path


def _build_db_at_version(path: str, target_version: int) -> None:
    """Run migrations through ``target_version`` into a fresh database."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    version    INTEGER NOT NULL,"
        "    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        ")"
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    context = MigrationContext(legacy={})
    for version in range(1, target_version + 1):
        step = MIGRATIONS[version]
        if callable(step):
            step(conn, context)
        else:
            for stmt in step.split(";"):
                if statement := stmt.strip():
                    conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
    conn.close()


def test_v29_adds_historical_column_and_backfills_existing_rows(tmp_path: Path) -> None:
    """Pin v29 as immutable history; v31 owns the later rename."""
    db_path = tmp_path / "m29.db"
    _build_db_at_version(str(db_path), 28)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
    conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    conn.execute("INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')")

    step = MIGRATIONS[29]
    assert isinstance(step, str)
    for stmt in step.split(";"):
        if statement := stmt.strip():
            conn.execute(statement)

    info = {row[1]: row for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "harness_state" in info
    assert info["harness_state"][3] == 1  # NOT NULL
    assert conn.execute("SELECT harness_state FROM sessions WHERE name = 's1'").fetchone()[0] == "{}"
    conn.close()
