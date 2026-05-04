"""Session lifecycle orchestration."""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from functools import partial
from typing import TYPE_CHECKING

import typer

from agentworks import output
from agentworks.db import PID_STOPPED, SessionHealth, SessionMode
from agentworks.ssh import admin_exec_target

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Template variable substitution -- uses {{var}} syntax consistent with nerftools.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import ExecTarget, SSHLogger


# -- Helpers ---------------------------------------------------------------

# Grace period (seconds) to wait after sending C-c before killing a session
_STOP_GRACE_SECONDS = 5


def _resolve_session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user for a session.

    Agent-mode sessions look up the agent by name. Admin-mode sessions use the VM admin.
    """
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise output.AgentError(
                f"agent '{session.agent_name}' not found "
                f"(referenced by session '{session.name}')"
            )
        return agent.linux_user
    return vm.admin_username


def _effective_socket_path(db: Database, session: SessionRow) -> str | None:
    """Return the socket path for a session, deriving it for migrated agent sessions.

    Migrated sessions have socket_path=NULL in the DB. For agent-mode sessions,
    we can derive the path from the agent's linux_user and the session name.
    """
    if session.socket_path:
        return session.socket_path
    if not session.agent_name:
        return None
    from agentworks.sessions.tmux import agent_socket_path

    agent = db.get_agent(session.agent_name)
    if agent is None:
        return None
    return agent_socket_path(agent.linux_user, session.name)


def _session_alive(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> bool:
    """Check if a session's tmux session is alive on its expected server."""
    from agentworks.sessions.tmux import session_exists

    return session_exists(session_name, run_command=run_command, socket_path=socket_path)


def _kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> None:
    """Kill a session on its expected tmux server."""
    from agentworks.sessions.tmux import kill_session

    kill_session(session_name, run_command=run_command, socket_path=socket_path)


def _require_workspace(db: Database, name: str) -> WorkspaceRow:
    ws = db.get_workspace(name)
    if ws is None:
        raise output.WorkspaceError(f"workspace '{name}' not found")
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    if ws.type != "vm":
        raise output.SessionError("sessions are only supported on VM workspaces")
    vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
    if vm is None:
        raise output.VMError(f"VM '{ws.vm_name}' not found")
    return vm


