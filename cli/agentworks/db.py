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


class InitStatus(Enum):
    PENDING = "pending"
    BOOTSTRAPPING = "bootstrapping"
    TAILSCALE_UP = "tailscale_up"
    INITIALIZING = "initializing"
    COMPLETE = "complete"
    FAILED = "failed"


class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    DEALLOCATED = "deallocated"
    UNKNOWN = "unknown"


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
    extra_packages: list[str]
    init_status: str
    ssh_public_key: str | None
    tailscale_host: str | None
    azure_resource_id: str | None
    wsl_distro_name: str | None
    cpus: int | None
    memory_gib: int | None
    disk_gib: int | None
    vm_user: str
    created_at: str
    last_seen_at: str | None


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
class VMGitHostKeyRow:
    id: int
    vm_name: str
    git_host_name: str
    remote_key_id: str
    created_at: str


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
            self._conn.executescript(MIGRATIONS[version])
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
        row = self._conn.execute(
            "SELECT COUNT(*) FROM vms WHERE vm_host_name = ?", (vm_host_name,)
        ).fetchone()
        return int(row[0])

    # -- VMs ---------------------------------------------------------------

    def insert_vm(
        self,
        name: str,
        platform: str,
        vm_host_name: str | None = None,
        extra_packages: list[str] | None = None,
        azure_resource_id: str | None = None,
        wsl_distro_name: str | None = None,
        cpus: int | None = None,
        memory_gib: int | None = None,
        disk_gib: int | None = None,
        vm_user: str = "agentworks",
    ) -> VMRow:
        self._conn.execute(
            "INSERT INTO vms "
            "(name, platform, vm_host_name, extra_packages, azure_resource_id, wsl_distro_name, "
            "cpus, memory_gib, disk_gib, vm_user) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                platform,
                vm_host_name,
                json.dumps(extra_packages) if extra_packages else None,
                azure_resource_id,
                wsl_distro_name,
                cpus,
                memory_gib,
                disk_gib,
                vm_user,
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

    def update_vm_init_status(self, name: str, status: InitStatus) -> None:
        self._conn.execute("UPDATE vms SET init_status = ? WHERE name = ?", (status.value, name))
        self._conn.commit()

    def update_vm_tailscale(self, name: str, tailscale_host: str) -> None:
        self._conn.execute("UPDATE vms SET tailscale_host = ? WHERE name = ?", (tailscale_host, name))
        self._conn.commit()

    def clear_vm_tailscale(self, name: str) -> None:
        self._conn.execute("UPDATE vms SET tailscale_host = NULL WHERE name = ?", (name,))
        self._conn.commit()

    def update_vm_ssh_public_key(self, name: str, ssh_public_key: str) -> None:
        self._conn.execute("UPDATE vms SET ssh_public_key = ? WHERE name = ?", (ssh_public_key, name))
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
        self._conn.execute("DELETE FROM vm_git_host_keys WHERE vm_name = ?", (name,))
        self._conn.execute(
            "DELETE FROM agents WHERE workspace_name IN (SELECT name FROM workspaces WHERE vm_name = ?)",
            (name,),
        )
        self._conn.execute("DELETE FROM workspaces WHERE vm_name = ?", (name,))
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
        self._conn.execute("DELETE FROM agents WHERE workspace_name = ?", (name,))
        self._conn.execute("DELETE FROM workspaces WHERE name = ?", (name,))
        self._conn.commit()

    def count_workspaces_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE vm_name = ?", (vm_name,)
        ).fetchone()
        return int(row[0])

    # -- Git Host Keys -----------------------------------------------------

    def insert_vm_git_host_key(self, vm_name: str, git_host_name: str, remote_key_id: str) -> VMGitHostKeyRow:
        self._conn.execute(
            "INSERT INTO vm_git_host_keys (vm_name, git_host_name, remote_key_id) VALUES (?, ?, ?)",
            (vm_name, git_host_name, remote_key_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM vm_git_host_keys WHERE vm_name = ? AND git_host_name = ?",
            (vm_name, git_host_name),
        ).fetchone()
        assert row is not None
        return _to_git_host_key(row)

    def list_vm_git_host_keys(self, vm_name: str) -> list[VMGitHostKeyRow]:
        rows = self._conn.execute(
            "SELECT * FROM vm_git_host_keys WHERE vm_name = ? ORDER BY git_host_name",
            (vm_name,),
        ).fetchall()
        return [_to_git_host_key(r) for r in rows]

    def delete_vm_git_host_key(self, key_id: int) -> None:
        self._conn.execute("DELETE FROM vm_git_host_keys WHERE id = ?", (key_id,))
        self._conn.commit()

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
            "DELETE FROM agents WHERE workspace_name = ?", (workspace_name,),
        )
        self._conn.commit()
        return agents


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
        extra_packages=json.loads(extra) if extra else [],
        init_status=row["init_status"],
        ssh_public_key=row["ssh_public_key"],
        tailscale_host=row["tailscale_host"],
        azure_resource_id=row["azure_resource_id"],
        wsl_distro_name=row["wsl_distro_name"],
        cpus=row["cpus"],
        memory_gib=row["memory_gib"],
        disk_gib=row["disk_gib"],
        vm_user=row["vm_user"],
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


def _to_git_host_key(row: sqlite3.Row) -> VMGitHostKeyRow:
    return VMGitHostKeyRow(
        id=row["id"],
        vm_name=row["vm_name"],
        git_host_name=row["git_host_name"],
        remote_key_id=row["remote_key_id"],
        created_at=row["created_at"],
    )
