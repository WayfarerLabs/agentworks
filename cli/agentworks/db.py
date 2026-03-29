"""SQLite state database for Agentworks.

Database lives at ~/.config/agentworks/agentworks.db. Created automatically on
first use. Schema migrations are forward-only via a version table.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agentworks.config import CONFIG_DIR

if TYPE_CHECKING:
    from pathlib import Path

DB_PATH = CONFIG_DIR / "agentworks.db"


class ProvisioningStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class InitStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    DEALLOCATED = "deallocated"
    UNKNOWN = "unknown"


class TaskMode(Enum):
    ADMIN = "admin"
    AGENT = "agent"


class TaskStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"


# -- Row types -------------------------------------------------------------


@dataclass
class VMHostRow:
    name: str
    ssh_host: str
    platform: str
    os: str | None
    created_at: str
    last_seen_at: str | None


@dataclass
class VMRow:
    name: str
    platform: str
    vm_host_name: str | None
    template: str | None
    extra_packages: list[str]
    provisioning_status: str
    init_status: str
    tailscale_host: str | None
    azure_resource_id: str | None
    wsl_distro_name: str | None
    cpus: int | None
    memory_gib: int | None
    disk_gib: int | None
    swap_gib: int | None
    admin_username: str
    created_at: str
    last_seen_at: str | None


@dataclass
class VMEventRow:
    id: int
    vm_name: str
    event: str
    detail: str | None
    created_at: str


@dataclass
class WorkspaceRow:
    name: str
    type: str
    vm_name: str | None
    template: str | None
    workspace_path: str
    created_at: str
    last_seen_at: str | None


@dataclass
class AgentRow:
    name: str
    workspace_name: str
    linux_user: str
    created_at: str


@dataclass
class TaskRow:
    name: str
    workspace_name: str
    template: str
    mode: str
    linux_user: str
    status: str
    created_at: str
    updated_at: str


# -- Migrations ------------------------------------------------------------

MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE vm_hosts (
            name         TEXT PRIMARY KEY,
            ssh_host     TEXT NOT NULL,
            platform     TEXT NOT NULL DEFAULT 'lima',
            os           TEXT,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen_at TEXT
        );

        CREATE TABLE vms (
            name              TEXT PRIMARY KEY,
            platform          TEXT NOT NULL,
            vm_host_name      TEXT,
            extra_packages    TEXT,
            init_status       TEXT NOT NULL DEFAULT 'pending',
            ssh_public_key    TEXT,
            tailscale_host    TEXT,
            azure_resource_id TEXT,
            wsl_distro_name   TEXT,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen_at      TEXT,
            FOREIGN KEY (vm_host_name) REFERENCES vm_hosts(name)
        );

        CREATE TABLE workspaces (
            name           TEXT PRIMARY KEY,
            type           TEXT NOT NULL,
            vm_name        TEXT,
            template       TEXT,
            workspace_path TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen_at   TEXT,
            FOREIGN KEY (vm_name) REFERENCES vms(name)
        );

        CREATE TABLE vm_git_host_keys (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_name       TEXT NOT NULL,
            git_host_name TEXT NOT NULL,
            remote_key_id TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (vm_name) REFERENCES vms(name),
            UNIQUE (vm_name, git_host_name)
        );
    """,
    2: """
        ALTER TABLE vms ADD COLUMN cpus INTEGER;
        ALTER TABLE vms ADD COLUMN memory_gib INTEGER;
        ALTER TABLE vms ADD COLUMN disk_gib INTEGER;
    """,
    3: """
        ALTER TABLE vms ADD COLUMN vm_user TEXT NOT NULL DEFAULT 'agentworks';
    """,
    4: """
        CREATE TABLE agents (
            name           TEXT NOT NULL,
            workspace_name TEXT NOT NULL,
            linux_user     TEXT NOT NULL UNIQUE,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (workspace_name, name),
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name)
        );
    """,
    5: """
        DROP TABLE IF EXISTS vm_git_host_keys;
    """,
    6: """
        ALTER TABLE vms ADD COLUMN provisioning_status TEXT NOT NULL DEFAULT 'pending';

        -- Migrate existing init_status values to the two-column model.
        -- Use tailscale_host presence to distinguish provisioning vs init failures.
        UPDATE vms SET provisioning_status = CASE
            WHEN init_status = 'pending' THEN 'pending'
            WHEN init_status = 'bootstrapping' THEN 'in_progress'
            WHEN init_status IN ('tailscale_up', 'initializing', 'complete', 'partial') THEN 'complete'
            WHEN init_status = 'failed' AND tailscale_host IS NOT NULL THEN 'complete'
            WHEN init_status = 'failed' AND tailscale_host IS NULL THEN 'failed'
            ELSE 'pending'
        END;

        UPDATE vms SET init_status = CASE
            WHEN init_status = 'pending' THEN 'pending'
            WHEN init_status = 'bootstrapping' THEN 'pending'
            WHEN init_status = 'tailscale_up' THEN 'pending'
            WHEN init_status = 'initializing' THEN 'in_progress'
            WHEN init_status = 'complete' THEN 'complete'
            WHEN init_status = 'partial' THEN 'partial'
            WHEN init_status = 'failed' AND tailscale_host IS NOT NULL THEN 'failed'
            WHEN init_status = 'failed' AND tailscale_host IS NULL THEN 'pending'
            ELSE 'pending'
        END;

        CREATE TABLE vm_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_name    TEXT NOT NULL,
            event      TEXT NOT NULL,
            detail     TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (vm_name) REFERENCES vms(name)
        );

        CREATE INDEX idx_vm_events_vm_name ON vm_events(vm_name);
    """,
    7: """
        ALTER TABLE vms RENAME COLUMN vm_user TO admin_username;
    """,
    8: """
        CREATE TABLE tasks (
            name           TEXT NOT NULL,
            workspace_name TEXT NOT NULL,
            template       TEXT NOT NULL,
            mode           TEXT NOT NULL DEFAULT 'admin',
            linux_user     TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'running',
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (workspace_name, name),
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name)
        );
    """,
    9: """
        UPDATE workspaces SET template = 'default' WHERE template = '(built-in)';
    """,
    10: """
        ALTER TABLE vms ADD COLUMN swap_gib INTEGER;
        UPDATE vms SET swap_gib = 0;
    """,
    11: """
        ALTER TABLE vms ADD COLUMN template TEXT;
        UPDATE vms SET template = 'default';
    """,
}

