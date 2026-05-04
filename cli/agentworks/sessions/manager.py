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
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
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



def _kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> bool:
    """Kill a session on its expected tmux server. Returns True if successful."""
    from agentworks.sessions.tmux import kill_session

    return kill_session(session_name, run_command=run_command, socket_path=socket_path)


def _ensure_pid(session: SessionRow, *, target: ExecTarget, db: Database) -> SessionRow:
    """Auto-recover PID + boot ID for a session missing either. Returns updated session.

    Triggers when pid is NULL or boot_id is NULL. Always writes both together.
    If the tmux server is running, stores the PID + boot ID. If genuinely
    stopped (no socket file), marks as PID_STOPPED. If ambiguous (socket
    exists but tmux unreachable), leaves as NULL and warns.
    """
    if session.pid is not None and session.boot_id is not None:
        return session

    from agentworks.sessions.tmux import get_tmux_server_pid, tmux_cmd

    sock = session.socket_path
    q_session = shlex.quote(session.name)

    # Step 1: try has-session (the primary liveness check)
    has_cmd = tmux_cmd(f"has-session -t {q_session}", sock) + " 2>/dev/null"
    if target.run(has_cmd, check=False).ok:
        # Session is alive -- recover PID + boot ID
        pid = get_tmux_server_pid(target=target, socket_path=sock)
        if pid is not None:
            db.update_session_pid(session.name, pid, boot_id=_get_boot_id(target))
            output.warn(f"Recovered PID {pid} for session '{session.name}'")
        else:
            # has-session succeeded but display-message failed -- session is alive but we
            # can't get the PID. Leave as NULL (UNKNOWN) rather than contradicting has-session.
            output.warn(f"Session '{session.name}' is alive but PID recovery failed")
    elif sock and target.run(f"test -e {shlex.quote(sock)}", check=False).ok:
        # Socket exists but has-session failed. Probe with sudo to distinguish
        # stale socket (dead server) from live-but-unreachable server (permissions).
        probe_cmd = tmux_cmd("list-sessions", sock, sudo=True) + " 2>/dev/null"
        if target.run(probe_cmd, check=False).ok:
            # Server is alive but unreachable without sudo -- permission drift.
            # Leave as NULL so the caller's UNKNOWN handler surfaces the issue.
            output.warn(
                f"Session '{session.name}' has a live tmux server but it is unreachable. "
                "This may indicate a permissions issue."
            )
            return session
        else:
            # Stale socket, server is dead
            db.update_session_pid(session.name, PID_STOPPED)
            output.warn(f"Session '{session.name}' is not running, marked stopped")
    else:
        # No socket (or admin session) and has-session failed -- genuinely stopped
        db.update_session_pid(session.name, PID_STOPPED)
        output.warn(f"Session '{session.name}' is not running, marked stopped")

    result = db.get_session(session.name)
    assert result is not None
    return result