def _prepare_vm(
    db: Database, config: Config, workspace_name: str, *, operation: str | None = None
) -> tuple[WorkspaceRow, VMRow, RunCommand, RunCommand, ExecTarget]:
    """Validate workspace/VM, ensure running, and return (ws, vm, run_command, run_as_root, target).

    If operation is set, creates an SSHLogger and binds it into both callables.
    The ExecTarget is also returned directly for callers that need it (e.g.
    ensure_agent_socket_*, batch_check_sessions).
    """
    from agentworks.ssh import SSHLogger, run
    from agentworks.ssh import run_as_root as ssh_run_as_root

    ws = _require_workspace(db, workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        raise output.VMError(f"VM '{vm.name}' has no Tailscale address")

    logger = SSHLogger(vm.name, operation) if operation else None
    target = admin_exec_target(vm, config, logger=logger)
    run_command = partial(run, target, logger=logger)
    run_as_root = partial(ssh_run_as_root, target, logger=logger)
    return ws, vm, run_command, run_as_root, target


def _require_session(db: Database, name: str) -> SessionRow:
    session = db.get_session(name)
    if session is None:
        raise output.SessionError(f"session '{name}' not found")
    return session


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Regenerate the workspace tmuxinator config from current session state."""
    from agentworks.ssh import write_file
    from agentworks.workspaces.tmuxinator import generate_config

    sessions = db.list_sessions(workspace_name=ws.name)
    # Build effective socket paths for tmuxinator (migrated sessions have NULL socket_path)
    socket_paths = {s.name: _effective_socket_path(db, s) for s in sessions}
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions, socket_paths=socket_paths)
    target = admin_exec_target(vm, config)
    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", config_text, logger=logger)


def _filter_sessions(
    db: Database,
    *,
    workspace_name: str | None = None,
    vm_name: str | None = None,
) -> list[SessionRow]:
    """Load sessions with optional workspace/VM filters."""
    sessions = db.list_sessions(workspace_name=workspace_name)
    if vm_name is not None:
        vm_workspaces = {ws.name for ws in db.list_workspaces(vm_name=vm_name)}
        sessions = [s for s in sessions if s.workspace_name in vm_workspaces]
    return sessions


def _resolve_template(config: Config, template_name: str | None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.sessions.templates import resolve_template

    try:
        return resolve_template(config, template_name)
    except ValueError as e:
        raise output.SessionError(str(e)) from None


def _substitute_template_vars(text: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders in a string with their values."""

    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in _KNOWN_TEMPLATE_VARS:
            raise output.SessionError(f"unknown template variable '{{{{{name}}}}}'")
        return variables[name]

    return _TEMPLATE_VAR_RE.sub(replace, text)


def _build_session_command(
    template: ResolvedSessionTemplate,
    *,
    session_name: str,
    workspace_name: str,
    restart: bool = False,
) -> str:
    """Build the shell command string for a session from its template.

    Returns an empty string if the template has no command (login shell only).
    Uses restart_command (if defined) when restart=True.
    """
    variables = {
        "session_name": session_name,
        "workspace_name": workspace_name,
    }

    raw_command = template.restart_command if restart and template.restart_command else template.command
    command = _substitute_template_vars(raw_command, variables)

    parts = []
    for key, val in template.env.items():
        if not _ENV_KEY_RE.match(key):
            raise output.SessionError(f"invalid env var name {key!r} in template '{template.name}'")
        val = _substitute_template_vars(val, variables)
        parts.append(f"export {key}={shlex.quote(val)}")

    if command:
        parts.append(f"exec {command}")

    return " && ".join(parts)


# -- Liveness checks -------------------------------------------------------


def check_session_status(pid: int, *, target: ExecTarget) -> bool:
    """Check if a PID is alive via /proc. No sudo or signal permissions needed."""
    result = target.run(f"test -d /proc/{pid}", check=False)
    return result.ok


def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: ExecTarget,
) -> dict[str, bool]:
    """Check PIDs for multiple sessions in one SSH call.

    Returns {session_name: alive}. Sessions with pid=None are excluded.
    """
    pid_sessions = [(s.name, s.pid) for s in sessions if s.pid is not None and s.pid > 0]
    if not pid_sessions:
        return {}

    parts = []
    for name, pid in pid_sessions:
        q_name = shlex.quote(name)
        parts.append(f"test -d /proc/{pid}; echo \"STATUS:{q_name}:$?\"")
    cmd = "; ".join(parts)

    result = target.run(cmd, check=False)
    stdout = getattr(result, "stdout", "") or ""

    status_map: dict[str, bool] = {}
    for line in stdout.strip().splitlines():
        if line.startswith("STATUS:"):
            # STATUS:<name>:<exit_code>
            _, rest = line.split(":", 1)
            colon = rest.rfind(":")
            if colon > 0:
                name = rest[:colon]
                exit_code = rest[colon + 1 :]
                status_map[name] = exit_code == "0"

    return status_map


def check_session_health(
    session: SessionRow,
    *,
    target: ExecTarget,
) -> SessionHealth:
    """Full health check: PID liveness + tmux connectivity.

    Pure function -- no DB side effects.
    """
    from agentworks.sessions.tmux import tmux_cmd

    if session.pid is None:
        return SessionHealth.UNKNOWN
    if session.pid == PID_STOPPED:
        return SessionHealth.STOPPED

    alive = check_session_status(session.pid, target=target)
    if not alive:
        return SessionHealth.STOPPED

    # Transport-specific connectivity test
    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}", session.socket_path) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if result.ok:
        return SessionHealth.OK

    return SessionHealth.BROKEN


# -- Public API ------------------------------------------------------------


