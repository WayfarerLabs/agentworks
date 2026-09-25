"""Migration and persistence coverage for VM instance markers."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from tests.database_support import build_schema

if TYPE_CHECKING:
    from pathlib import Path


def test_v40_upgrade_leaves_existing_vm_unadopted(tmp_path: Path) -> None:
    path = tmp_path / "v40.db"
    build_schema(path, 40)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO vms (name, site, hostname, admin_username) VALUES ('legacy', 'local', 'legacy', 'admin')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    vm = database.get_vm("legacy")
    database.close()

    assert vm is not None
    assert vm.instance_marker is None


def test_marker_persistence_accepts_only_canonical_or_null(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    marker = "0123456789abcdef0123456789abcdef"
    vm = database.insert_vm("new", "local", "new", instance_marker=marker)
    assert vm.instance_marker == marker

    with pytest.raises(ValidationError):
        database.insert_vm("bad", "local", "bad", instance_marker="A" * 32)
    database.close()


def test_invalid_persisted_marker_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    database._conn.execute("PRAGMA ignore_check_constraints = ON")
    database._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, instance_marker) "
        "VALUES ('corrupt', 'local', 'corrupt', 'admin', 'invalid')"
    )
    database._conn.commit()

    with pytest.raises(StateError):
        database.get_vm("corrupt")
    database.close()
