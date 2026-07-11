"""SQLite state database for Agentworks.

Database lives at ~/.config/agentworks/agentworks.db. Created automatically on
first use. Schema migrations are forward-only via a version table.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypedDict

from agentworks.config import CONFIG_DIR

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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
class VMRow:
    name: str
    # The vm-site the VM was created at (the resource name; resolved to
    # a bound platform via agentworks.vms.sites).
    site: str
    template: str | None
    extra_packages: list[str]
    provisioning_status: str
    init_status: str
    tailscale_host: str | None
    cpus: int | None
    memory_gib: int | None
    disk_gib: int | None
    swap_gib: int | None
    admin_username: str
    # The VM's OS-level hostname, recorded at create time so later
    # reads (SSH config, prompts) never re-derive it from live config.
    hostname: str
    created_at: str
    last_seen_at: str | None
    # Opaque per-platform identifiers (JSON in the column); the owning
    # platform is the only reader (azure resource_id, wsl2 distro_name,
    # proxmox vmid/node, lima instance_name).
    platform_metadata: dict[str, str] = field(default_factory=dict)
    # Operator intent flag: the operator explicitly stopped this VM, so
    # auto-start gates (ensure_active) must not restart it.
    operator_stopped: bool = False


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
    vm_name: str
    template: str | None
    workspace_path: str
    created_at: str
    last_seen_at: str | None
    # Linux group on the VM. Set at create time so legacy workspaces
    # (created when the prefix was "ws--") keep their existing group even
    # after the prefix changed to "ws-".
    linux_group: str


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


@dataclass
class MigrationContext:
    """Context handed to Python-step migrations.

    ``legacy`` is a best-effort, UNVALIDATED parse of the operator's
    config file (the whole TOML document, so hooks can reach legacy
    sections like ``[proxmox]``). A missing or unreadable config yields
    an empty mapping -- tolerant by construction, nothing in a
    migration may depend on it succeeding.
    """

    legacy: dict[str, Any] = field(default_factory=dict)


def _load_legacy_toml() -> dict[str, Any]:
    """Best-effort parse of the operator config for MigrationContext."""
    import tomllib

    from agentworks.config import CONFIG_PATH

    try:
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _migrate_vm_sites(conn: sqlite3.Connection, context: MigrationContext) -> None:
    """v27 (vm-sites SDD): ``vms`` grows ``platform_metadata`` /
    ``operator_stopped`` / ``hostname``; remote-Lima rows re-point at
    their host-named site (printing ready-to-paste site manifests); the
    legacy per-platform columns and ``vm_hosts`` drop; ``settings``
    lands.
    """
    from agentworks.vms.platforms import VM_PLATFORM_REGISTRY

    # Validate BEFORE the first DDL statement. Pre-SDD schemas only
    # ever stored the four legacy platform names, and the vm-sites
    # bridge refuses custom-site creates before this migration exists,
    # so anything else is genuine corruption: fail loudly, don't guess.
    # The scan must precede the ALTERs because sqlite3 auto-commits
    # DDL -- failing after them would leave a half-migrated v26 DB
    # that dies on duplicate-column at every retry, even once the
    # operator fixes the corrupt row.
    for row in conn.execute("SELECT name, platform FROM vms").fetchall():
        if row["platform"] not in VM_PLATFORM_REGISTRY:
            raise sqlite3.IntegrityError(
                f"vms row '{row['name']}' has unknown platform "
                f"'{row['platform']}'; cannot backfill platform metadata"
            )

    # Same pre-DDL stance for the remote-Lima site names: a host that
    # shadows a platform name gets a '-host' suffix (the new site-name
    # rules reserve platform names for their own platform), and a
    # suffixed name landing on another real host's site would silently
    # merge two distinct hosts -- fail loudly while the DB is pristine.
    host_sites: dict[str, str] = {}  # host -> site
    for row in conn.execute(
        "SELECT DISTINCT vm_host_name AS host FROM vms "
        "WHERE vm_host_name IS NOT NULL"
    ).fetchall():
        host = row["host"]
        site = f"{host}-host" if host in VM_PLATFORM_REGISTRY else host
        clash = next((h for h, s in host_sites.items() if s == site), None)
        if clash is not None:
            raise sqlite3.IntegrityError(
                f"remote-Lima site name collision: hosts '{clash}' and "
                f"'{host}' both map to site '{site}'; rename one "
                f"vm_hosts row and retry"
            )
        host_sites[host] = site

    conn.execute(
        "ALTER TABLE vms ADD COLUMN platform_metadata TEXT NOT NULL DEFAULT '{}'"
    )
    conn.execute(
        "ALTER TABLE vms ADD COLUMN operator_stopped INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute("ALTER TABLE vms ADD COLUMN hostname TEXT")

    # Backfill platform_metadata (the owning platform's hook) and
    # hostname (the value the create-time bootstrap actually set),
    # keyed by the pre-rename platform column.
    for row in conn.execute("SELECT * FROM vms").fetchall():
        platform = row["platform"]
        cls = VM_PLATFORM_REGISTRY[platform]  # validated above
        metadata = cls.legacy_platform_metadata(row, context.legacy)
        conn.execute(
            "UPDATE vms SET platform_metadata = ?, hostname = ? WHERE name = ?",
            (json.dumps(metadata), f"{platform}--{row['name']}", row["name"]),
        )

    # Remote-Lima rows: the site IS the host (R3). The operator must
    # declare the matching vm-site manifest; until then those VMs are
    # in the designed stranded state, so collect ready-to-paste
    # manifest documents and print them once at the end (the host ->
    # site map was validated pre-DDL above).
    site_hosts: dict[str, tuple[str | None, str]] = {}
    for host, site in host_sites.items():
        conn.execute(
            "UPDATE vms SET platform = ? WHERE vm_host_name = ?", (site, host)
        )
        host_row = conn.execute(
            "SELECT ssh_host FROM vm_hosts WHERE name = ?", (host,)
        ).fetchone()
        site_hosts[site] = (host_row["ssh_host"] if host_row else None, host)

    # Rebuild vms (the vm_host_name FK blocks DROP COLUMN): drop the
    # legacy per-platform columns and vm_host_name, rename platform to
    # site, declare hostname NOT NULL.
    conn.execute("""
        CREATE TABLE vms_new (
            name                TEXT PRIMARY KEY,
            site                TEXT NOT NULL,
            template            TEXT,
            extra_packages      TEXT,
            provisioning_status TEXT NOT NULL DEFAULT 'pending',
            init_status         TEXT NOT NULL DEFAULT 'pending',
            ssh_public_key      TEXT,
            tailscale_host      TEXT,
            cpus                INTEGER,
            memory_gib          INTEGER,
            disk_gib            INTEGER,
            swap_gib            INTEGER,
            admin_username      TEXT NOT NULL DEFAULT 'agentworks',
            hostname            TEXT NOT NULL,
            platform_metadata   TEXT NOT NULL DEFAULT '{}',
            operator_stopped    INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen_at        TEXT
        )
    """)
    conn.execute("""
        INSERT INTO vms_new
            (name, site, template, extra_packages, provisioning_status,
             init_status, ssh_public_key, tailscale_host, cpus, memory_gib,
             disk_gib, swap_gib, admin_username, hostname, platform_metadata,
             operator_stopped, created_at, last_seen_at)
            SELECT name, platform, template, extra_packages,
                   provisioning_status, init_status, ssh_public_key,
                   tailscale_host, cpus, memory_gib, disk_gib, swap_gib,
                   admin_username, hostname, platform_metadata,
                   operator_stopped, created_at, last_seen_at
            FROM vms
    """)
    conn.execute("DROP TABLE vms")
    conn.execute("ALTER TABLE vms_new RENAME TO vms")
    conn.execute("DROP TABLE vm_hosts")

    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")

    if site_hosts:
        from agentworks import output
        from agentworks.vms.sites import site_manifest_hint

        # Everything goes through output.warn: migrations run at every
        # Database() open, including from the shell-completion helpers
        # that capture stdout (`agw vm list --names-only`), so stdout
        # must stay clean for machine consumers.
        output.warn(
            "remote-Lima VMs now live at host-named sites; declare each "
            "site or those VMs stay unreachable:"
        )
        for site, (ssh_host, host) in sorted(site_hosts.items()):
            if site != host:
                output.warn(
                    f"(the host '{host}' shadows a platform name, so its "
                    f"site is named '{site}')"
                )
            output.warn(f"vm-site '{site}': " + site_manifest_hint(site, vm_host=ssh_host))


MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection, MigrationContext], None]] = {
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
    # -- Drop local workspaces. All workspaces are now VM-scoped. ---------
    # -- Removes the `type` column and tightens vm_name/linux_group to ---
    # -- NOT NULL. Any pre-existing local workspaces (and their sessions) -
    # -- are deleted. Defensively also drops any malformed rows missing ---
    # -- vm_name or linux_group so the rebuild's NOT NULL constraints can -
    # -- not fail mid-migration. ------------------------------------------
    26: """
        DELETE FROM sessions WHERE workspace_name IN (
            SELECT name FROM workspaces
            WHERE type != 'vm' OR vm_name IS NULL OR linux_group IS NULL
        );
        DELETE FROM agent_workspace_grants WHERE workspace_name IN (
            SELECT name FROM workspaces
            WHERE type != 'vm' OR vm_name IS NULL OR linux_group IS NULL
        );
        DELETE FROM workspaces
            WHERE type != 'vm' OR vm_name IS NULL OR linux_group IS NULL;
        CREATE TABLE workspaces_new (
            name           TEXT PRIMARY KEY,
            vm_name        TEXT NOT NULL,
            template       TEXT,
            workspace_path TEXT NOT NULL,
            linux_group    TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen_at   TEXT,
            FOREIGN KEY (vm_name) REFERENCES vms(name)
        );
        INSERT INTO workspaces_new
            (name, vm_name, template, workspace_path, linux_group, created_at, last_seen_at)
            SELECT name, vm_name, template, workspace_path, linux_group, created_at, last_seen_at
            FROM workspaces;
        DROP TABLE workspaces;
        ALTER TABLE workspaces_new RENAME TO workspaces;
    """,
    # -- vm-sites SDD: platform_metadata / operator_stopped / hostname, ----
    # -- the platform -> site rename, legacy column + vm_hosts drops, ------
    # -- and the settings table. Python step: the backfill dispatches ------
    # -- through the platform classes' legacy_platform_metadata hooks. -----
    27: _migrate_vm_sites,
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

        if current >= LATEST_VERSION:
            return

        # Disable FK enforcement for the duration of the migration run.
        # Table-rebuild migrations (CREATE _new, INSERT, DROP, RENAME) can
        # momentarily invalidate FK references that hold by name -- the
        # SQLite-recommended pattern is to disable FKs around the rebuild
        # and run a foreign_key_check at the end to confirm consistency.
        # Note: sqlite3 auto-commits DDL, so a mid-loop failure can leave
        # the DB partially migrated. The foreign_key_check is best-effort
        # consistency verification, not a full transactional guard.
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            context: MigrationContext | None = None
            for version in range(current + 1, LATEST_VERSION + 1):
                step = MIGRATIONS[version]
                if callable(step):
                    # Python steps get the migration context (built once,
                    # lazily -- string-only runs never read the config).
                    if context is None:
                        context = MigrationContext(legacy=_load_legacy_toml())
                    step(self._conn, context)
                else:
                    for stmt in step.split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            self._conn.execute(stmt)
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"foreign key violations after migration: {violations}"
                )
            self._conn.commit()
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    # -- VMs ---------------------------------------------------------------

    def insert_vm(
        self,
        name: str,
        site: str,
        hostname: str,
        template: str | None = None,
        cpus: int | None = None,
        memory_gib: int | None = None,
        disk_gib: int | None = None,
        swap_gib: int | None = None,
        admin_username: str = "agentworks",
    ) -> VMRow:
        self._conn.execute(
            "INSERT INTO vms "
            "(name, site, hostname, template, cpus, memory_gib, disk_gib, "
            "swap_gib, admin_username) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                site,
                hostname,
                template,
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

    def update_vm_platform_metadata(self, name: str, metadata: dict[str, str]) -> None:
        """Replace the VM's platform_metadata wholesale (the owning
        platform returns the complete dict from ``create()``)."""
        self._conn.execute(
            "UPDATE vms SET platform_metadata = ? WHERE name = ?",
            (json.dumps(metadata), name),
        )
        self._conn.commit()

    def set_operator_stopped(self, name: str, stopped: bool) -> None:
        """Record operator stop/start intent (gates ensure_active)."""
        self._conn.execute(
            "UPDATE vms SET operator_stopped = ? WHERE name = ?",
            (1 if stopped else 0, name),
        )
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
        workspace_path: str,
        vm_name: str,
        linux_group: str,
        template: str | None = None,
    ) -> WorkspaceRow:
        self._conn.execute(
            "INSERT INTO workspaces (name, vm_name, template, workspace_path, linux_group) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, vm_name, template, workspace_path, linux_group),
        )
        self._conn.commit()
        result = self.get_workspace(name)
        assert result is not None
        return result

    def get_workspace(self, name: str) -> WorkspaceRow | None:
        row = self._conn.execute("SELECT * FROM workspaces WHERE name = ?", (name,)).fetchone()
        return _to_workspace(row) if row else None

    def list_workspaces(self, *, vm_name: str | list[str] | None = None) -> list[WorkspaceRow]:
        """List workspaces, optionally filtered by VM. `vm_name` accepts a
        single string or a list of strings; list values are OR-ed together."""
        clause, params = _eq_or_in("vm_name", vm_name)
        sql = "SELECT * FROM workspaces"
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY name"
        rows = self._conn.execute(sql, params).fetchall()
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

    def list_agents(self, *, vm_name: str | list[str] | None = None) -> list[AgentRow]:
        """List agents, optionally filtered by VM. `vm_name` accepts a
        single string or a list of strings; list values are OR-ed together."""
        clause, params = _eq_or_in("vm_name", vm_name)
        sql = "SELECT * FROM agents"
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY vm_name, name"
        rows = self._conn.execute(sql, params).fetchall()
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

    def list_sessions(
        self,
        *,
        workspace_name: str | list[str] | None = None,
        vm_name: str | list[str] | None = None,
        agent_name: str | list[str] | None = None,
        admin_only: bool = False,
    ) -> list[SessionRow]:
        """List sessions, optionally filtered by workspace, VM, agent, or mode.

        Each name filter accepts a single string or a list of strings; list
        values are OR-ed together within a filter, and filters AND together
        across the call. `vm_name` filters via the session's workspace
        (sessions on workspaces that live on the given VM). `agent_name`
        matches the session's `agent_name` column directly; admin-mode
        sessions (NULL agent_name) are excluded when this filter is set.
        `admin_only` restricts to admin-mode sessions (agent_name IS NULL);
        it is the inverse of `agent_name` and the two should not be passed
        together (the CLI layer enforces the mutex; this layer accepts the
        combination but will simply return no rows since the predicates are
        contradictory).
        """
        clauses: list[str] = []
        params: list[object] = []
        ws_clause, ws_params = _eq_or_in("s.workspace_name", workspace_name)
        if ws_clause:
            clauses.append(ws_clause)
            params.extend(ws_params)
        vm_inner_clause, vm_params = _eq_or_in("vm_name", vm_name)
        if vm_inner_clause:
            clauses.append(
                f"s.workspace_name IN (SELECT name FROM workspaces WHERE {vm_inner_clause})"
            )
            params.extend(vm_params)
        ag_clause, ag_params = _eq_or_in("s.agent_name", agent_name)
        if ag_clause:
            clauses.append(ag_clause)
            params.extend(ag_params)
        if admin_only:
            clauses.append("s.agent_name IS NULL")

        sql = "SELECT s.* FROM sessions s"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY s.workspace_name, s.name"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
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

    def list_consoles(self, *, vm_name: str | list[str] | None = None) -> list[ConsoleRow]:
        """List consoles, optionally filtered by VM. `vm_name` accepts a
        single string or a list of strings; list values are OR-ed together."""
        clause, params = _eq_or_in("vm_name", vm_name)
        sql = "SELECT * FROM consoles"
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY name"
        rows = self._conn.execute(sql, params).fetchall()
        return [_to_console(r) for r in rows]

    def list_consoles_with_counts(
        self,
        *,
        vm_name: str | list[str] | None = None,
        workspace_name: str | list[str] | None = None,
        agent_name: str | list[str] | None = None,
    ) -> list[tuple[ConsoleRow, int]]:
        """Return consoles paired with session counts, one query, ORDER BY name.

        Each filter accepts a single string or a list; list values OR within
        a filter and filters AND across the call. `workspace_name` and
        `agent_name` filter on the console's session membership: a console
        is returned if it has at least one member session that matches every
        session-level filter that was supplied. When both filters are passed
        together, BOTH predicates must hold on the SAME session (not on
        different sessions in the same console) -- this matches how
        `list_sessions` composes filters and avoids surprising results where
        a console matches because one session is in workspace X and a
        completely unrelated session is run by agent Y.

        The session count returned is the console's TOTAL membership, not the
        count of matching sessions, so the displayed count always reflects the
        full console.
        """
        sql = (
            "SELECT c.*, COUNT(cs.session_name) AS session_count "
            "FROM consoles c "
            "LEFT JOIN console_sessions cs ON cs.console_name = c.name "
        )
        clauses: list[str] = []
        params: list[object] = []
        vm_clause, vm_params = _eq_or_in("c.vm_name", vm_name)
        if vm_clause:
            clauses.append(vm_clause)
            params.extend(vm_params)
        # Session-level filters share a single correlated EXISTS so a console
        # only matches when one of its sessions satisfies all of them at once.
        session_predicates: list[str] = []
        ws_clause, ws_params = _eq_or_in("s.workspace_name", workspace_name)
        if ws_clause:
            session_predicates.append(ws_clause)
            params.extend(ws_params)
        ag_clause, ag_params = _eq_or_in("s.agent_name", agent_name)
        if ag_clause:
            session_predicates.append(ag_clause)
            params.extend(ag_params)
        if session_predicates:
            clauses.append(
                "EXISTS (SELECT 1 FROM console_sessions cs2 "
                "JOIN sessions s ON s.name = cs2.session_name "
                "WHERE cs2.console_name = c.name AND "
                + " AND ".join(session_predicates)
                + ")"
            )
        if clauses:
            sql += "WHERE " + " AND ".join(clauses) + " "
        sql += "GROUP BY c.name ORDER BY c.name"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
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

    def reorder_console_sessions(
        self, console_name: str, ordered_session_names: list[str]
    ) -> None:
        """Rewrite ``console_sessions.position`` to match *ordered_session_names*.

        The list must contain every current member exactly once -- this is a
        full reordering primitive, not a partial-bump helper. The manager
        layer is responsible for computing the desired full ordering before
        calling this (so the same primitive can serve future operations like
        "move to back" or "swap two").

        Implemented in two passes inside a transaction: first all positions
        get bumped to negative offsets to escape the UNIQUE(console_name,
        position) constraint during the rewrite, then each member's final
        positive position is written. The transaction guarantees the table
        never settles in a state that violates the unique constraint visible
        outside the transaction.
        """
        with self.transaction():
            current = self.list_console_sessions(console_name)
            current_names = {m.session_name for m in current}
            desired_names = set(ordered_session_names)
            if current_names != desired_names or len(ordered_session_names) != len(current):
                # Caller bug: positions can't be assigned if the membership set
                # doesn't match. Manager layer must guarantee this contract.
                raise ValueError(
                    f"reorder_console_sessions for '{console_name}' requires the "
                    f"full member list: given {sorted(desired_names)} does not "
                    f"match current members {sorted(current_names)}"
                )
            # Phase 1: park every row at -(old_position + 1) so no UNIQUE
            # collision can fire when phase 2 writes the new positions.
            self._conn.execute(
                "UPDATE console_sessions SET position = -(position + 1) "
                "WHERE console_name = ?",
                (console_name,),
            )
            # Phase 2: assign final positions in the desired order.
            for new_pos, name in enumerate(ordered_session_names):
                self._conn.execute(
                    "UPDATE console_sessions SET position = ? "
                    "WHERE console_name = ? AND session_name = ?",
                    (new_pos, console_name, name),
                )
            self._touch_console(console_name)

    def list_consoles_for_session(self, session_name: str) -> list[ConsoleRow]:
        """Return consoles that currently list *session_name* as a member.

        Must be called before deleting the session row; the FK cascade on
        console_sessions makes this query return nothing after the fact.
        """
        rows = self._conn.execute(
            "SELECT c.* FROM consoles c "
            "JOIN console_sessions cs ON cs.console_name = c.name "
            "WHERE cs.session_name = ? "
            "ORDER BY c.name",
            (session_name,),
        ).fetchall()
        return [_to_console(r) for r in rows]

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


# -- Query helpers ---------------------------------------------------------


def _eq_or_in(column: str, value: str | list[str] | None) -> tuple[str, tuple[str, ...]]:
    """Build a SQL WHERE-clause fragment for a singular OR list filter.

    Returns ``("", ())`` when the filter is absent. Returns ``column = ?``
    for a single string or single-element list. Returns ``column IN (?, ?, ...)``
    for a multi-element list. Used by every ``Database.list_*`` method that
    accepts a multi-value filter so single-value callers stay readable and
    multi-value callers get OR-within-filter semantics from one place.
    """
    if value is None:
        return "", ()
    values = [value] if isinstance(value, str) else list(value)
    if not values:
        return "", ()
    if len(values) == 1:
        return f"{column} = ?", (values[0],)
    placeholders = ",".join("?" * len(values))
    return f"{column} IN ({placeholders})", tuple(values)


# -- Row converters --------------------------------------------------------


def _to_vm(row: sqlite3.Row) -> VMRow:
    extra = row["extra_packages"]
    metadata = row["platform_metadata"]
    return VMRow(
        name=row["name"],
        site=row["site"],
        template=row["template"],
        extra_packages=json.loads(extra) if extra else [],
        provisioning_status=row["provisioning_status"],
        init_status=row["init_status"],
        tailscale_host=row["tailscale_host"],
        cpus=row["cpus"],
        memory_gib=row["memory_gib"],
        disk_gib=row["disk_gib"],
        swap_gib=row["swap_gib"],
        admin_username=row["admin_username"],
        hostname=row["hostname"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        platform_metadata=json.loads(metadata) if metadata else {},
        operator_stopped=bool(row["operator_stopped"]),
    )


def _to_workspace(row: sqlite3.Row) -> WorkspaceRow:
    return WorkspaceRow(
        name=row["name"],
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