def create_session(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    template_name: str | None = None,
    agent_name: str | None = None,
    created_workspace: bool = False,
) -> None:
    """Create and start a session."""
    from agentworks.config import validate_name
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    validate_name(name)
    ws, vm, run_command, run_as_root, target = _prepare_vm(db, config, workspace_name, operation="session-create")

    if db.get_session(name) is not None:
        raise output.SessionError(f"session '{name}' already exists")

    # Resolve mode and linux user
    resolved_agent_name: str | None = None
    if agent_name is not None:
        mode = SessionMode.AGENT
        agent = db.get_agent(agent_name)
        if agent is None:
            raise output.AgentError(f"agent '{agent_name}' not found")
        if agent.vm_name != vm.name:
            raise output.SessionError(
                f"agent '{agent_name}' is on VM '{agent.vm_name}', "
                f"but workspace '{workspace_name}' is on VM '{vm.name}'"
            )
        linux_user = agent.linux_user
        resolved_agent_name = agent_name

        # Auto-grant implicit workspace access if needed
        if not db.has_any_grant(agent_name, workspace_name):
            from agentworks.agents.manager import _add_to_workspace_group

            _add_to_workspace_group(vm, config, linux_user, workspace_name)
        db.insert_agent_grant(agent_name, workspace_name, "implicit", session_name=name)
    else:
        mode = SessionMode.ADMIN
        linux_user = vm.admin_username

    template = _resolve_template(config, template_name)

    # Compute socket path up front (deterministic from linux_user + session name).
    # Needed for the DB insert since the CHECK constraint requires agent sessions
    # to have a socket_path.
    expected_socket: str | None = None
    if mode == SessionMode.AGENT:
        from agentworks.sessions.tmux import agent_socket_path

        expected_socket = agent_socket_path(linux_user, name)

    # Insert DB record first to avoid orphaned tmux sessions on crash
    db.insert_session(
        name,
        workspace_name,
        template.name,
        mode,
        agent_name=resolved_agent_name,
        created_workspace=created_workspace,
        socket_path=expected_socket,
    )

    deploy_restricted_config(run_command, history_limit=config.session.history_limit)
    command = _build_session_command(template, session_name=name, workspace_name=workspace_name)

    try:
        sock, pid = create_tmux_session(
            name,
            ws.workspace_path,
            command,
            linux_user,
            run_command=run_command,
            target=target,
            run_as_root=run_as_root,
            admin_username=vm.admin_username,
            is_admin=(mode == SessionMode.ADMIN),
        )
    except Exception:
        db.delete_session(name)
        if resolved_agent_name:
            db.delete_agent_grant(resolved_agent_name, workspace_name, "implicit", session_name=name)
        raise

    # Persist socket path and PID
    if sock:
        db.update_session_socket_path(name, sock)
    db.update_session_pid(name, pid)

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    output.info(f"Session '{name}' started ({mode_label}, template: {template.name})")

    # Update tmuxinator config and add to console if it exists
    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=sock)


