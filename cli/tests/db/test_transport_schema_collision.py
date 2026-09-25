"""Refusal for the transport branch's reused pre-release schema versions."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SchemaState, inspect_schema, prepare_database_open
from agentworks.errors import StateError
from tests.database_support import build_schema

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("claimed_version", (39, 40, 41))
def test_reused_schema_number_refuses_before_migration(tmp_path: Path, claimed_version: int) -> None:
    path = tmp_path / "state.db"
    build_schema(path, 38)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (claimed_version,))

    expected = SchemaState.MALFORMED if claimed_version == 41 else SchemaState.STALE
    assert inspect_schema(path).state is expected
    with pytest.raises(StateError):
        prepare_database_open(path)
    with pytest.raises(StateError):
        Database(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone() == (claimed_version,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'operation_owners'").fetchone() is None
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_read_only_open_refuses_reused_current_schema_number(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    build_schema(path, 38)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO schema_version (version) VALUES (41)")

    with pytest.raises(StateError):
        Database(path, read_only=True)


@pytest.mark.parametrize("version", (39, 40, 41))
def test_canonical_transport_schemas_remain_openable(tmp_path: Path, version: int) -> None:
    path = tmp_path / "state.db"
    build_schema(path, version)

    expected = SchemaState.CURRENT if version == 41 else SchemaState.STALE
    assert inspect_schema(path).state is expected
    database = Database(path)
    try:
        assert database._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 41  # noqa: SLF001
    finally:
        database.close()