def ensure_pids_batch(sessions: list[SessionRow], *, db: Database, config: Config) -> list[SessionRow]:
    """Auto-recover PIDs for all sessions with pid=NULL. Returns updated list.

    Same logic as _ensure_pid but for batch commands. Socket ambiguity (socket
    exists but tmux unreachable) leaves pid=NULL so callers see UNKNOWN.
    """
    from agentworks.sessions.tmux import get_tmux_server_pid, tmux_cmd

    need_repair = [s for s in sessions if s.pid is None or s.boot_id is None]
    if not need_repair:
        return sessions

    # Group by workspace to resolve VM targets
    by_ws: dict[str, list[SessionRow]] = {}
    for s in need_repair:
        by_ws.setdefault(s.workspace_name, []).append(s)

    repaired_names: set[str] = set()
    for ws_name, ws_sessions in by_ws.items():
        ws = db.get_workspace(ws_name)
        if not ws or ws.type != "vm" or not ws.vm_name:
            continue
        vm = db.get_vm(ws.vm_name)
        if not vm or not vm.tailscale_host:
            continue
        target = admin_exec_target(vm, config)
        for session in ws_sessions:
            sock = session.socket_path
            q_session = shlex.quote(session.name)

            # Step 1: try has-session
            has_cmd = tmux_cmd(f"has-session -t {q_session}", sock) + " 2>/dev/null"
            if target.run(has_cmd, check=False).ok:
                # Alive -- recover PID + boot ID
                pid = get_tmux_server_pid(target=target, socket_path=sock)
                if pid is not None:
                    db.update_session_pid(session.name, pid, boot_id=_get_boot_id(target))
                    output.warn(f"Recovered PID {pid} for session '{session.name}'")
                else:
                    # has-session succeeded but display-message failed -- leave as UNKNOWN
                    output.warn(f"Session '{session.name}' is alive but PID recovery failed")
                    continue
                repaired_names.add(session.name)
            elif sock and target.run(f"test -e {shlex.quote(sock)}", check=False).ok:
                # Socket exists, has-session failed -- sudo probe for stale vs live
                probe_cmd = tmux_cmd("list-sessions", sock, sudo=True) + " 2>/dev/null"
                if target.run(probe_cmd, check=False).ok:
                    output.warn(
                        f"Session '{session.name}' has a live tmux server but it is unreachable. "
                        "This may indicate a permissions issue."
                    )
                else:
                    db.update_session_pid(session.name, PID_STOPPED)
                    output.warn(f"Session '{session.name}' is not running, marked stopped")
                    repaired_names.add(session.name)
            else:
                db.update_session_pid(session.name, PID_STOPPED)
                output.warn(f"Session '{session.name}' is not running, marked stopped")
                repaired_names.add(session.name)

    # Return original list with repaired sessions refreshed from DB
    if not repaired_names:
        return sessions
    result = []
    for s in sessions:
        if s.name in repaired_names:
            refreshed = db.get_session(s.name)
            result.append(refreshed if refreshed else s)
        else:
            result.append(s)
    return result


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
    socket_paths = {s.name: s.socket_path for s in sessions}
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions, socket_paths=socket_paths)
    target = admin_exec_target(vm, config)
    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", config_text, logger=logger)


