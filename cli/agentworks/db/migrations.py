"""Schema migrations for the Agentworks state database.

Migrations are forward-only, keyed by integer version in ``MIGRATIONS``. Each
value is either a semicolon-separated block of DDL/DML statements or a Python
callable (``(sqlite3.Connection, MigrationContext) -> None``) for migrations
that need row-level logic beyond what SQL alone can express.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentworks.errors import MigrationBlockedError
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform.base import VMPlatform


@dataclass
class MigrationContext:
    """Context handed to Python-step migrations.

    ``legacy`` is a best-effort, UNVALIDATED parse of the operator's
    config file (the whole TOML document, so hooks can reach legacy
    sections like ``[proxmox]``). A missing or unreadable config yields
    an empty mapping: tolerant by construction, nothing in a
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


def _retire_vm_checkpoints(conn: sqlite3.Connection, _context: MigrationContext) -> None:
    """Drop the superseded checkpoint schema only after proving it empty."""

    # Own the write slot before observing emptiness. An already-running v34
    # process does not know about the sidecar migration lock and could otherwise
    # insert an ownership row between SELECT and DROP. The migration runner's
    # schema-version insert and commit close this same transaction.
    conn.execute("BEGIN IMMEDIATE")
    exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'vm_checkpoints'").fetchone()
    if exists is None:
        return
    names = [row[0] for row in conn.execute("SELECT vm_name FROM vm_checkpoints ORDER BY vm_name").fetchall()]
    if names:
        raise MigrationBlockedError(
            "state database migration is blocked by managed VM checkpoint records",
            entity_kind="database",
            hint=(
                "Reinstall the previous Agentworks build, delete each managed checkpoint for VMs: "
                + ", ".join(names)
                + ", then retry."
            ),
        )
    conn.execute("DROP TABLE vm_checkpoints")


