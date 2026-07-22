"""The Database class: typed interface to the Agentworks state database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING

import agentworks.db as _db
from agentworks.db.converters import (
    _eq_or_in,
    _to_agent,
    _to_agent_grant,
    _to_console,
    _to_console_session,
    _to_session,
    _to_vm,
    _to_vm_event,
    _to_workspace,
)
from agentworks.db.migrations import LATEST_VERSION, MIGRATIONS, MigrationContext

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agentworks.db.models import (
        AgentGrantRow,
        AgentRow,
        ConsoleRow,
        ConsoleSessionRow,
        InitStatus,
        ProvisioningStatus,
        SessionMode,
        SessionRow,
        ShellEntry,
        VMEventRow,
        VMRow,
        WorkspaceRow,
    )


class Database:
    """Typed interface to the Agentworks state database."""

    def __init__(self, path: Path | None = None) -> None:
        db_path = path or _db.DB_PATH
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
        db_path = path or _db.DB_PATH
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
        #
        # Each version commits as a durable checkpoint (schema_version
        # row included): sqlite3 auto-commits DDL anyway, so a
        # transactional whole-run guard was never real, but WITHOUT
        # the per-version commit, a failure in version N+1 rolled back
        # version N's schema_version INSERT while N's DDL survived, and
        # the retry re-ran N's DDL into duplicate-column errors. With
        # the checkpoint, retry resumes at the failed version. A
        # failure INSIDE one version can still leave that version
        # partially applied (the documented residual; per-version
        # pre-DDL validation in Python steps minimizes it).
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            context: MigrationContext | None = None
            for version in range(current + 1, LATEST_VERSION + 1):
                step = MIGRATIONS[version]
                if callable(step):
                    # Python steps get the migration context (built once,
                    # lazily; string-only runs never read the config).
                    if context is None:
                        context = MigrationContext(legacy=_db._load_legacy_toml())
                    step(self._conn, context)
                else:
                    for stmt in step.split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            self._conn.execute(stmt)
                violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(f"foreign key violations after migration {version}: {violations}")
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
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
        admin_template: str | None = None,
        cpus: int | None = None,
        memory_gib: int | None = None,
        disk_gib: int | None = None,
        swap_gib: int | None = None,
        admin_username: str = "agentworks",
    ) -> VMRow:
        self._conn.execute(
            "INSERT INTO vms "
            "(name, site, hostname, template, admin_template, cpus, "
            "memory_gib, disk_gib, swap_gib, admin_username) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                site,
                hostname,
                template,
                admin_template,
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
        """Record operator stop/start intent (gates the activation gate's auto-start)."""
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

    # -- Settings ------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        """Install-level state (per ADR 0016 config-side state, not a
        resource). ``None`` means the key was never written; an empty
        string is a written-but-empty value (e.g. the declined system
        slug), so callers can distinguish never-asked from declined.
        """
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Write (or overwrite) one install-level settings row."""
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
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
            "INSERT INTO workspaces (name, vm_name, template, workspace_path, linux_group) VALUES (?, ?, ?, ?, ?)",
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
        harness_state: dict[str, object] | None = None,
    ) -> SessionRow:
        self._conn.execute(
            "INSERT INTO sessions "
            "(name, workspace_name, template, mode, agent_name, created_workspace, "
            "created_agent, socket_path, harness_state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                workspace_name,
                template,
                mode.value,
                agent_name,
                int(created_workspace),
                int(created_agent),
                socket_path,
                json.dumps(harness_state or {}),
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
            clauses.append(f"s.workspace_name IN (SELECT name FROM workspaces WHERE {vm_inner_clause})")
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
        if pid is not None and pid != _db.PID_STOPPED and pid <= 0:
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

    def update_session_harness_state(self, name: str, harness_state: dict[str, object]) -> None:
        """Persist the harness's per-session state blob (harness-owned,
        opaque to the core) after the harness op. Usually a no-op on
        restart (the value was minted and stored on create), but a
        session predating the ``harness_state`` column (backfilled to
        ``{}``) mints and stores here on its first restart."""
        self._conn.execute(
            "UPDATE sessions SET harness_state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
            (json.dumps(harness_state), name),
        )
        self._conn.commit()

    def update_session_socket_path(self, name: str, socket_path: str | None) -> None:
        self._conn.execute(
            "UPDATE sessions SET socket_path = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
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
                "WHERE cs2.console_name = c.name AND " + " AND ".join(session_predicates) + ")"
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

    def get_console_session(self, console_name: str, session_name: str) -> ConsoleSessionRow | None:
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

    def reorder_console_sessions(self, console_name: str, ordered_session_names: list[str]) -> None:
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
                "UPDATE console_sessions SET position = -(position + 1) WHERE console_name = ?",
                (console_name,),
            )
            # Phase 2: assign final positions in the desired order.
            for new_pos, name in enumerate(ordered_session_names):
                self._conn.execute(
                    "UPDATE console_sessions SET position = ? WHERE console_name = ? AND session_name = ?",
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
            "UPDATE console_sessions SET shells = ? WHERE console_name = ? AND session_name = ?",
            (json.dumps(shells), console_name, session_name),
        )
        self._touch_console(console_name)
        self._commit_unless_in_tx()

    def _touch_console(self, name: str) -> None:
        self._conn.execute(
            "UPDATE consoles SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name = ?",
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