def _execute_stop(
    targets: list[tuple[SessionRow, ExecTarget]],
    *,
    db: Database,
    force: bool = False,
) -> list[tuple[str, str]]:
    """Core stop logic: C-c all, single grace period, kill survivors.

    Handles both single and batch stops. Returns list of (name, error) failures.
    """
    import time

    from agentworks.sessions.tmux import force_kill_tmux_server, send_keys
    from agentworks.ssh import run

    if not targets:
        return []

    # Phase 1: send C-c to all sessions (best effort)
    names = ", ".join(s.name for s, _ in targets)
    output.detail(f"Sending C-c to {len(targets)} session(s): {names}")
    for session, target in targets:
        sock = _effective_socket_path(db, session)
        run_cmd = partial(run, target)
        with contextlib.suppress(Exception):
            send_keys(session.name, "C-c", run_command=run_cmd, socket_path=sock)

    # Phase 2: single grace period
    output.detail(f"Waiting {_STOP_GRACE_SECONDS}s for graceful exit...")
    time.sleep(_STOP_GRACE_SECONDS)

    # Phase 3: check survivors per VM (reuse existing targets)
    by_target: dict[int, tuple[ExecTarget, list[SessionRow]]] = {}
    for session, target in targets:
        tid = id(target)
        if tid not in by_target:
            by_target[tid] = (target, [])
        by_target[tid][1].append(session)

    survivor_map: dict[str, bool] = {}
    for target, group in by_target.values():
        survivor_map.update(batch_check_status(group, target=target))

    failed: list[tuple[str, str]] = []

    for session, target in targets:
        alive = survivor_map.get(session.name, False)
        if alive:
            output.detail(f"Session '{session.name}' survived C-c, killing")
            sock = _effective_socket_path(db, session)
            run_cmd = partial(run, target)
            try:
                _kill_session(session.name, run_command=run_cmd, socket_path=sock)
            except Exception as exc:
                if force and session.pid and session.pid > 0:
                    output.detail(f"tmux kill failed for '{session.name}', force-killing PID {session.pid}")
                    force_kill_tmux_server(
                        session.pid, target=target, socket_path=session.socket_path, log=output.detail,
                    )
                else:
                    failed.append((session.name, str(exc)))
                    output.warn(f"Failed to stop '{session.name}': {exc}")
                    continue

        db.update_session_pid(session.name, PID_STOPPED)
        output.info(f"Session '{session.name}' stopped")

    return failed


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
) -> None:
    """Stop a running session. Sends C-c first, then kills after a grace period."""
    from agentworks.sessions.tmux import force_kill_tmux_server

    session = _require_session(db, name)
    _ws, _vm, _run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-stop")
    health = check_session_health(session, target=target)

    if health == SessionHealth.STOPPED:
        output.info(f"Session '{name}' is already stopped")
        return
    if health == SessionHealth.UNKNOWN:
        raise output.SessionError(f"session '{name}' has no PID recorded. Run 'session repair {name}' first.")
    if health == SessionHealth.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to kill the process."
            )
        assert session.pid is not None
        force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail)
        db.update_session_pid(name, PID_STOPPED)
        output.info(f"Session '{name}' force-stopped")
        return

    # OK health -- delegate to shared stop logic
    failed = _execute_stop([(session, target)], db=db, force=force)
    if failed:
        raise output.SessionError(f"failed to stop session '{name}': {failed[0][1]}")


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Restart a session. Prompts if running (--yes to skip). --force for BROKEN."""
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    session = _require_session(db, name)
    ws, vm, run_command, run_as_root, target = _prepare_vm(
        db, config, session.workspace_name, operation="session-restart",
    )
    health = check_session_health(session, target=target)

    if health == SessionHealth.UNKNOWN:
        raise output.SessionError(f"session '{name}' has no PID recorded. Run 'session repair {name}' first.")
    if health == SessionHealth.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to restart."
            )
        from agentworks.sessions.tmux import force_kill_tmux_server

        assert session.pid is not None
        force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail)
    elif health == SessionHealth.OK:
        if not yes and not output.confirm(f"Session '{name}' is running. Restart?"):
            raise output.UserAbort("restart cancelled")
        sock = _effective_socket_path(db, session)
        _kill_session(name, run_command=run_command, socket_path=sock)

    template = _resolve_template(config, session.template)
    deploy_restricted_config(run_command, history_limit=config.session.history_limit)

    # Use restart_command if available, otherwise fall back to command
    command = _build_session_command(
        template,
        session_name=name,
        workspace_name=session.workspace_name,
        restart=True,
    )
    is_admin = session.mode == SessionMode.ADMIN.value
    linux_user = _resolve_session_linux_user(db, session, vm)

    try:
        new_sock, pid = create_tmux_session(
            name,
            ws.workspace_path,
            command,
            linux_user,
            run_command=run_command,
            target=target,
            run_as_root=run_as_root,
            admin_username=vm.admin_username,
            is_admin=is_admin,
        )
    except RuntimeError as exc:
        if "already has an active tmux server" in str(exc):
            raise output.SessionError(
                f"session '{name}' has an active tmux server that was not detected by the health check. "
                "Use 'session stop --force' to kill it, then retry."
            ) from exc
        raise

    # Persist socket path if it differs from what's stored. Compare against
    # session.socket_path (not the derived effective path) so migrated sessions
    # with NULL socket_path get backfilled on restart.
    if new_sock != session.socket_path:
        db.update_session_socket_path(name, new_sock)
    db.update_session_pid(name, pid)

    output.info(f"Session '{name}' restarted")

    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=new_sock)


def stop_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | None = None,
    workspace_name: str | None = None,
    force: bool = False,
) -> None:
    """Stop all running sessions, optionally filtered by VM or workspace."""
    sessions = _filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)

    # Batch PID check to find running sessions
    alive_map = _batch_check_all_sessions(sessions, db=db, config=config)
    alive_sessions = [
        s
        for s in sessions
        if s.pid is not None and s.pid != PID_STOPPED and alive_map.get(s.name, False)
    ]

    if not alive_sessions:
        output.info("No running sessions to stop.")
        return

    output.info(f"Stopping {len(alive_sessions)} session(s)...")

    # Resolve VM targets (reuse across sessions on the same VM)
    vm_targets: dict[str, ExecTarget] = {}
    for s in alive_sessions:
        ws = db.get_workspace(s.workspace_name)
        if ws and ws.vm_name and ws.vm_name not in vm_targets:
            vm = db.get_vm(ws.vm_name)
            if vm and vm.tailscale_host:
                vm_targets[ws.vm_name] = admin_exec_target(vm, config)

    # Build (session, target) pairs for _execute_stop
    stop_targets: list[tuple[SessionRow, ExecTarget]] = []
    for s in alive_sessions:
        ws = db.get_workspace(s.workspace_name)
        if ws and ws.vm_name and ws.vm_name in vm_targets:
            stop_targets.append((s, vm_targets[ws.vm_name]))

    failed = _execute_stop(stop_targets, db=db, force=force)
    if failed:
        raise output.SessionError(f"{len(failed)} session(s) failed to stop.")


def restart_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | None = None,
    workspace_name: str | None = None,
    include_running: bool = False,
    force: bool = False,
) -> None:
    """Restart sessions, optionally filtered by VM or workspace.

    With include_running=False (--all-stopped), only stopped sessions are
    restarted. With include_running=True (--all), all sessions are targeted;
    if any are running, the caller should have prompted or passed yes=True.
    """
    sessions = _filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)
    alive_map = _batch_check_all_sessions(sessions, db=db, config=config)

    if not include_running:
        # Only stopped sessions
        sessions = [
            s
            for s in sessions
            if s.pid == PID_STOPPED
            or (s.pid is not None and s.pid > 0 and not alive_map.get(s.name, False))
        ]
    # UNKNOWN sessions are skipped in both modes (need repair)
    sessions = [s for s in sessions if s.pid is not None]

    if not sessions:
        output.info("No matching sessions to restart.")
        return

    output.info(f"Restarting {len(sessions)} session(s)...")
    failed: list[tuple[str, str]] = []
    for session in sessions:
        try:
            restart_session(db, config, name=session.name, force=force, yes=include_running)
        except output.SessionError as exc:
            if not force and "broken" in str(exc).lower():
                output.warn(f"Skipping '{session.name}': {exc}")
            else:
                failed.append((session.name, str(exc)))
                output.warn(f"Error restarting '{session.name}': {exc}")
        except Exception as exc:
            failed.append((session.name, str(exc)))
            output.warn(f"Error restarting '{session.name}': {exc}")

    if failed:
        raise output.SessionError(f"{len(failed)} session(s) failed to restart.")


def delete_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a session. Prompts if running/unknown (--yes to skip). --force for BROKEN."""
    session = _require_session(db, name)
    ws, vm, run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-delete")
    health = check_session_health(session, target=target)

    if health == SessionHealth.OK:
        if not yes and not output.confirm(f"Session '{name}' is running. Delete?"):
            raise output.UserAbort("delete cancelled")
        sock = _effective_socket_path(db, session)
        _kill_session(name, run_command=run_command, socket_path=sock)
    elif health == SessionHealth.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to delete."
            )
        from agentworks.sessions.tmux import force_kill_tmux_server

        assert session.pid is not None
        force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail)
    elif health == SessionHealth.UNKNOWN:
        if not yes and not output.confirm(f"Session '{name}' has no PID (state unknown). Delete anyway?"):
            raise output.UserAbort("delete cancelled")

    # Final confirmation for STOPPED/BROKEN (OK/UNKNOWN already prompted above)
    if health in (SessionHealth.STOPPED, SessionHealth.BROKEN) and not yes and not output.confirm(
        f"Delete session '{name}'?"
    ):
        raise output.UserAbort("delete cancelled")

    # Clean up socket if present
    sock = _effective_socket_path(db, session)
    if sock:
        run_command(f"sudo rm -f {shlex.quote(sock)}", check=False)

    db.delete_session(name)

    # Clean up implicit grant for this session
    if session.agent_name:
        db.delete_agent_grant(session.agent_name, session.workspace_name, "implicit", session_name=name)
        # If no grants remain, remove from workspace group
        if not db.has_any_grant(session.agent_name, session.workspace_name):
            from agentworks.agents.manager import _remove_from_workspace_group

            agent = db.get_agent(session.agent_name)
            if agent:
                _remove_from_workspace_group(vm, config, agent.linux_user, session.workspace_name)

    _regenerate_tmuxinator(db, config, vm, ws)
    output.info(f"Session '{name}' deleted")

    # If this session created its workspace, offer to delete it
    if session.created_workspace:
        remaining = db.list_sessions(workspace_name=session.workspace_name)
        if remaining:
            output.detail(
                f"Workspace '{session.workspace_name}' was created with this session but has "
                f"{len(remaining)} other session(s), not offering to delete."
            )
        elif not yes:
            if output.confirm(
                f"Workspace '{session.workspace_name}' was created with this session "
                f"and has no other sessions. Delete it?",
            ):
                from agentworks.workspaces.manager import delete_workspace

                delete_workspace(db, config, session.workspace_name, yes=True)
        else:
            from agentworks.workspaces.manager import delete_workspace

            output.detail(f"Deleting workspace '{session.workspace_name}' (created with this session)...")
            delete_workspace(db, config, session.workspace_name, yes=True)


