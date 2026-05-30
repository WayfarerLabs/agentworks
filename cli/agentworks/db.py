"""SQLite state database for Agentworks.

Database lives at ~/.config/agentworks/agentworks.db. Created automatically on
first use. Schema migrations are forward-only via a version table.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

from agentworks.config import CONFIG_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator
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


class SessionMode(Enum):
    ADMIN = "admin"
    AGENT = "agent"


class SessionStatus(Enum):
    """Session liveness state, computed live from has-session + PID/boot_id checks."""

    OK = "ok"
    STOPPED = "stopped"
    BROKEN = "broken"
    UNKNOWN = "unknown"


# Sentinel PID value: session is known to be stopped (no process to check).
# Distinct from NULL (never checked / pre-enhancement).
PID_STOPPED = -1


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
    proxmox_vmid: str | None
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
    # Linux group on the VM. Null for local workspaces. Set at create time
    # so legacy VM workspaces (created when the prefix was "ws--") keep
    # their existing group even after the prefix changed to "ws-".
    linux_group: str | None


@dataclass
class AgentRow:
    name: str
    vm_name: str
    linux_user: str
    template: str | None
    grant_all: bool
    created_at: str


@dataclass
class AgentGrantRow:
    agent_name: str
    workspace_name: str
    grant_type: str  # 'explicit' or 'implicit'
    session_name: str | None  # NULL for explicit, session name for implicit
    created_at: str


@dataclass
class SessionRow:
    name: str
    workspace_name: str
    template: str
    mode: str
    created_at: str
    updated_at: str
    agent_name: str | None = None
    created_workspace: bool = False
    created_agent: bool = False
    socket_path: str | None = None
    pid: int | None = None
    boot_id: str | None = None


class ShellEntry(TypedDict):
    """One shell pane in a console window. cwd None = workspace root."""

    cwd: str | None
    admin: bool


@dataclass
class ConsoleRow:
    name: str
    vm_name: str
    admin_shell: bool
    created_at: str
    updated_at: str


@dataclass
class ConsoleSessionRow:
    console_name: str
    session_name: str
    position: int
    shells: list[ShellEntry]


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
    12: """
        ALTER TABLE agents ADD COLUMN template TEXT;
        UPDATE agents SET template = 'default';
    """,
    13: """
        -- Restructure agents: workspace-scoped -> VM-scoped
        CREATE TABLE agents_new (
            name           TEXT PRIMARY KEY,
            vm_name        TEXT NOT NULL,
            linux_user     TEXT NOT NULL UNIQUE,
            template       TEXT,
            grant_all      INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (vm_name) REFERENCES vms(name) ON DELETE CASCADE
        );
        INSERT INTO agents_new (name, vm_name, linux_user, template, grant_all, created_at)
            SELECT a.name, w.vm_name, 'agt--' || a.name, a.template, 0, a.created_at
            FROM agents a JOIN workspaces w ON a.workspace_name = w.name
            WHERE w.vm_name IS NOT NULL;
        DROP TABLE agents;
        ALTER TABLE agents_new RENAME TO agents;

        -- Workspace grants table
        CREATE TABLE IF NOT EXISTS agent_workspace_grants (
            agent_name     TEXT NOT NULL,
            workspace_name TEXT NOT NULL,
            grant_type     TEXT NOT NULL,
            task_name      TEXT,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name) ON DELETE CASCADE
        );

        -- Rename workspace groups: ws-<name> -> ws--<name>
        -- (actual Linux group rename must be done manually on VMs)
    """,
    14: """
        ALTER TABLE tasks ADD COLUMN agent_name TEXT REFERENCES agents(name);
        -- Backfill agent_name from linux_user for existing agent-mode tasks
        UPDATE tasks SET agent_name = (
            SELECT a.name FROM agents a WHERE a.linux_user = tasks.linux_user
        ) WHERE mode = 'agent';
        ALTER TABLE tasks DROP COLUMN linux_user;
    """,
    15: """
        ALTER TABLE tasks ADD COLUMN created_workspace INTEGER NOT NULL DEFAULT 0;
    """,
    16: """
        ALTER TABLE vms ADD COLUMN proxmox_vmid TEXT;
    """,
    17: """
        -- Rename tasks -> sessions with globally unique names
        CREATE TABLE sessions (
            name              TEXT PRIMARY KEY,
            workspace_name    TEXT NOT NULL,
            template          TEXT NOT NULL,
            mode              TEXT NOT NULL DEFAULT 'admin',
            status            TEXT NOT NULL DEFAULT 'running',
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            agent_name        TEXT REFERENCES agents(name),
            created_workspace INTEGER NOT NULL DEFAULT 0,
            socket_path       TEXT,
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name)
        );
        INSERT INTO sessions
            (name, workspace_name, template, mode, status,
             created_at, updated_at, agent_name, created_workspace)
            SELECT workspace_name || '--' || name, workspace_name,
                   template, mode, status, created_at, updated_at,
                   agent_name, created_workspace
            FROM tasks;
        DROP TABLE tasks;
    """,
    18: """
        -- Rename task_name -> session_name in agent_workspace_grants
        CREATE TABLE agent_workspace_grants_new (
            agent_name     TEXT NOT NULL,
            workspace_name TEXT NOT NULL,
            grant_type     TEXT NOT NULL,
            session_name   TEXT,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name) ON DELETE CASCADE
        );
        INSERT INTO agent_workspace_grants_new (agent_name, workspace_name, grant_type, session_name, created_at)
            SELECT agent_name, workspace_name, grant_type,
                   CASE WHEN task_name IS NOT NULL THEN workspace_name || '--' || task_name ELSE NULL END,
                   created_at
            FROM agent_workspace_grants;
        DROP TABLE agent_workspace_grants;
        ALTER TABLE agent_workspace_grants_new RENAME TO agent_workspace_grants;
    """,
    # -- Enforce: agent sessions must have a socket_path ---------------------
    # Recreate sessions table with a CHECK constraint. The INSERT will fail
    # if any agent sessions have NULL socket_path (legacy default-server
    # mode). If this happens, revert to the previous version and run
    # 'session restart --force' or 'session delete' for each legacy agent
    # session before upgrading.
    19: """
        CREATE TABLE sessions_new (
            name              TEXT PRIMARY KEY,
            workspace_name    TEXT NOT NULL,
            template          TEXT NOT NULL,
            mode              TEXT NOT NULL DEFAULT 'admin',
            status            TEXT NOT NULL DEFAULT 'running',
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            agent_name        TEXT REFERENCES agents(name),
            created_workspace INTEGER NOT NULL DEFAULT 0,
            socket_path       TEXT,
            FOREIGN KEY (workspace_name) REFERENCES workspaces(name),
            CHECK (mode != 'agent' OR socket_path IS NOT NULL)
        );
        INSERT INTO sessions_new SELECT * FROM sessions;
        DROP TABLE sessions;
        ALTER TABLE sessions_new RENAME TO sessions;
    """,
    # -- Drop cached status, add PID for live liveness checks -----------------
    20: """
        ALTER TABLE sessions DROP COLUMN status;
        ALTER TABLE sessions ADD COLUMN pid INTEGER;
    """,
    # -- Add boot ID for PID staleness detection across VM reboots ----------
    21: """
        ALTER TABLE sessions ADD COLUMN boot_id TEXT;
    """,
    # -- Store workspace Linux group on the row so the prefix can change ----
    # -- without renaming existing groups on VMs. ---------------------------
    22: """
        ALTER TABLE workspaces ADD COLUMN linux_group TEXT;
        UPDATE workspaces SET linux_group = 'ws--' || name WHERE type = 'vm';
    """,
    # -- Multi-console support: named consoles with explicit session lists --
    23: """
        CREATE TABLE consoles (
            name       TEXT PRIMARY KEY,
            vm_name    TEXT NOT NULL REFERENCES vms(name) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE console_sessions (
            console_name TEXT NOT NULL REFERENCES consoles(name) ON DELETE CASCADE,
            session_name TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE,
            position     INTEGER NOT NULL,
            shells       TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (console_name, session_name),
            UNIQUE (console_name, position)
        );
        CREATE INDEX idx_console_sessions_order ON console_sessions(console_name, position);
    """,
    # -- Optional admin-shell window (legacy vm-console behavior) ----------
    24: """
        ALTER TABLE consoles ADD COLUMN admin_shell INTEGER NOT NULL DEFAULT 0;
    """,
    # -- Track sessions that created their own agent (parallel to ----------
    # -- created_workspace) so session delete can offer cleanup. -----------
    25: """
        ALTER TABLE sessions ADD COLUMN created_agent INTEGER NOT NULL DEFAULT 0;
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
        self._tx_depth = 0
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run multiple writes as one transaction, committing on success or
        rolling back on exception. Nested with-blocks defer to the outermost.

        Inside this context, CRUD methods skip their per-call commits and
        defer to the enclosing transaction.
        """
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self._tx_depth = 1
        try:
            with self._conn:
                yield
        finally:
            self._tx_depth = 0

    def _commit_unless_in_tx(self) -> None:
        """Commit pending changes unless inside an explicit transaction()."""
        if self._tx_depth == 0:
            self._conn.commit()

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
        proxmox_vmid: str | None = None,
        cpus: int | None = None,
        memory_gib: int | None = None,
        disk_gib: int | None = None,
        swap_gib: int | None = None,
        admin_username: str = "agentworks",
    ) -> VMRow:
        self._conn.execute(
            "INSERT INTO vms "
            "(name, platform, vm_host_name, template, azure_resource_id, wsl_distro_name, "
            "proxmox_vmid, cpus, memory_gib, disk_gib, swap_gib, admin_username) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                platform,
                vm_host_name,
                template,
                azure_resource_id,
                wsl_distro_name,
                proxmox_vmid,
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

    def update_vm_proxmox_vmid(self, name: str, proxmox_vmid: str) -> None:
        self._conn.execute("UPDATE vms SET proxmox_vmid = ? WHERE name = ?", (proxmox_vmid, name))
        self._conn.commit()

    def update_vm_last_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE vms SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_vm(self, name: str) -> None:
        # console_sessions cascade via FK when consoles are deleted
        self._conn.execute("DELETE FROM consoles WHERE vm_name = ?", (name,))
        self._conn.execute(
            "DELETE FROM sessions WHERE workspace_name IN (SELECT name FROM workspaces WHERE vm_name = ?)",
            (name,),
        )
        # Agents are VM-scoped; grants cascade via FK
        self._conn.execute("DELETE FROM agents WHERE vm_name = ?", (name,))
        self._conn.execute(
            "DELETE FROM agent_workspace_grants WHERE workspace_name IN "
            "(SELECT name FROM workspaces WHERE vm_name = ?)",
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
        linux_group: str | None = None,
    ) -> WorkspaceRow:
        self._conn.execute(
            "INSERT INTO workspaces (name, type, vm_name, template, workspace_path, linux_group) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, ws_type, vm_name, template, workspace_path, linux_group),
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

    def update_workspace_path(self, name: str, workspace_path: str) -> None:
        self._conn.execute(
            "UPDATE workspaces SET workspace_path = ? WHERE name = ?",
            (workspace_path, name),
        )
        self._conn.commit()

    def update_workspace_last_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE workspaces SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_workspace(self, name: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE workspace_name = ?", (name,))
        # Grants cascade via FK; agents are VM-scoped so not deleted with workspaces
        self._conn.execute("DELETE FROM agent_workspace_grants WHERE workspace_name = ?", (name,))
        self._conn.execute("DELETE FROM workspaces WHERE name = ?", (name,))
        self._conn.commit()

    def count_workspaces_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM workspaces WHERE vm_name = ?", (vm_name,)).fetchone()
        return int(row[0])

    def count_agents_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM agents WHERE vm_name = ?",
            (vm_name,),
        ).fetchone()
        return int(row[0])

    def count_sessions_on_vm(self, vm_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE workspace_name IN (SELECT name FROM workspaces WHERE vm_name = ?)",
            (vm_name,),
        ).fetchone()
        return int(row[0])

    # -- Agents ------------------------------------------------------------

    def insert_agent(
        self,
        name: str,
        vm_name: str,
        linux_user: str,
        template: str | None = None,
        grant_all: bool = False,
    ) -> AgentRow:
        self._conn.execute(
            "INSERT INTO agents (name, vm_name, linux_user, template, grant_all) VALUES (?, ?, ?, ?, ?)",
            (name, vm_name, linux_user, template, int(grant_all)),
        )
        self._conn.commit()
        result = self.get_agent(name)
        assert result is not None
        return result

    def get_agent(self, name: str) -> AgentRow | None:
        row = self._conn.execute(
            "SELECT * FROM agents WHERE name = ?",
            (name,),
        ).fetchone()
        return _to_agent(row) if row else None

    def list_agents(self, vm_name: str | None = None) -> list[AgentRow]:
        if vm_name is not None:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE vm_name = ? ORDER BY name",
                (vm_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY vm_name, name",
            ).fetchall()
        return [_to_agent(r) for r in rows]

    def delete_agent(self, name: str) -> None:
        # Grants cascade via FK
        self._conn.execute("DELETE FROM agents WHERE name = ?", (name,))
        self._conn.commit()

    def list_agents_on_vm_with_grant_all(self, vm_name: str) -> list[AgentRow]:
        """List agents on a VM that have grant_all enabled."""
        rows = self._conn.execute(
            "SELECT * FROM agents WHERE vm_name = ? AND grant_all = 1 ORDER BY name",
            (vm_name,),
        ).fetchall()
        return [_to_agent(r) for r in rows]

    # -- Agent workspace grants ------------------------------------------------

    def insert_agent_grant(
        self,
        agent_name: str,
        workspace_name: str,
        grant_type: str,
        session_name: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO agent_workspace_grants "
            "(agent_name, workspace_name, grant_type, session_name) "
            "VALUES (?, ?, ?, ?)",
            (agent_name, workspace_name, grant_type, session_name),
        )
        self._conn.commit()

    def delete_agent_grant(
        self,
        agent_name: str,
        workspace_name: str,
        grant_type: str,
        session_name: str | None = None,
    ) -> None:
        if session_name is not None:
            self._conn.execute(
                "DELETE FROM agent_workspace_grants "
                "WHERE agent_name = ? AND workspace_name = ? AND grant_type = ? AND session_name = ?",
                (agent_name, workspace_name, grant_type, session_name),
            )
        else:
            self._conn.execute(
                "DELETE FROM agent_workspace_grants "
                "WHERE agent_name = ? AND workspace_name = ? AND grant_type = ? AND session_name IS NULL",
                (agent_name, workspace_name, grant_type),
            )
        self._conn.commit()

    def delete_explicit_grants(self, agent_name: str) -> None:
        """Remove all explicit grants for an agent."""
        self._conn.execute(
            "DELETE FROM agent_workspace_grants WHERE agent_name = ? AND grant_type = 'explicit'",
            (agent_name,),
        )
        self._conn.commit()

    def has_any_grant(self, agent_name: str, workspace_name: str) -> bool:
        """Check if an agent has any grant (explicit or implicit) for a workspace."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM agent_workspace_grants WHERE agent_name = ? AND workspace_name = ?",
            (agent_name, workspace_name),
        ).fetchone()
        return int(row[0]) > 0

    def list_agent_grants(self, agent_name: str) -> list[AgentGrantRow]:
        rows = self._conn.execute(
            "SELECT * FROM agent_workspace_grants WHERE agent_name = ? ORDER BY workspace_name",
            (agent_name,),
        ).fetchall()
        return [_to_agent_grant(r) for r in rows]

    def list_granted_workspaces(self, agent_name: str) -> list[str]:
        """List distinct workspace names the agent has access to."""
        rows = self._conn.execute(
            "SELECT DISTINCT workspace_name FROM agent_workspace_grants WHERE agent_name = ? ORDER BY workspace_name",
            (agent_name,),
        ).fetchall()
        return [row[0] for row in rows]

    def list_granted_workspaces_with_types(self, agent_name: str) -> list[tuple[str, bool, bool]]:
        """List workspaces with grant type info: (name, has_explicit, has_implicit)."""
        rows = self._conn.execute(
            "SELECT workspace_name, grant_type FROM agent_workspace_grants "
            "WHERE agent_name = ? ORDER BY workspace_name",
            (agent_name,),
        ).fetchall()
        # Aggregate by workspace
        ws_map: dict[str, tuple[bool, bool]] = {}
        for row in rows:
            ws = row[0]
            gt = row[1]
            has_explicit, has_implicit = ws_map.get(ws, (False, False))
            if gt == "explicit":
                has_explicit = True
            else:
                has_implicit = True
            ws_map[ws] = (has_explicit, has_implicit)
        return [(ws, e, i) for ws, (e, i) in sorted(ws_map.items())]

    def count_agent_grants(self, agent_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT workspace_name) FROM agent_workspace_grants WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        return int(row[0])

    def update_agent_grant_all(self, name: str, grant_all: bool) -> None:
        self._conn.execute(
            "UPDATE agents SET grant_all = ? WHERE name = ?",
            (int(grant_all), name),
        )
        self._conn.commit()

    # -- Sessions ----------------------------------------------------------

    def insert_session(
        self,
        name: str,
        workspace_name: str,
        template: str,
        mode: SessionMode,
        agent_name: str | None = None,
        created_workspace: bool = False,
        created_agent: bool = False,
        socket_path: str | None = None,
    ) -> SessionRow:
        self._conn.execute(
            "INSERT INTO sessions "
            "(name, workspace_name, template, mode, agent_name, created_workspace, created_agent, socket_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                workspace_name,
                template,
                mode.value,
                agent_name,
                int(created_workspace),
                int(created_agent),
                socket_path,
            ),
        )
        self._conn.commit()
        result = self.get_session(name)
        assert result is not None
        return result

    def get_session(self, name: str) -> SessionRow | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE name = ?",
            (name,),
        ).fetchone()
        return _to_session(row) if row else None

    def list_sessions(self, workspace_name: str | None = None) -> list[SessionRow]:
        if workspace_name is not None:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE workspace_name = ? ORDER BY name",
                (workspace_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY workspace_name, name",
            ).fetchall()
        return [_to_session(r) for r in rows]

    def update_session_pid(self, name: str, pid: int | None, boot_id: str | None = None) -> None:
        """Store or clear the PID and boot ID for a session.

        Valid pid values: None, PID_STOPPED (-1), or a positive integer.
        When setting a positive PID, boot_id is required. When clearing
        (PID_STOPPED or None), COALESCE preserves the existing boot_id.
        """
        if pid is not None and pid != PID_STOPPED and pid <= 0:
            raise ValueError(f"invalid PID: {pid} (must be None, PID_STOPPED, or > 0)")
        if pid is not None and pid > 0 and boot_id is None:
            raise ValueError(f"boot_id is required when setting a positive PID ({pid})")
        self._conn.execute(
            "UPDATE sessions SET pid = ?, boot_id = COALESCE(?, boot_id), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE name = ?",
            (pid, boot_id, name),
        )
        self._conn.commit()

    def update_session_socket_path(self, name: str, socket_path: str | None) -> None:
        self._conn.execute(
            "UPDATE sessions SET socket_path = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE name = ?",
            (socket_path, name),
        )
        self._conn.commit()

    def delete_session(self, name: str) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE name = ?",
            (name,),
        )
        self._conn.commit()

    def delete_sessions_for_workspace(self, workspace_name: str) -> list[SessionRow]:
        """Delete all sessions for a workspace, returning the deleted sessions."""
        sessions = self.list_sessions(workspace_name=workspace_name)
        self._conn.execute(
            "DELETE FROM sessions WHERE workspace_name = ?",
            (workspace_name,),
        )
        self._conn.commit()
        return sessions

    # -- Consoles ----------------------------------------------------------

    def insert_console(
        self,
        name: str,
        vm_name: str,
        admin_shell: bool = False,
    ) -> ConsoleRow:
        self._conn.execute(
            "INSERT INTO consoles (name, vm_name, admin_shell) VALUES (?, ?, ?)",
            (name, vm_name, int(admin_shell)),
        )
        self._commit_unless_in_tx()
        result = self.get_console(name)
        assert result is not None
        return result

    def get_console(self, name: str) -> ConsoleRow | None:
        row = self._conn.execute("SELECT * FROM consoles WHERE name = ?", (name,)).fetchone()
        return _to_console(row) if row else None

    def list_consoles(self, vm_name: str | None = None) -> list[ConsoleRow]:
        if vm_name is not None:
            rows = self._conn.execute(
                "SELECT * FROM consoles WHERE vm_name = ? ORDER BY name",
                (vm_name,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM consoles ORDER BY name").fetchall()
        return [_to_console(r) for r in rows]

    def list_consoles_with_counts(
        self, vm_name: str | None = None
    ) -> list[tuple[ConsoleRow, int]]:
        """Return consoles paired with session counts, one query, ORDER BY name."""
        sql = (
            "SELECT c.*, COUNT(cs.session_name) AS session_count "
            "FROM consoles c "
            "LEFT JOIN console_sessions cs ON cs.console_name = c.name "
        )
        params: tuple[str, ...] = ()
        if vm_name is not None:
            sql += "WHERE c.vm_name = ? "
            params = (vm_name,)
        sql += "GROUP BY c.name ORDER BY c.name"
        rows = self._conn.execute(sql, params).fetchall()
        return [(_to_console(r), int(r["session_count"])) for r in rows]

    def delete_console(self, name: str) -> None:
        # console_sessions cascade via FK
        self._conn.execute("DELETE FROM consoles WHERE name = ?", (name,))
        self._commit_unless_in_tx()

    def add_console_session(
        self,
        console_name: str,
        session_name: str,
        shells: list[ShellEntry],
    ) -> ConsoleSessionRow:
        """Add a session to a console at position max(existing) + 1.

        Position assignment is atomic (single statement). Raises
        sqlite3.IntegrityError if (console_name, session_name) already exists
        or two concurrent inserts collide on position.
        """
        self._conn.execute(
            "INSERT INTO console_sessions (console_name, session_name, position, shells) "
            "VALUES (?, ?, "
            "COALESCE((SELECT MAX(position) FROM console_sessions WHERE console_name = ?), -1) + 1, "
            "?)",
            (console_name, session_name, console_name, json.dumps(shells)),
        )
        self._touch_console(console_name)
        self._commit_unless_in_tx()
        result = self.get_console_session(console_name, session_name)
        assert result is not None
        return result

    def remove_console_session(self, console_name: str, session_name: str) -> None:
        self._conn.execute(
            "DELETE FROM console_sessions WHERE console_name = ? AND session_name = ?",
            (console_name, session_name),
        )
        self._touch_console(console_name)
        self._commit_unless_in_tx()

    def get_console_session(
        self, console_name: str, session_name: str
    ) -> ConsoleSessionRow | None:
        row = self._conn.execute(
            "SELECT * FROM console_sessions WHERE console_name = ? AND session_name = ?",
            (console_name, session_name),
        ).fetchone()
        return _to_console_session(row) if row else None

    def list_console_sessions(self, console_name: str) -> list[ConsoleSessionRow]:
        """Return console members ordered by position ascending."""
        rows = self._conn.execute(
            "SELECT * FROM console_sessions WHERE console_name = ? ORDER BY position",
            (console_name,),
        ).fetchall()
        return [_to_console_session(r) for r in rows]

    def update_console_shells(
        self,
        console_name: str,
        session_name: str,
        shells: list[ShellEntry],
    ) -> None:
        self._conn.execute(
            "UPDATE console_sessions SET shells = ? "
            "WHERE console_name = ? AND session_name = ?",
            (json.dumps(shells), console_name, session_name),
        )
        self._touch_console(console_name)
        self._commit_unless_in_tx()

    def _touch_console(self, name: str) -> None:
        self._conn.execute(
            "UPDATE consoles SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE name = ?",
            (name,),
        )

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

    def snapshot_vm_backup_data(
        self,
        vm_name: str,
    ) -> tuple[
        VMRow | None,
        list[AgentRow],
        list[WorkspaceRow],
        list[SessionRow],
        list[VMEventRow],
        dict[str, list[AgentGrantRow]],
    ]:
        """Read all VM-related data in a single transaction for backup consistency.

        Returns (vm, agents, workspaces, sessions, events, grants_by_agent).
        """
        self._conn.execute("BEGIN")
        try:
            vm = self.get_vm(vm_name)
            agents = self.list_agents(vm_name=vm_name)
            workspaces = self.list_workspaces(vm_name=vm_name)
            ws_names = {ws.name for ws in workspaces}
            all_sessions = self.list_sessions()
            sessions = [s for s in all_sessions if s.workspace_name in ws_names]
            events = self.list_vm_events(vm_name)
            grants_by_agent: dict[str, list[AgentGrantRow]] = {}
            for agent in agents:
                grants_by_agent[agent.name] = self.list_agent_grants(agent.name)
        finally:
            self._conn.execute("COMMIT")
        return vm, agents, workspaces, sessions, events, grants_by_agent


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
        proxmox_vmid=row["proxmox_vmid"],
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
        linux_group=row["linux_group"],
    )


def _to_agent(row: sqlite3.Row) -> AgentRow:
    return AgentRow(
        name=row["name"],
        vm_name=row["vm_name"],
        linux_user=row["linux_user"],
        template=row["template"],
        grant_all=bool(row["grant_all"]),
        created_at=row["created_at"],
    )


def _to_agent_grant(row: sqlite3.Row) -> AgentGrantRow:
    return AgentGrantRow(
        agent_name=row["agent_name"],
        workspace_name=row["workspace_name"],
        grant_type=row["grant_type"],
        session_name=row["session_name"],
        created_at=row["created_at"],
    )


def _to_session(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        name=row["name"],
        workspace_name=row["workspace_name"],
        template=row["template"],
        mode=row["mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        agent_name=row["agent_name"],
        created_workspace=bool(row["created_workspace"]),
        created_agent=bool(row["created_agent"]),
        socket_path=row["socket_path"],
        pid=row["pid"],
        boot_id=row["boot_id"],
    )


def _to_vm_event(row: sqlite3.Row) -> VMEventRow:
    return VMEventRow(
        id=row["id"],
        vm_name=row["vm_name"],
        event=row["event"],
        detail=row["detail"],
        created_at=row["created_at"],
    )


def _to_console(row: sqlite3.Row) -> ConsoleRow:
    return ConsoleRow(
        name=row["name"],
        vm_name=row["vm_name"],
        admin_shell=bool(row["admin_shell"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_console_session(row: sqlite3.Row) -> ConsoleSessionRow:
    return ConsoleSessionRow(
        console_name=row["console_name"],
        session_name=row["session_name"],
        position=row["position"],
        shells=_parse_shells(row["shells"], row["console_name"], row["session_name"]),
    )


def _parse_shells(raw: str, console_name: str, session_name: str) -> list[ShellEntry]:
    """Decode the shells JSON column and verify it matches ShellEntry shape."""
    where = f"console_sessions[{console_name!r}, {session_name!r}].shells"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: invalid JSON ({exc})") from None
    if not isinstance(decoded, list):
        raise ValueError(f"{where}: expected list, got {type(decoded).__name__}")
    for i, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}[{i}]: expected dict, got {type(entry).__name__}")
        extra = set(entry.keys()) - {"cwd", "admin"}
        missing = {"cwd", "admin"} - set(entry.keys())
        if extra or missing:
            raise ValueError(
                f"{where}[{i}]: keys must be exactly cwd, admin "
                f"(extra={sorted(extra)}, missing={sorted(missing)})"
            )
        if entry["cwd"] is not None and not isinstance(entry["cwd"], str):
            raise ValueError(f"{where}[{i}].cwd: expected str or null")
        if not isinstance(entry["admin"], bool):
            raise ValueError(f"{where}[{i}].admin: expected bool")
    return decoded