def _migrate_vm_sites(conn: sqlite3.Connection, context: MigrationContext) -> None:
    """v27 (the vm-site refactor): ``vms`` grows ``platform_metadata`` /
    ``operator_stopped`` / ``hostname``; remote-Lima rows re-point at
    their host-named site (printing ready-to-paste site manifests); the
    legacy per-platform columns and ``vm_hosts`` drop; ``settings``
    lands.
    """
    # Name the DB file in validation errors so the operator knows which
    # file to inspect/fix (PRAGMA reports the actual attached file, so
    # this stays honest for non-default paths, e.g. in tests). Spelled
    # home-relative like every other host path an operator reads.
    db_file = format_host_path(Path(conn.execute("PRAGMA database_list").fetchone()[2]))

    # Validate BEFORE the first DDL statement. Pre-v27 schemas only
    # ever stored the four legacy platform names, and the vm-sites
    # bridge refuses custom-site creates before this migration exists,
    # so anything else is genuine corruption: fail loudly, don't guess.
    # The map is FROZEN as of v27 in the PRE-v27 vocabulary and pinned
    # to classes directly: a later build adding a platform must not
    # loosen this check, and a later platform RENAME (azure -> azure-vm
    # already happened) must not break the backfill lookup, so this
    # deliberately does not go through VM_PLATFORM_REGISTRY keys. The
    # scan must precede the ALTERs because sqlite3 auto-commits DDL:
    # failing after them would leave a half-migrated v26 DB that dies
    # on duplicate-column at every retry, even once the operator fixes
    # the corrupt row.
    # ``proxmox`` and ``azure`` now ship in opt-in system plugins; this frozen
    # backfill imports their classes DIRECTLY (from the plugin packages, not
    # through the live registry) to keep the registry-bypassing philosophy
    # above intact.
    from agentworks.capabilities.vm_platform import (
        LimaPlatform,
        WSL2Platform,
    )
    from agentworks.plugins.azure.platform import AzureVMPlatform
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    legacy_platform_classes: dict[str, type[VMPlatform]] = {
        "lima": LimaPlatform,
        "wsl2": WSL2Platform,
        "azure": AzureVMPlatform,
        "proxmox": ProxmoxPlatform,
    }
    for row in conn.execute("SELECT name, platform FROM vms").fetchall():
        if row["platform"] not in legacy_platform_classes:
            raise sqlite3.IntegrityError(
                f"vms row '{row['name']}' has unknown platform "
                f"'{row['platform']}'; cannot backfill platform metadata "
                f"(database: {db_file})"
            )

    # Same pre-DDL stance for the remote-Lima site names: a host that
    # shadows a platform name or a bundled-site name gets a '-host'
    # suffix (platform names are reserved for the shadow rule; bundled
    # names are reserved built-ins), and a suffixed name landing on
    # another real host's site would silently merge two distinct hosts;
    # fail loudly while the DB is pristine. The set is FROZEN as of
    # v27: a migration's output must not change when a later build adds
    # platforms or bundled sites, so this deliberately does not derive
    # from the live registry.
    reserved_site_names = {
        "lima",
        "wsl2",
        "azure",  # the legacy [azure] section's site keeps this name
        "azure-vm",
        "proxmox",
        "lima-local",
    }
    host_sites: dict[str, str] = {}  # host -> site
    for row in conn.execute("SELECT DISTINCT vm_host_name AS host FROM vms WHERE vm_host_name IS NOT NULL").fetchall():
        host = row["host"]
        site = f"{host}-host" if host in reserved_site_names else host
        clash = next((h for h, s in host_sites.items() if s == site), None)
        if clash is not None:
            raise sqlite3.IntegrityError(
                f"remote-Lima site name collision: hosts '{clash}' and "
                f"'{host}' both map to site '{site}'; rename one host in "
                f"BOTH vm_hosts.name and the referencing vms.vm_host_name "
                f"rows, then retry (database: {db_file})"
            )
        host_sites[host] = site

    conn.execute("ALTER TABLE vms ADD COLUMN platform_metadata TEXT NOT NULL DEFAULT '{}'")
    conn.execute("ALTER TABLE vms ADD COLUMN operator_stopped INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE vms ADD COLUMN hostname TEXT")

    # Backfill platform_metadata (the owning platform's hook) and
    # hostname (the value the create-time bootstrap actually set),
    # keyed by the pre-rename platform column.
    for row in conn.execute("SELECT * FROM vms").fetchall():
        platform = row["platform"]
        cls = legacy_platform_classes[platform]  # validated above
        metadata = cls.legacy_platform_metadata(row, context.legacy)
        conn.execute(
            "UPDATE vms SET platform_metadata = ?, hostname = ? WHERE name = ?",
            (json.dumps(metadata), f"{platform}--{row['name']}", row["name"]),
        )

    # Local-Lima rows: the bundled site is named lima-local, not lima
    # (the platform keeps the bare name; the site is one CONFIGURATION
    # of it). Remote rows are excluded here: they re-point at their
    # host-named sites below.
    conn.execute("UPDATE vms SET platform = 'lima-local' WHERE platform = 'lima' AND vm_host_name IS NULL")

    # Remote-Lima rows: the site IS the host. The operator must
    # declare the matching vm-site manifest; until then those VMs are
    # in the designed stranded state, so collect ready-to-paste
    # manifest documents and print them once at the end (the host ->
    # site map was validated pre-DDL above).
    site_hosts: dict[str, tuple[str | None, str]] = {}
    for host, site in host_sites.items():
        conn.execute("UPDATE vms SET platform = ? WHERE vm_host_name = ?", (site, host))
        host_row = conn.execute("SELECT ssh_host FROM vm_hosts WHERE name = ?", (host,)).fetchone()
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

        # Everything goes through output.warn: migrations can run under
        # ordinary JSON or names-only commands, so stdout must stay clean
        # for machine consumers. Completion probes never migrate.
        output.warn("remote-Lima VMs now live at host-named sites; declare each site or those VMs stay unreachable:")
        for site, (ssh_host, host) in sorted(site_hosts.items()):
            if site != host:
                output.warn(f"(the host '{host}' shadows a platform name, so its site is named '{site}')")
            output.warn(f"vm-site '{site}': " + site_manifest_hint(site, vm_host=ssh_host))


