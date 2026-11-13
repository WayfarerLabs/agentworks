"""Relative workspace paths are stored as the absolute paths they named on the VM."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import Database
from tests.database_support import build_schema

if TYPE_CHECKING:
    from pathlib import Path


def test_migration_to_v40_anchors_a_relative_workspace_path_in_its_admins_home(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    build_schema(path, 39)
    stored = {
        "plain": ("box", "workspaces/plain"),
        "spelled": ("ops-box", "./ws//spelled/"),
        "absolute": ("box", "/srv/ws/absolute"),
        "tilde": ("box", "~/ws/tilde"),
    }
    with sqlite3.connect(path) as connection:
        for vm_name, admin_username in (("box", "agentworks"), ("ops-box", "ops")):
            connection.execute(
                "INSERT INTO vms (name, site, hostname, admin_username, desired_runtime_state, vm_uuid) "
                "VALUES (?, 'local', ?, ?, 'on-demand', ?)",
                (vm_name, vm_name, admin_username, str(uuid4())),
            )
        for name, (vm_name, workspace_path) in stored.items():
            connection.execute(
                "INSERT INTO workspaces (name, vm_name, template, workspace_path, linux_group) "
                "VALUES (?, ?, 'default', ?, ?)",
                (name, vm_name, workspace_path, f"ws-{name}"),
            )

    db = Database(path)
    paths = {row.name: row.workspace_path for row in db.list_workspaces()}
    db.close()

    assert paths == {
        "plain": "/home/agentworks/workspaces/plain",
        "spelled": "/home/ops/ws/spelled",
        "absolute": "/srv/ws/absolute",
        "tilde": "~/ws/tilde",
    }