LATEST_VERSION = max(MIGRATIONS)


# -- Database class --------------------------------------------------------


class Database:
    """Typed interface to the Agentworks state database."""

    def __init__(self, path: Path | None = None) -> None:
        db_path = path or DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def check_schema(path: Path | None = None) -> tuple[bool, int, int]:
        """Check DB schema version without migrating.

        Returns (exists, current_version, latest_version).
        """
        db_path = path or DB_PATH
        if not db_path.exists():
            return (False, 0, LATEST_VERSION)
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
        except sqlite3.OperationalError:
            current = 0
        finally:
            conn.close()
        return (True, current, LATEST_VERSION)

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version    INTEGER NOT NULL,"
            "    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
            ")"
        )
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0

        for version in range(current + 1, LATEST_VERSION + 1):
            for stmt in MIGRATIONS[version].split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._conn.execute(stmt)
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        self._conn.commit()

    # -- VM Hosts ----------------------------------------------------------

    def insert_vm_host(self, name: str, ssh_host: str, platform: str = "lima", os: str | None = None) -> VMHostRow:
        self._conn.execute(
            "INSERT INTO vm_hosts (name, ssh_host, platform, os) VALUES (?, ?, ?, ?)",
            (name, ssh_host, platform, os),
        )
        self._conn.commit()
        result = self.get_vm_host(name)
        assert result is not None
        return result

    def get_vm_host(self, name: str) -> VMHostRow | None:
        row = self._conn.execute("SELECT * FROM vm_hosts WHERE name = ?", (name,)).fetchone()
        return _to_vm_host(row) if row else None

    def list_vm_hosts(self) -> list[VMHostRow]:
        rows = self._conn.execute("SELECT * FROM vm_hosts ORDER BY name").fetchall()
        return [_to_vm_host(r) for r in rows]

    def update_vm_host_os(self, name: str, os: str) -> None:
        self._conn.execute("UPDATE vm_hosts SET os = ? WHERE name = ?", (os, name))
        self._conn.commit()

    def update_vm_host_last_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE vm_hosts SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_vm_host(self, name: str) -> None:
        self._conn.execute("DELETE FROM vm_hosts WHERE name = ?", (name,))
        self._conn.commit()

    def count_vms_on_host(self, vm_host_name: str) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM vms WHERE vm_host_name = ?", (vm_host_name,)).fetchone()
        return int(row[0])

    # -- VMs ---------------------------------------------------------------

    def insert_vm(
        self,
        name: str,
        platform: str,
        vm_host_name: str | None = None,
        template: str | None = None,
        azure_resource_id: str | None = None,
        wsl_distro_name: str | None = None,
        cpus: int | None = None,
        memory_gib: int | None = None,
        disk_gib: int | None = None,
        swap_gib: int | None = None,
        admin_username: str = "agentworks",
    ) -> VMRow:
        self._conn.execute(
            "INSERT INTO vms "
            "(name, platform, vm_host_name, template, azure_resource_id, wsl_distro_name, "
            "cpus, memory_gib, disk_gib, swap_gib, admin_username) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                platform,
                vm_host_name,
                template,
                azure_resource_id,
                wsl_distro_name,
                cpus,
                memory_gib,
                disk_gib,
                swap_gib,
                admin_username,
            ),
        )
        self._conn.commit()
        result = self.get_vm(name)
        assert result is not None
        return result

    def get_vm(self, name: str) -> VMRow | None:
        row = self._conn.execute("SELECT * FROM vms WHERE name = ?", (name,)).fetchone()
        return _to_vm(row) if row else None

    def list_vms(self) -> list[VMRow]:
        rows = self._conn.execute("SELECT * FROM vms ORDER BY name").fetchall()
        return [_to_vm(r) for r in rows]

    def update_vm_host_ref(self, name: str, vm_host_name: str | None) -> None:
        self._conn.execute("UPDATE vms SET vm_host_name = ? WHERE name = ?", (vm_host_name, name))
        self._conn.commit()

    def update_vm_provisioning_status(self, name: str, status: ProvisioningStatus) -> None:
        self._conn.execute("UPDATE vms SET provisioning_status = ? WHERE name = ?", (status.value, name))
        self._conn.commit()

    def update_vm_init_status(self, name: str, status: InitStatus) -> None:
        self._conn.execute("UPDATE vms SET init_status = ? WHERE name = ?", (status.value, name))
        self._conn.commit()

    def update_vm_tailscale(self, name: str, tailscale_host: str) -> None:
        self._conn.execute("UPDATE vms SET tailscale_host = ? WHERE name = ?", (tailscale_host, name))
        self._conn.commit()

    def clear_vm_tailscale(self, name: str) -> None:
        self._conn.execute("UPDATE vms SET tailscale_host = NULL WHERE name = ?", (name,))
        self._conn.commit()

    def update_vm_azure_resource_id(self, name: str, azure_resource_id: str) -> None:
        self._conn.execute("UPDATE vms SET azure_resource_id = ? WHERE name = ?", (azure_resource_id, name))
        self._conn.commit()

    def update_vm_wsl_distro_name(self, name: str, wsl_distro_name: str) -> None:
        self._conn.execute("UPDATE vms SET wsl_distro_name = ? WHERE name = ?", (wsl_distro_name, name))
        self._conn.commit()

    def update_vm_last_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE vms SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_vm(self, name: str) -> None:
        self._conn.execute(
            "DELETE FROM tasks WHERE workspace_name IN (SELECT name FROM workspaces WHERE vm_name = ?)",
            (name,),
        )
        self._conn.execute(
            "DELETE FROM agents WHERE workspace_name IN (SELECT name FROM workspaces WHERE vm_name = ?)",
            (name,),
        )
        self._conn.execute("DELETE FROM workspaces WHERE vm_name = ?", (name,))
        self._conn.execute("DELETE FROM vm_events WHERE vm_name = ?", (name,))
        self._conn.execute("DELETE FROM vms WHERE name = ?", (name,))
        self._conn.commit()

    # -- Workspaces --------------------------------------------------------

    def insert_workspace(
        self,
        name: str,
        ws_type: str,
        workspace_path: str,
        vm_name: str | None = None,
        template: str | None = None,
    ) -> WorkspaceRow:
        self._conn.execute(
            "INSERT INTO workspaces (name, type, vm_name, template, workspace_path) VALUES (?, ?, ?, ?, ?)",
            (name, ws_type, vm_name, template, workspace_path),
        )
        self._conn.commit()
        result = self.get_workspace(name)
        assert result is not None
        return result

    def get_workspace(self, name: str) -> WorkspaceRow | None:
        row = self._conn.execute("SELECT * FROM workspaces WHERE name = ?", (name,)).fetchone()
        return _to_workspace(row) if row else None

    def list_workspaces(self, vm_name: str | None = None, ws_type: str | None = None) -> list[WorkspaceRow]:
        query = "SELECT * FROM workspaces"
        params: list[str] = []
        conditions: list[str] = []

        if vm_name is not None:
            conditions.append("vm_name = ?")
            params.append(vm_name)
        if ws_type is not None:
            conditions.append("type = ?")
            params.append(ws_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY name"

        rows = self._conn.execute(query, params).fetchall()
        return [_to_workspace(r) for r in rows]

    def update_workspace_last_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE workspaces SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_workspace(self, name: str) -> None:
        self._conn.execute("DELETE FROM tasks WHERE workspace_name = ?", (name,))
        self._conn.execute("DELETE FROM agents WHERE workspace_name = ?", (name,))
        self._conn.execute("DELETE FROM workspaces WHERE name = ?", (name,))
        self._conn.commit()

    def count_workspaces_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM workspaces WHERE vm_name = ?", (vm_name,)).fetchone()
        return int(row[0])

    def count_agents_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM agents WHERE workspace_name IN "
            "(SELECT name FROM workspaces WHERE vm_name = ?)",
            (vm_name,),
        ).fetchone()
        return int(row[0])

    def count_tasks_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workspace_name IN "
            "(SELECT name FROM workspaces WHERE vm_name = ?)",
            (vm_name,),
        ).fetchone()
        return int(row[0])

    # -- Agents ------------------------------------------------------------

    def insert_agent(self, name: str, workspace_name: str, linux_user: str) -> AgentRow:
        self._conn.execute(
            "INSERT INTO agents (name, workspace_name, linux_user) VALUES (?, ?, ?)",
            (name, workspace_name, linux_user),
        )
        self._conn.commit()
        result = self.get_agent(workspace_name, name)
        assert result is not None
        return result

    def get_agent(self, workspace_name: str, name: str) -> AgentRow | None:
        row = self._conn.execute(
            "SELECT * FROM agents WHERE workspace_name = ? AND name = ?",
            (workspace_name, name),
        ).fetchone()
        return _to_agent(row) if row else None

    def list_agents(self, workspace_name: str | None = None) -> list[AgentRow]:
        if workspace_name is not None:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE workspace_name = ? ORDER BY name",
                (workspace_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY workspace_name, name",
            ).fetchall()
        return [_to_agent(r) for r in rows]

    def delete_agent(self, workspace_name: str, name: str) -> None:
        self._conn.execute(
            "DELETE FROM agents WHERE workspace_name = ? AND name = ?",
            (workspace_name, name),
        )
        self._conn.commit()

    def delete_agents_for_workspace(self, workspace_name: str) -> list[AgentRow]:
        """Delete all agents for a workspace, returning the deleted agents."""
        agents = self.list_agents(workspace_name=workspace_name)
        self._conn.execute(
            "DELETE FROM agents WHERE workspace_name = ?",
            (workspace_name,),
        )
        self._conn.commit()
        return agents

    # -- Tasks -------------------------------------------------------------

    def insert_task(
        self,
        name: str,
        workspace_name: str,
        template: str,
        mode: TaskMode,
        linux_user: str,
    ) -> TaskRow:
        self._conn.execute(
            "INSERT INTO tasks (name, workspace_name, template, mode, linux_user) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, workspace_name, template, mode.value, linux_user),
        )
        self._conn.commit()
        result = self.get_task(workspace_name, name)
        assert result is not None
        return result

    def get_task(self, workspace_name: str, name: str) -> TaskRow | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE workspace_name = ? AND name = ?",
            (workspace_name, name),
        ).fetchone()
        return _to_task(row) if row else None

    def list_tasks(self, workspace_name: str | None = None) -> list[TaskRow]:
        if workspace_name is not None:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE workspace_name = ? ORDER BY name",
                (workspace_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY workspace_name, name",
            ).fetchall()
        return [_to_task(r) for r in rows]

    def update_task_status(self, workspace_name: str, name: str, status: TaskStatus) -> None:
        self._conn.execute(
            "UPDATE tasks SET status = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE workspace_name = ? AND name = ?",
            (status.value, workspace_name, name),
        )
        self._conn.commit()

    def delete_task(self, workspace_name: str, name: str) -> None:
        self._conn.execute(
            "DELETE FROM tasks WHERE workspace_name = ? AND name = ?",
            (workspace_name, name),
        )
        self._conn.commit()

    def delete_tasks_for_workspace(self, workspace_name: str) -> list[TaskRow]:
        """Delete all tasks for a workspace, returning the deleted tasks."""
        tasks = self.list_tasks(workspace_name=workspace_name)
        self._conn.execute(
            "DELETE FROM tasks WHERE workspace_name = ?",
            (workspace_name,),
        )
        self._conn.commit()
        return tasks

    # -- VM Events ---------------------------------------------------------

    def insert_vm_event(self, vm_name: str, event: str, detail: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO vm_events (vm_name, event, detail) VALUES (?, ?, ?)",
            (vm_name, event, detail),
        )
        self._conn.commit()

    def list_vm_events(self, vm_name: str) -> list[VMEventRow]:
        rows = self._conn.execute(
            "SELECT * FROM vm_events WHERE vm_name = ? ORDER BY id",
            (vm_name,),
        ).fetchall()
        return [_to_vm_event(r) for r in rows]