def filter_sessions(
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


def _pid_alive(pid: int, *, target: ExecTarget) -> bool:
    """Check if a PID is alive via /proc. Internal helper."""
    return target.run(f"test -d /proc/{pid}", check=False).ok


def _get_boot_id(target: ExecTarget) -> str:
    """Read the current VM boot ID."""
    result = target.run("cat /proc/sys/kernel/random/boot_id", check=False)
    return (getattr(result, "stdout", "") or "").strip()


def check_session_status(
    session: SessionRow,
    *,
    target: ExecTarget,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

    Pure function -- no DB side effects.
    """
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED
    if session.pid is None or session.boot_id is None:
        return SessionStatus.UNKNOWN

    if session.mode == SessionMode.AGENT.value and session.socket_path is not None:
        return _check_dedicated_agent_session(session, target=target)
    if session.mode == SessionMode.ADMIN.value and session.socket_path is None:
        return _check_shared_admin_session(session, target=target)
    raise RuntimeError(f"unexpected session config: mode={session.mode}, socket_path={session.socket_path}")


def _check_dedicated_agent_session(session: SessionRow, *, target: ExecTarget) -> SessionStatus:
    """Agent sessions with their own tmux server and socket."""
    from agentworks.sessions.tmux import tmux_cmd

    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}", session.socket_path) + " 2>/dev/null"
    if target.run(cmd, check=False).ok:
        return SessionStatus.OK

    # has-session failed -- STOPPED or BROKEN?
    assert session.pid is not None and session.pid > 0
    if session.boot_id and session.boot_id != _get_boot_id(target):
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if not _pid_alive(session.pid, target=target):
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # process alive, socket unreachable


def _check_shared_admin_session(session: SessionRow, *, target: ExecTarget) -> SessionStatus:
    """Admin sessions on the default tmux server. BROKEN does not apply."""
    from agentworks.sessions.tmux import tmux_cmd

    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}") + " 2>/dev/null"
    if target.run(cmd, check=False).ok:
        return SessionStatus.OK
    return SessionStatus.STOPPED


def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: ExecTarget,
) -> dict[str, SessionStatus]:
    """Check status for multiple sessions in one SSH call per VM.

    Returns {session_name: SessionStatus}. Sessions with pid=None or PID_STOPPED
    are excluded (callers handle those via the enum directly).
    """
    from agentworks.sessions.tmux import tmux_cmd

    checkable = [s for s in sessions if s.pid is not None and s.pid > 0 and s.boot_id is not None]
    if not checkable:
        return {}

    # Build compound command: has-session with inline boot_id + PID for agent failures
    parts = []
    for s in checkable:
        q_session = shlex.quote(s.name)  # quoted for tmux -t argument
        name = s.name  # raw for output field (names are validated, no shell-special chars)
        has_cmd = tmux_cmd(f"has-session -t {q_session}", s.socket_path)
        if s.socket_path is not None:
            # Agent session: inline follow-up on failure
            parts.append(
                f"{has_cmd} 2>/dev/null; "
                f"if [ $? -ne 0 ]; then "
                f"BOOT=$(cat /proc/sys/kernel/random/boot_id); "
                f"test -d /proc/{s.pid}; "
                f"echo \"S:{name}:1:$BOOT:$?\"; "
                f"else echo \"S:{name}:0\"; fi"
            )
        else:
            # Admin session: has-session only
            parts.append(f"{has_cmd} 2>/dev/null; echo \"S:{name}:$?\"")
    cmd = "; ".join(parts)

    result = target.run(cmd, check=False)
    stdout = getattr(result, "stdout", "") or ""

    status_map: dict[str, SessionStatus] = {}
    # Build a quick lookup for stored boot_ids
    boot_ids = {s.name: s.boot_id for s in checkable}

    for line in stdout.strip().splitlines():
        if not line.startswith("S:"):
            continue
        fields = line.split(":", maxsplit=4)
        if len(fields) < 3:
            continue
        name = fields[1]
        exit_code = fields[2]

        if exit_code == "0":
            status_map[name] = SessionStatus.OK
        elif len(fields) == 5:
            # Agent session failure: S:name:1:<boot_id>:<pid_exit>
            current_boot = fields[3]
            pid_exit = fields[4]
            stored_boot = boot_ids.get(name)
            if stored_boot and stored_boot != current_boot:
                status_map[name] = SessionStatus.STOPPED  # stale boot
            elif pid_exit == "0":
                status_map[name] = SessionStatus.BROKEN  # PID alive, socket unreachable
            else:
                status_map[name] = SessionStatus.STOPPED  # PID dead
        else:
            # Admin session failure
            status_map[name] = SessionStatus.STOPPED

    return status_map


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

    # Persist socket path, PID, and boot ID
    if sock:
        db.update_session_socket_path(name, sock)
    db.update_session_pid(name, pid, boot_id=_get_boot_id(target))

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

    # Phase 1: send C-c to all sessions (best effort).
    # This gives processes that handle SIGINT gracefully (save state, flush)
    # a chance to clean up before we kill the session. In practice, tmux
    # kill-session sends SIGHUP which cascades through the shell to children,
    # so the C-c is rarely necessary. Consider removing the C-c + grace
    # period if the 5-second wait becomes a pain point.
    output.detail("Sending C-c to stop any running commands...")
    for session, target in targets:
        sock = session.socket_path
        run_cmd = partial(run, target, logger=target.logger)
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

    survivor_map: dict[str, SessionStatus] = {}
    for target, group in by_target.values():
        survivor_map.update(batch_check_status(group, target=target))

    failed: list[tuple[str, str]] = []

    for session, target in targets:
        status = survivor_map.get(session.name)
        if status == SessionStatus.OK or status == SessionStatus.BROKEN:
            output.detail(f"Killing session '{session.name}'")
            sock = session.socket_path
            run_cmd = partial(run, target, logger=target.logger)
            killed = _kill_session(session.name, run_command=run_cmd, socket_path=sock)
            if not killed:
                # Only escalate to PID kill for agent sessions (dedicated socket).
                # Admin sessions share a PID -- killing it would take down all admin sessions.
                if force and session.socket_path is not None and session.pid and session.pid > 0:
                    output.detail(f"tmux kill failed for '{session.name}', force-killing PID {session.pid}")
                    if not force_kill_tmux_server(
                        session.pid, target=target, socket_path=session.socket_path, log=output.detail,
                    ):
                        failed.append((session.name, f"PID {session.pid} survived force-kill"))
                        continue
                else:
                    failed.append((session.name, f"tmux kill-session failed for '{session.name}'"))
                    output.warn(f"Failed to stop '{session.name}' (tmux unreachable, use --force)")
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
    session = _ensure_pid(session, target=target, db=db)
    health = check_session_status(session, target=target)

    if health == SessionStatus.STOPPED:
        output.info(f"Session '{name}' is already stopped")
        return
    if health == SessionStatus.UNKNOWN:
        raise output.SessionError(
            f"session '{name}' has no PID and auto-recovery failed. Investigate the tmux server manually."
        )
    if health == SessionStatus.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to kill the process."
            )
        assert session.pid is not None
        if not force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail):
            raise output.SessionError(f"failed to kill PID {session.pid} for session '{name}'")
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
    session = _ensure_pid(session, target=target, db=db)
    health = check_session_status(session, target=target)

    if health == SessionStatus.UNKNOWN:
        raise output.SessionError(
            f"session '{name}' has no PID and auto-recovery failed. Investigate the tmux server manually."
        )
    if health == SessionStatus.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to restart."
            )
        from agentworks.sessions.tmux import force_kill_tmux_server

        assert session.pid is not None
        if not force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail):
            raise output.SessionError(f"failed to kill PID {session.pid} for session '{name}'")
    elif health == SessionStatus.OK:
        if not yes and not output.confirm(f"Session '{name}' is running. Restart?"):
            raise output.UserAbort("restart cancelled")
        sock = session.socket_path
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
    db.update_session_pid(name, pid, boot_id=_get_boot_id(target))

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
    sessions = filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)

    # Auto-repair NULL-PID sessions, then batch check
    sessions = ensure_pids_batch(sessions, db=db, config=config)
    status_map = batch_check_all_sessions(sessions, db=db, config=config)
    alive_sessions = [
        s
        for s in sessions
        if status_map.get(s.name) in (SessionStatus.OK, SessionStatus.BROKEN)
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
    sessions = filter_sessions(db, workspace_name=workspace_name, vm_name=vm_name)

    # Auto-repair NULL-PID sessions, then batch check
    sessions = ensure_pids_batch(sessions, db=db, config=config)
    status_map = batch_check_all_sessions(sessions, db=db, config=config)

    if not include_running:
        # Only stopped sessions
        sessions = [
            s
            for s in sessions
            if s.pid == PID_STOPPED
            or status_map.get(s.name) == SessionStatus.STOPPED
        ]

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
    session = _ensure_pid(session, target=target, db=db)
    health = check_session_status(session, target=target)

    if health == SessionStatus.UNKNOWN:
        raise output.SessionError(
            f"session '{name}' has no PID and auto-recovery failed. Investigate the tmux server manually."
        )
    if health == SessionStatus.OK:
        if not yes and not output.confirm(f"Session '{name}' is running. Delete?"):
            raise output.UserAbort("delete cancelled")
        sock = session.socket_path
        _kill_session(name, run_command=run_command, socket_path=sock)
    elif health == SessionStatus.BROKEN:
        if not force:
            raise output.SessionError(
                f"session '{name}' is broken (PID alive but tmux unreachable). Use --force to delete."
            )
        from agentworks.sessions.tmux import force_kill_tmux_server

        assert session.pid is not None
        if not force_kill_tmux_server(session.pid, target=target, socket_path=session.socket_path, log=output.detail):
            raise output.SessionError(f"failed to kill PID {session.pid} for session '{name}'")

    # Final confirmation for STOPPED/BROKEN (OK/UNKNOWN already prompted above)
    if health in (SessionStatus.STOPPED, SessionStatus.BROKEN) and not yes and not output.confirm(
        f"Delete session '{name}'?"
    ):
        raise output.UserAbort("delete cancelled")

    # Clean up socket if the server is dead (don't remove a live socket)
    sock = session.socket_path
    if sock:
        post_status = check_session_status(session, target=target)
        if post_status != SessionStatus.OK and post_status != SessionStatus.BROKEN:
            run_command(f"sudo rm -f {shlex.quote(sock)}", check=False)
        else:
            output.warn(f"tmux server for '{name}' is still alive, socket preserved at {sock}")

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
    session = _ensure_pid(session, target=target, db=db)

    status = check_session_status(session, target=target)

    # Build status label with PID if running and current boot
    if status == SessionStatus.OK and session.pid and session.pid > 0:
        status_label = f"running (PID {session.pid})"
    elif status == SessionStatus.BROKEN and session.pid and session.pid > 0:
        status_label = f"broken (PID {session.pid} alive, tmux unreachable)"
    else:
        status_label = {
            SessionStatus.OK: "running",
            SessionStatus.STOPPED: "stopped",
            SessionStatus.BROKEN: "broken",
            SessionStatus.UNKNOWN: "unknown",
        }[status]

    mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

    output.info(f"Name:       {session.name}")
    output.info(f"Workspace:  {session.workspace_name}")
    output.info(f"VM:         {vm.name}")
    output.info(f"Template:   {session.template}")
    output.info(f"Mode:       {mode_label}")
    output.info(f"Status:     {status_label}")
    output.info(f"Created:    {session.created_at}")
    output.info(f"Updated:    {session.updated_at}")


def batch_check_all_sessions(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
) -> dict[str, SessionStatus]:
    """Batch status check grouped by VM, parallel across VMs (capped at 8).

    Returns {session_name: SessionStatus}. Sessions with no reachable VM or
    pid=None/PID_STOPPED are excluded from the result.
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

    result_map: dict[str, SessionStatus] = {}

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    with ThreadPoolExecutor(max_workers=min(8, len(by_vm))) as executor:
        futures = {executor.submit(_check_vm, name): name for name in by_vm}
        for future in as_completed(futures):
            vm_name = futures[future]
            try:
                result_map.update(future.result())
            except Exception as exc:
                output.warn(f"Failed to check sessions on VM '{vm_name}': {exc}")

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

    # Auto-repair sessions with missing PIDs, then batch check
    if not no_status:
        sessions = ensure_pids_batch(sessions, db=db, config=config)
    status_map: dict[str, SessionStatus] = {}
    if not no_status:
        status_map = batch_check_all_sessions(sessions, db=db, config=config)

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
            elif session.name in status_map:
                s_status = status_map[session.name]
                status = {
                    SessionStatus.OK: "running",
                    SessionStatus.STOPPED: "stopped",
                    SessionStatus.BROKEN: "broken",
                    SessionStatus.UNKNOWN: "unknown",
                }[s_status]
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
    broken_names = []
    unknown_names = []
    for sname, ws_name, vm_col, tpl, mode, status in rows:
        output.info(
            f"{sname:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
        )
        if status == "broken":
            broken_names.append(sname)
        elif status == "unknown":
            unknown_names.append(sname)

    if broken_names or unknown_names:
        output.info("")
        if broken_names:
            output.warn(
                f"{len(broken_names)} session(s) are broken (tmux unreachable): "
                f"{', '.join(broken_names)}. Use restart/stop/delete --force."
            )
        if unknown_names:
            output.warn(
                f"{len(unknown_names)} session(s) have unknown status: "
                f"{', '.join(unknown_names)}. Status could not be determined."
            )


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
    session = _ensure_pid(session, target=target, db=db)
    health = check_session_status(session, target=target)

    if health == SessionStatus.STOPPED:
        raise output.SessionError(f"session '{name}' is not running")
    if health == SessionStatus.BROKEN:
        raise output.SessionError(
            f"session '{name}' is broken (PID alive but tmux unreachable)."
        )

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
    session = _ensure_pid(session, target=target, db=db)
    health = check_session_status(session, target=target)

    if health == SessionStatus.STOPPED:
        raise output.SessionError(f"session '{name}' is not running")
    if health == SessionStatus.BROKEN:
        raise output.SessionError(
            f"session '{name}' is broken (PID alive but tmux unreachable)."
        )

    sock = session.socket_path
    captured = capture_output(
        name,
        run_command=run_command,
        lines=lines or config.session.history_limit,
        socket_path=sock,
    )
    # Raw data pipe (opaque tmux capture-pane output), not a structured message.
    # Intentionally not routed through the output handler.
    typer.echo(captured, nl=False)


