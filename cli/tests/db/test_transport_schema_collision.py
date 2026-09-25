"""Refusal for the transport branch's reused pre-release schema versions."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import (
    Database,
    SchemaState,
    inspect_schema,
    open_completion_database,
    open_database_safely,
    prepare_database_open,
)
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

    expected = SchemaState.CURRENT if claimed_version == 41 else SchemaState.STALE
    assert inspect_schema(path).state is expected
    if claimed_version == 41:
        plan = prepare_database_open(path)
        with pytest.raises(StateError):
            open_database_safely(path, plan, create_backup=False)
    else:
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


def test_completion_quietly_refuses_lock_acquired_during_schema_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.db import backup as backup_module

    path = tmp_path / "state.db"
    build_schema(path, 41)
    validate = backup_module._validate_consolidated_transport_schema
    calls = 0
    locker: sqlite3.Connection | None = None

    def lock_after_version_read(connection: sqlite3.Connection, version: int) -> None:
        nonlocal calls, locker
        calls += 1
        if calls == 1:
            locker = sqlite3.connect(path)
            locker.execute("BEGIN EXCLUSIVE")
        validate(connection, version)

    monkeypatch.setattr(backup_module, "_validate_consolidated_transport_schema", lock_after_version_read)
    try:
        assert open_completion_database(path) is None
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()
    assert calls == 1


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