def describe_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Show session details."""
    session = _require_session(db, name)
    ws, vm, run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation=None)

    health = check_session_health(session, target=target)
    status_label = {
        SessionHealth.OK: "running",
        SessionHealth.STOPPED: "stopped",
        SessionHealth.BROKEN: "broken (PID alive, tmux unreachable -- run 'session repair')",
        SessionHealth.UNKNOWN: "unknown (no PID -- run 'session repair')",
    }[health]

    mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

    output.info(f"Name:       {session.name}")
    output.info(f"Workspace:  {session.workspace_name}")
    output.info(f"VM:         {vm.name}")
    output.info(f"Template:   {session.template}")
    output.info(f"Mode:       {mode_label}")
    output.info(f"Status:     {status_label}")
    if session.pid is not None and session.pid > 0:
        output.info(f"PID:        {session.pid}")
    output.info(f"Created:    {session.created_at}")
    output.info(f"Updated:    {session.updated_at}")


def _batch_check_all_sessions(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
) -> dict[str, bool]:
    """Batch PID check grouped by VM, parallel across VMs (capped at 8).

    Returns {session_name: alive}. Sessions with no reachable VM or no PID
    are excluded from the result.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_targets: dict[str, ExecTarget] = {}

    for s in sessions:
        ws = db.get_workspace(s.workspace_name)
        if not ws or ws.type != "vm" or not ws.vm_name:
            continue
        if ws.vm_name not in vm_targets:
            vm = db.get_vm(ws.vm_name)
            if not vm or not vm.tailscale_host:
                continue
            vm_targets[ws.vm_name] = admin_exec_target(vm, config)
        by_vm.setdefault(ws.vm_name, []).append(s)

    if not by_vm:
        return {}

    result_map: dict[str, bool] = {}

    def _check_vm(vm_name: str) -> dict[str, bool]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    with ThreadPoolExecutor(max_workers=min(8, len(by_vm))) as executor:
        futures = {executor.submit(_check_vm, name): name for name in by_vm}
        for future in as_completed(futures):
            result_map.update(future.result())

    return result_map


