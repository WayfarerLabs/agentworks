from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentworks.db import Database
from agentworks.errors import StateError


def test_read_only_database_handles_uri_significant_path_without_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state?#.db"
    writable = Database(path)
    writable.close()

    monkeypatch.setattr(Database, "_migrate", lambda self: pytest.fail("migration invoked"))
    readonly = Database(path, read_only=True)
    try:
        assert readonly.list_vms() == []
    finally:
        readonly.close()


def test_read_only_database_rejects_stale_schema(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (0)")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="outdated"):
        Database(path, read_only=True)


def test_read_only_database_frames_malformed_schema(tmp_path: Path) -> None:
    path = tmp_path / "malformed.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(StateError, match="malformed"):
        Database(path, read_only=True)