def _rename_harness_integration_state(conn: sqlite3.Connection, _context: MigrationContext) -> None:
    """v31: rename the session harness-integration-state column, resumably."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    old_column = "harness_state"
    new_column = "harness_integration_state"
    old_exists = old_column in columns
    new_exists = new_column in columns

    if old_exists and not new_exists:
        conn.execute("ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state")
        return
    if new_exists and not old_exists:
        return

    found = sorted(columns & {old_column, new_column})
    raise sqlite3.IntegrityError(
        f"sessions state-column migration expected exactly one of {old_column!r} or {new_column!r}; found {found!r}"
    )


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
    # session before upgrading. This recovery targets the PREVIOUS release's
    # CLI, where the command is 'session restart' (renamed to 'session resume'
    # in 0.13.0), so it deliberately keeps the old spelling.
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
    # -- Optional admin-shell window ---------------------------------------
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
    # -- vm-sites: platform_metadata / operator_stopped / hostname, --------
    # -- the platform -> site rename, legacy column + vm_hosts drops, ------
    # -- and the settings table. Python step: the backfill dispatches ------
    # -- through the platform classes' legacy_platform_metadata hooks. -----
    27: _migrate_vm_sites,
    # -- Drop the write-dead workspaces.last_seen_at column: it has had ----
    # -- no writer since update_workspace_last_seen was removed. Rebuild ---
    # -- the table without it (the project's table-rebuild discipline). ----
    # -- Every row is preserved and no column any child table references ---
    # -- (they reference workspaces.name) is touched, so no ----------------
    # -- delete-from-referencing-tables step is needed; the run's ----------
    # -- foreign_key_check confirms the name-based FKs still hold.
    28: """
        CREATE TABLE workspaces_new (
            name           TEXT PRIMARY KEY,
            vm_name        TEXT NOT NULL,
            template       TEXT,
            workspace_path TEXT NOT NULL,
            linux_group    TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (vm_name) REFERENCES vms(name)
        );
        INSERT INTO workspaces_new
            (name, vm_name, template, workspace_path, linux_group, created_at)
            SELECT name, vm_name, template, workspace_path, linux_group, created_at
            FROM workspaces;
        DROP TABLE workspaces;
        ALTER TABLE workspaces_new RENAME TO workspaces;
    """,
    # -- Harness-state blob: a per-session, harness-owned JSON object the --
    # -- harness reads and mutates during its start/restart op (the ------
    # -- manager persists it after). A pure additive column with a -------
    # -- default backfills existing rows to '{}' in place, so no table ---
    # -- rebuild is needed (the additive-column pattern, like v3/v11). ---
    29: """
        ALTER TABLE sessions ADD COLUMN harness_state TEXT NOT NULL DEFAULT '{}';
    """,
    # -- Per-VM admin-template selector: which admin-template the VM's -----
    # -- admin user was provisioned from. Nullable; NULL means the --------
    # -- reserved ``default`` admin-template (mirrors the vms.template -----
    # -- column added in migration 11). Renumbered to 30 on merge with -----
    # -- main's harness_state migration, which took 29. -------------------
    30: """
        ALTER TABLE vms ADD COLUMN admin_template TEXT;
    """,
    # -- Rename the persisted capability state for harness-integration. ---
    # -- The Python step is resumable across the DDL-to-version-record -----
    # -- crash window: seeing only the new column means the rename already -
    # -- committed and v31 only needs its schema-version record. -----------
    31: _rename_harness_integration_state,
    # -- Cross-kind desired and applied instance state. The envelope stays -
    # -- stable as domain codecs add record payload versions. Existing VM --
    # -- hardware values lack the required operation provenance, so this ---
    # -- migration creates structure only and performs no backfill. --------
    32: """
        CREATE TABLE instance_records (
            instance_kind  TEXT NOT NULL
                CHECK (instance_kind IN ('vm', 'workspace', 'agent', 'session')),
            instance_name  TEXT NOT NULL CHECK (length(instance_name) > 0),
            record_type    TEXT NOT NULL CHECK (length(record_type) > 0),
            record_key     TEXT NOT NULL CHECK (length(record_key) > 0),
            payload_version INTEGER NOT NULL
                CHECK (typeof(payload_version) = 'integer' AND payload_version > 0),
            value_json     TEXT NOT NULL CHECK (typeof(value_json) = 'text'),
            recorded_at    TEXT NOT NULL,
            operation      TEXT,
            PRIMARY KEY (instance_kind, instance_name, record_type, record_key),
            CHECK (
                record_type != 'desired-overlay'
                OR (record_key = 'spec' AND operation IS NULL)
            ),
            CHECK (
                record_type != 'applied-state'
                OR (typeof(operation) = 'text' AND length(operation) > 0)
            )
        );
        CREATE INDEX idx_instance_records_kind_type
            ON instance_records (instance_kind, record_type, instance_name, record_key);
    """,
    # -- Last live Debian release observation. Both columns are nullable --
    # -- for pre-observation VMs, but they must change as one fact. The ---
    # -- release itself is intentionally not enumerated in SQL so future -
    # -- registry profiles do not require another schema-shape migration. -
    33: """
        ALTER TABLE vms ADD COLUMN debian_release TEXT;
        ALTER TABLE vms ADD COLUMN debian_release_observed_at TEXT
            CHECK ((debian_release IS NULL) = (debian_release_observed_at IS NULL));
    """,
    # -- One Agentworks-managed recovery checkpoint per VM. Operation -----
    # -- identity fences interrupted lifecycle retries; the desired-state -
    # -- fingerprint lets core refuse an unsafe whole-VM restore after ----
    # -- the VM-owned desired database subtree has changed. ---------------
    34: """
        CREATE TABLE vm_checkpoints (
            vm_name                   TEXT PRIMARY KEY
                                      REFERENCES vms(name) ON DELETE RESTRICT,
            name                      TEXT NOT NULL UNIQUE,
            provider_identifier       TEXT,
            operation_id              TEXT UNIQUE,
            desired_state_fingerprint TEXT NOT NULL
                                      CHECK (length(desired_state_fingerprint) = 64),
            state                     TEXT NOT NULL
                                      CHECK (state IN ('creating', 'ready', 'restoring', 'deleting')),
            capture_release           TEXT NOT NULL,
            source_release            TEXT,
            target_release            TEXT,
            created_at                TEXT NOT NULL
                                      DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            CHECK (provider_identifier IS NOT NULL OR state = 'creating'),
            CHECK ((state = 'ready') = (operation_id IS NULL)),
            CHECK ((source_release IS NULL) = (target_release IS NULL)),
            CHECK (source_release IS NULL OR capture_release = source_release)
        );
    """,
    # -- Assisted upgrades and their managed checkpoint product were -----
    # -- removed before release. Keep the already-applied v34 history for -
    # -- development installations, but never discard a provider-artifact -
    # -- ownership record implicitly. ------------------------------------
    35: _retire_vm_checkpoints,
}

LATEST_VERSION = max(MIGRATIONS)

# Restore validation depends on this map matching every completed migration.
# Future migrations that add, rebuild, rename, or remove a table or column must
# update the sentinel changes below in the same change as the migration.
_SCHEMA_SENTINEL_ADDITIONS: dict[int, dict[str, tuple[str, ...]]] = {
    1: {
        "schema_version": ("version", "applied_at"),
        "vm_hosts": ("name", "ssh_host", "platform", "os", "created_at", "last_seen_at"),
        "vms": (
            "name",
            "platform",
            "vm_host_name",
            "extra_packages",
            "init_status",
            "ssh_public_key",
            "tailscale_host",
            "azure_resource_id",
            "wsl_distro_name",
            "created_at",
            "last_seen_at",
        ),
        "workspaces": (
            "name",
            "type",
            "vm_name",
            "template",
            "workspace_path",
            "created_at",
            "last_seen_at",
        ),
        "vm_git_host_keys": ("id", "vm_name", "git_host_name", "remote_key_id", "created_at"),
    },
    2: {"vms": ("cpus", "memory_gib", "disk_gib")},
    3: {"vms": ("vm_user",)},
    4: {"agents": ("name", "workspace_name", "linux_user", "created_at")},
    6: {
        "vms": ("provisioning_status",),
        "vm_events": ("id", "vm_name", "event", "detail", "created_at"),
    },
    7: {"vms": ("admin_username",)},
    8: {
        "tasks": (
            "name",
            "workspace_name",
            "template",
            "mode",
            "linux_user",
            "status",
            "created_at",
            "updated_at",
        )
    },
    10: {"vms": ("swap_gib",)},
    11: {"vms": ("template",)},
    12: {"agents": ("template",)},
    13: {
        "agents": ("vm_name", "grant_all"),
        "agent_workspace_grants": ("agent_name", "workspace_name", "grant_type", "task_name", "created_at"),
    },
    14: {"tasks": ("agent_name",)},
    15: {"tasks": ("created_workspace",)},
    16: {"vms": ("proxmox_vmid",)},
    17: {
        "sessions": (
            "name",
            "workspace_name",
            "template",
            "mode",
            "status",
            "created_at",
            "updated_at",
            "agent_name",
            "created_workspace",
            "socket_path",
        )
    },
    18: {"agent_workspace_grants": ("session_name",)},
    20: {"sessions": ("pid",)},
    21: {"sessions": ("boot_id",)},
    22: {"workspaces": ("linux_group",)},
    23: {
        "consoles": ("name", "vm_name", "created_at", "updated_at"),
        "console_sessions": ("console_name", "session_name", "position", "shells"),
    },
    24: {"consoles": ("admin_shell",)},
    25: {"sessions": ("created_agent",)},
    27: {
        "vms": ("site", "hostname", "platform_metadata", "operator_stopped"),
        "settings": ("key", "value"),
    },
    29: {"sessions": ("harness_state",)},
    30: {"vms": ("admin_template",)},
    31: {"sessions": ("harness_integration_state",)},
    32: {
        "instance_records": (
            "instance_kind",
            "instance_name",
            "record_type",
            "record_key",
            "payload_version",
            "value_json",
            "recorded_at",
            "operation",
        )
    },
    33: {"vms": ("debian_release", "debian_release_observed_at")},
    34: {
        "vm_checkpoints": (
            "vm_name",
            "name",
            "provider_identifier",
            "operation_id",
            "desired_state_fingerprint",
            "state",
            "capture_release",
            "source_release",
            "target_release",
            "created_at",
        )
    },
}

_SCHEMA_SENTINEL_REMOVED_TABLES: dict[int, tuple[str, ...]] = {
    5: ("vm_git_host_keys",),
    17: ("tasks",),
    27: ("vm_hosts",),
    35: ("vm_checkpoints",),
}

_SCHEMA_SENTINEL_REMOVED_COLUMNS: dict[int, dict[str, tuple[str, ...]]] = {
    7: {"vms": ("vm_user",)},
    13: {"agents": ("workspace_name",)},
    14: {"tasks": ("linux_user",)},
    18: {"agent_workspace_grants": ("task_name",)},
    20: {"sessions": ("status",)},
    26: {"workspaces": ("type",)},
    27: {
        "vms": ("platform", "vm_host_name", "azure_resource_id", "wsl_distro_name", "proxmox_vmid"),
    },
    28: {"workspaces": ("last_seen_at",)},
    31: {"sessions": ("harness_state",)},
}


def _build_schema_sentinels() -> dict[int, dict[str, frozenset[str]]]:
    """Expand milestone changes into the completed shape for each version."""
    tables: dict[str, set[str]] = {}
    versions: dict[int, dict[str, frozenset[str]]] = {}
    for version in range(1, LATEST_VERSION + 1):
        for table in _SCHEMA_SENTINEL_REMOVED_TABLES.get(version, ()):
            tables.pop(table)
        for table, columns in _SCHEMA_SENTINEL_REMOVED_COLUMNS.get(version, {}).items():
            tables[table].difference_update(columns)
        for table, columns in _SCHEMA_SENTINEL_ADDITIONS.get(version, {}).items():
            tables.setdefault(table, set()).update(columns)
        versions[version] = {table: frozenset(columns) for table, columns in tables.items()}
    return versions


SCHEMA_SENTINELS = _build_schema_sentinels()
"""Complete Agentworks table-and-column shape for each completed schema."""