def list_sessions(
    db: Database,
    config: Config,
    *,
    workspace_name: str | None = None,
    no_status: bool = False,
) -> None:
    """List sessions with batch PID status checks (one SSH call per VM, parallel)."""
    sessions = db.list_sessions(workspace_name=workspace_name)
    if not sessions:
        output.info("No sessions found.")
        return

    # Batch PID check across all VMs in parallel
    alive_map: dict[str, bool] = {}
    if not no_status:
        alive_map = _batch_check_all_sessions(sessions, db=db, config=config)

    # Build table rows grouped by workspace
    by_workspace: dict[str, list[SessionRow]] = {}
    for session in sessions:
        by_workspace.setdefault(session.workspace_name, []).append(session)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for ws_name, ws_sessions in sorted(by_workspace.items()):
        ws = db.get_workspace(ws_name)
        vm_name = ws.vm_name or "-" if ws else "-"

        for session in ws_sessions:
            if no_status:
                status = "-"
            elif session.pid is None:
                status = "unknown"
            elif session.pid == PID_STOPPED:
                status = "stopped"
            elif session.name in alive_map:
                status = "running" if alive_map[session.name] else "stopped"
            else:
                status = "-"
            mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
            rows.append((session.name, ws_name, vm_name, session.template, mode_label, status))

    if not rows:
        output.info("No sessions found.")
        return

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    ws_w = max(len("WORKSPACE"), max(len(r[1]) for r in rows))
    vm_w = max(len("VM"), max(len(r[2]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[3]) for r in rows))
    mode_w = max(len("MODE"), max(len(r[4]) for r in rows))

    header = (
        f"{'NAME':<{name_w}}  {'WORKSPACE':<{ws_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  {'MODE':<{mode_w}}  STATUS"
    )
    output.info(header)
    output.info("-" * len(header))
    has_unknown = False
    for sname, ws_name, vm_col, tpl, mode, status in rows:
        output.info(
            f"{sname:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
        )
        if status == "unknown":
            has_unknown = True

    if has_unknown:
        output.info("")
        output.warn("Some sessions have no PID recorded. Run 'agentworks session repair --all' to recover.")


