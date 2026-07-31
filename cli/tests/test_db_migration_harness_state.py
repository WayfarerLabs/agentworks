"""Migration v29 (the harness-state blob) against the prior schema.

A v28 fixture database with a session row (and no ``harness_state``
column), asserting the additive column lands NOT NULL, existing rows
backfill to ``{}``, and the ``SessionRow`` round-trips the blob through
insert / get / update.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import (
    MIGRATIONS,
    Database,
    MigrationContext,
    SessionMode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import CapturedOutput


def _build_db_at_version(path: str, target_version: int) -> None:
    """Run migrations 1..target_version into a fresh DB exactly as
    ``Database._migrate`` does (the v27 Python step included), stopping
    one short of the harness-state migration under test."""
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
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
    conn.close()


def _seed_admin_session(path: str) -> None:
    """A VM, workspace, and one admin session, valid under the FKs (the
    migration run's foreign_key_check would flag orphans)."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
    conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    conn.execute("INSERT INTO sessions (name, workspace_name, template, mode) VALUES ('s1', 'ws1', 'default', 'admin')")
    conn.commit()
    conn.close()


def test_migration_adds_column_and_backfills_existing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "m29.db"
    _build_db_at_version(str(db_path), 28)
    _seed_admin_session(str(db_path))

    # Opening the DB runs the pending v29 migration (and its per-version
    # foreign_key_check), so a clean open proves the FKs still hold.
    db = Database(db_path)
    try:
        info = {row[1]: row for row in db._conn.execute("PRAGMA table_info(sessions)")}
        assert "harness_state" in info
        assert info["harness_state"][3] == 1  # NOT NULL

        # The pre-existing row backfilled to the empty-object default.
        session = db.get_session("s1")
        assert session is not None
        assert session.harness_state == {}
    finally:
        db.close()


def test_harness_state_round_trips_through_insert_get_update(tmp_path: Path) -> None:
    db = Database(tmp_path / "rt.db")
    try:
        db._conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
        db._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
        )
        db._conn.commit()

        # A value written at insert reads back verbatim.
        row = db.insert_session(
            "s1",
            "ws1",
            "claude",
            SessionMode.ADMIN,
            harness_state={"session_id": "abc-123"},
        )
        assert row.harness_state == {"session_id": "abc-123"}
        assert db.get_session("s1").harness_state == {"session_id": "abc-123"}  # type: ignore[union-attr]

        # A later update (the restart path) replaces it.
        db.update_session_harness_state("s1", {"session_id": "def-456"})
        assert db.get_session("s1").harness_state == {"session_id": "def-456"}  # type: ignore[union-attr]

        # An omitted blob defaults to empty (the shell harness's case).
        db.insert_session("s2", "ws1", "default", SessionMode.ADMIN)
        assert db.get_session("s2").harness_state == {}  # type: ignore[union-attr]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("raw", "detail_fragment"),
    [("not json{", "invalid JSON"), ('["a", "b"]', "expected a JSON object")],
)
def test_malformed_blob_degrades_to_empty_with_a_warning(
    tmp_path: Path,
    captured_output: CapturedOutput,
    raw: str,
    detail_fragment: str,
) -> None:
    """A corrupt harness_state (a future harness bug, a hand-edited DB)
    must not break reads: the row degrades to ``{}`` with a warning rather
    than raising, so one bad row cannot block ``session list`` (which maps
    the row conversion over every session) for all the others."""
    db = Database(tmp_path / "bad.db")
    try:
        db._conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
        db._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
        )
        db.insert_session("good", "ws1", "default", SessionMode.ADMIN)
        db.insert_session("bad", "ws1", "default", SessionMode.ADMIN)
        # Corrupt one row's blob directly, bypassing the JSON writer.
        db._conn.execute("UPDATE sessions SET harness_state = ? WHERE name = 'bad'", (raw,))
        db._conn.commit()

        # The single read degrades, not raises, and warns naming the session.
        assert db.get_session("bad").harness_state == {}  # type: ignore[union-attr]
        assert any("bad" in msg and detail_fragment in msg for msg in captured_output.warnings)

        # And the list read (the real blast radius) survives the bad row.
        listed = {s.name: s for s in db.list_sessions()}
        assert listed["bad"].harness_state == {}
        assert listed["good"].harness_state == {}
    finally:
        db.close()
