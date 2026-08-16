"""Read-only database transaction contract."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError

if TYPE_CHECKING:
    from pathlib import Path


def _initialized_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    Database(path).close()
    return path


def test_read_transaction_retains_first_read_snapshot(tmp_path: Path) -> None:
    path = _initialized_path(tmp_path)
    reader = Database(path, read_only=True)
    writer = Database(path)
    try:
        with reader.read_transaction():
            assert reader.list_vms() == []
            writer.insert_vm("new-vm", site="local", hostname="new-vm")
            assert reader.list_vms() == []

        assert [vm.name for vm in reader.list_vms()] == ["new-vm"]
    finally:
        reader.close()
        writer.close()


def test_read_transaction_cleans_up_after_exception_and_database_closes(tmp_path: Path) -> None:
    path = _initialized_path(tmp_path)
    reader = Database(path, read_only=True)

    with pytest.raises(RuntimeError), reader.read_transaction():
        assert reader.list_vms() == []
        raise RuntimeError("stop")

    with reader.read_transaction():
        assert reader.list_vms() == []

    reader.close()
    with pytest.raises(sqlite3.ProgrammingError):
        reader.list_vms()


def test_read_transaction_rejects_mode_misuse_and_nesting(tmp_path: Path) -> None:
    path = _initialized_path(tmp_path)
    writable = Database(path)
    reader = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as read_on_writable, writable.read_transaction():
            pass
        assert read_on_writable.value.entity_kind == "database"

        with pytest.raises(StateError) as write_on_read_only, reader.transaction():
            pass
        assert write_on_read_only.value.entity_kind == "database"

        with reader.read_transaction():
            with pytest.raises(StateError) as nested, reader.read_transaction():
                pass
            assert nested.value.entity_kind == "database"
            assert reader.list_vms() == []
    finally:
        writable.close()
        reader.close()


def test_sqlite_rejects_write_inside_read_transaction(tmp_path: Path) -> None:
    path = _initialized_path(tmp_path)
    reader = Database(path, read_only=True)
    try:
        with reader.read_transaction(), pytest.raises(sqlite3.OperationalError):
            reader.insert_vm("forbidden", site="local", hostname="forbidden")
        assert reader.list_vms() == []
    finally:
        reader.close()