def attach_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Attach to a session's tmux session (interactive)."""
    from agentworks.sessions.tmux import tmux_cmd
    from agentworks.ssh import interactive

    session = _require_session(db, name)
    _ws, vm, _run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-attach")
    health = check_session_health(session, target=target)

    if health == SessionHealth.STOPPED:
        raise output.SessionError(f"session '{name}' is not running")
    if health == SessionHealth.BROKEN:
        raise output.SessionError(
            f"session '{name}' is broken (PID alive but tmux unreachable). Run 'session repair {name}'."
        )
    if health == SessionHealth.UNKNOWN:
        raise output.SessionError(f"session '{name}' has no PID recorded. Run 'session repair {name}'.")

    q_session = shlex.quote(name)
    sys.exit(interactive(target, tmux_cmd(f"attach -t {q_session}", session.socket_path)))


def session_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    lines: int | None = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.sessions.tmux import capture_output

    session = _require_session(db, name)
    _ws, _vm, run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-logs")
    health = check_session_health(session, target=target)

    if health == SessionHealth.STOPPED:
        raise output.SessionError(f"session '{name}' is not running")
    if health == SessionHealth.BROKEN:
        raise output.SessionError(
            f"session '{name}' is broken (PID alive but tmux unreachable). Run 'session repair {name}'."
        )
    if health == SessionHealth.UNKNOWN:
        raise output.SessionError(f"session '{name}' has no PID recorded. Run 'session repair {name}'.")

    sock = _effective_socket_path(db, session)
    captured = capture_output(
        name,
        run_command=run_command,
        lines=lines or config.session.history_limit,
        socket_path=sock,
    )
    # Raw data pipe (opaque tmux capture-pane output), not a structured message.
    # Intentionally not routed through the output handler.
    typer.echo(captured, nl=False)


def repair_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Recover the PID for a session from its running tmux server."""
    from agentworks.sessions.tmux import get_tmux_server_pid

    session = _require_session(db, name)
    if session.pid is not None:
        output.info(f"Session '{name}' already has PID {session.pid}, skipped")
        return

    _ws, _vm, _run_command, _, target = _prepare_vm(db, config, session.workspace_name, operation="session-repair")
    sock = _effective_socket_path(db, session)
    pid = get_tmux_server_pid(target=target, socket_path=sock)

    if pid is not None:
        db.update_session_pid(name, pid)
        output.info(f"Recovered PID {pid} for session '{name}'")
    else:
        db.update_session_pid(name, PID_STOPPED)
        output.info(f"Session '{name}' is not running")


def repair_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | None = None,
    workspace_name: str | None = None,
) -> None:
    """Batch-recover PIDs for sessions missing them."""
    from agentworks.sessions.tmux import get_tmux_server_pid

    sessions = db.list_sessions(workspace_name=workspace_name)
    sessions = [s for s in sessions if s.pid is None]

    if vm_name is not None:
        vm_workspaces = {ws.name for ws in db.list_workspaces(vm_name=vm_name)}
        sessions = [s for s in sessions if s.workspace_name in vm_workspaces]

    if not sessions:
        output.info("No sessions need repair.")
        return

    # Group by workspace for VM target reuse
    by_ws: dict[str, list[SessionRow]] = {}
    for s in sessions:
        by_ws.setdefault(s.workspace_name, []).append(s)

    repaired = 0
    for ws_name, ws_sessions in by_ws.items():
        ws = db.get_workspace(ws_name)
        if not ws or ws.type != "vm" or not ws.vm_name:
            continue
        vm = db.get_vm(ws.vm_name)
        if not vm or not vm.tailscale_host:
            continue
        target = admin_exec_target(vm, config)

        for session in ws_sessions:
            sock = _effective_socket_path(db, session)
            pid = get_tmux_server_pid(target=target, socket_path=sock)
            if pid is not None:
                db.update_session_pid(session.name, pid)
                output.info(f"Recovered PID {pid} for session '{session.name}'")
            else:
                db.update_session_pid(session.name, PID_STOPPED)
                output.info(f"Session '{session.name}' is not running")
            repaired += 1

    output.info(f"Repair complete: {repaired} session(s) repaired")