# -- Row converters --------------------------------------------------------


def _to_vm_host(row: sqlite3.Row) -> VMHostRow:
    return VMHostRow(
        name=row["name"],
        ssh_host=row["ssh_host"],
        platform=row["platform"],
        os=row["os"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_vm(row: sqlite3.Row) -> VMRow:
    extra = row["extra_packages"]
    return VMRow(
        name=row["name"],
        platform=row["platform"],
        vm_host_name=row["vm_host_name"],
        template=row["template"],
        extra_packages=json.loads(extra) if extra else [],
        provisioning_status=row["provisioning_status"],
        init_status=row["init_status"],
        tailscale_host=row["tailscale_host"],
        azure_resource_id=row["azure_resource_id"],
        wsl_distro_name=row["wsl_distro_name"],
        cpus=row["cpus"],
        memory_gib=row["memory_gib"],
        disk_gib=row["disk_gib"],
        swap_gib=row["swap_gib"],
        admin_username=row["admin_username"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_workspace(row: sqlite3.Row) -> WorkspaceRow:
    return WorkspaceRow(
        name=row["name"],
        type=row["type"],
        vm_name=row["vm_name"],
        template=row["template"],
        workspace_path=row["workspace_path"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_agent(row: sqlite3.Row) -> AgentRow:
    return AgentRow(
        name=row["name"],
        workspace_name=row["workspace_name"],
        linux_user=row["linux_user"],
        created_at=row["created_at"],
    )


def _to_task(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        name=row["name"],
        workspace_name=row["workspace_name"],
        template=row["template"],
        mode=row["mode"],
        linux_user=row["linux_user"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_vm_event(row: sqlite3.Row) -> VMEventRow:
    return VMEventRow(
        id=row["id"],
        vm_name=row["vm_name"],
        event=row["event"],
        detail=row["detail"],
        created_at=row["created_at"],
    )
