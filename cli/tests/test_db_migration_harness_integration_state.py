"""Migration v31 renames the persisted harness-integration state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from agentworks.db import MIGRATIONS, Database, MigrationContext, SessionMode

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import CapturedOutput


RAW_STATE = '{  "claude-code" : { "session_id" : "abc-123" } }'
DECODED_STATE = {"claude-code": {"session_id": "abc-123"}}


def _build_db_at_version(path: str, target_version: int) -> None:
    """Run migrations through ``target_version`` like ``Database`` does."""
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
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
    conn.close()


def _seed_v30_admin_session(path: Path, raw_state: str = RAW_STATE) -> None:
    """Create a valid v30 session row containing deliberately spaced JSON."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
    conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, harness_state) "
        "VALUES ('s1', 'ws1', 'default', 'admin', ?)",
        (raw_state,),
    )
    conn.commit()
    conn.close()


def _session_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}


def test_v31_renames_column_preserving_raw_and_decoded_state(tmp_path: Path) -> None:
    db_path = tmp_path / "m31.db"
    _build_db_at_version(str(db_path), 30)
    _seed_v30_admin_session(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        before = conn.execute("SELECT harness_state FROM sessions WHERE name = 's1'").fetchone()[0]
        before_bytes = conn.execute("SELECT CAST(harness_state AS BLOB) FROM sessions WHERE name = 's1'").fetchone()[0]
    assert before == RAW_STATE

    db = Database(db_path)
    try:
        column_info = {row[1]: row for row in db._conn.execute("PRAGMA table_info(sessions)")}
        columns = set(column_info)
        assert "harness_integration_state" in columns
        assert "harness_state" not in columns
        assert column_info["harness_integration_state"][3] == 1  # NOT NULL
        assert column_info["harness_integration_state"][4] == "'{}'"
        after = db._conn.execute("SELECT harness_integration_state FROM sessions WHERE name = 's1'").fetchone()[0]
        after_bytes = db._conn.execute(
            "SELECT CAST(harness_integration_state AS BLOB) FROM sessions WHERE name = 's1'"
        ).fetchone()[0]
        assert after == before
        assert after_bytes == before_bytes
        assert db.get_session("s1").harness_integration_state == DECODED_STATE  # type: ignore[union-attr]
        assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []

        inserted = db.insert_session(
            "s2",
            "ws1",
            "claude",
            SessionMode.ADMIN,
            harness_integration_state={"codex": {"session_id": "new-123"}},
        )
        assert inserted.harness_integration_state == {"codex": {"session_id": "new-123"}}
        assert db.get_session("s2").harness_integration_state == {  # type: ignore[union-attr]
            "codex": {"session_id": "new-123"}
        }

        db.update_session_harness_integration_state("s2", {"codex": {"session_id": "updated-456"}})
        assert db.get_session("s2").harness_integration_state == {  # type: ignore[union-attr]
            "codex": {"session_id": "updated-456"}
        }
    finally:
        db.close()


def test_v31_resumes_after_rename_before_version_record(tmp_path: Path) -> None:
    db_path = tmp_path / "interrupted.db"
    _build_db_at_version(str(db_path), 30)
    _seed_v30_admin_session(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 30
    conn.close()

    db = Database(db_path)
    try:
        assert db._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 31
        columns = _session_columns(db._conn)
        assert columns & {"harness_state", "harness_integration_state"} == {"harness_integration_state"}
        raw = db._conn.execute("SELECT harness_integration_state FROM sessions WHERE name = 's1'").fetchone()[0]
        assert raw == RAW_STATE
        assert db.get_session("s1").harness_integration_state == DECODED_STATE  # type: ignore[union-attr]
        assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize(
    "state_columns",
    [
        "",
        ", harness_state TEXT, harness_integration_state TEXT",
    ],
)
def test_v31_rejects_an_unexpected_state_column_set(state_columns: str) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE sessions (name TEXT{state_columns})")
        step = MIGRATIONS[31]
        assert callable(step)

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            step(conn, MigrationContext(legacy={}))

        message = str(exc_info.value)
        assert "harness_state" in message
        assert "harness_integration_state" in message


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
    db = Database(tmp_path / "bad.db")
    try:
        db._conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm1', 'lima', 'h', 'admin')")
        db._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
        )
        db.insert_session("good", "ws1", "default", SessionMode.ADMIN)
        db.insert_session("bad", "ws1", "default", SessionMode.ADMIN)
        db._conn.execute("UPDATE sessions SET harness_integration_state = ? WHERE name = 'bad'", (raw,))
        db._conn.commit()

        assert db.get_session("bad").harness_integration_state == {}  # type: ignore[union-attr]
        assert any("bad" in message and detail_fragment in message for message in captured_output.warnings)
        listed = {session.name: session for session in db.list_sessions()}
        assert listed["bad"].harness_integration_state == {}
        assert listed["good"].harness_integration_state == {}
    finally:
        db.close()
