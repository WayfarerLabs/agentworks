"""Session lifecycle orchestration."""

from __future__ import annotations

import re
import shlex
import sys
from functools import partial
from typing import TYPE_CHECKING

import typer

from agentworks import output
from agentworks.db import SessionMode, SessionStatus
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
    from agentworks.ssh import SSHLogger


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
) -> tuple[WorkspaceRow, VMRow, RunCommand, RunCommand]:
    """Validate workspace/VM, ensure running, and return (ws, vm, run_command, run_as_root).

    If operation is set, creates an SSHLogger and binds it into both callables.
    """
    from agentworks.ssh import SSHLogger, run
    from agentworks.ssh import run_as_root as ssh_run_as_root

    ws = _require_workspace(db, workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        raise output.VMError(f"VM '{vm.name}' has no Tailscale address")

    target = admin_exec_target(vm, config)
    logger = SSHLogger(vm.name, operation) if operation else None
    run_command = partial(run, target, logger=logger)
    run_as_root = partial(ssh_run_as_root, target, logger=logger)
    return ws, vm, run_command, run_as_root


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


def _reconcile_status(
    session: SessionRow,
    *,
    run_command: RunCommand,
    db: Database,
) -> str:
    """Check tmux session and reconcile session status in the DB."""
    sock = _effective_socket_path(db, session)
    alive = _session_alive(session.name, run_command=run_command, socket_path=sock)
    if session.status == SessionStatus.RUNNING.value and not alive:
        db.update_session_status(session.name, SessionStatus.STOPPED)
        return SessionStatus.STOPPED.value
    if session.status == SessionStatus.STOPPED.value and alive:
        db.update_session_status(session.name, SessionStatus.RUNNING)
        return SessionStatus.RUNNING.value
    return session.status


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
    ws, vm, run_command, run_as_root = _prepare_vm(db, config, workspace_name, operation="session-create")

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

    # Insert DB record first to avoid orphaned tmux sessions on crash
    db.insert_session(
        name,
        workspace_name,
        template.name,
        mode,
        agent_name=resolved_agent_name,
        created_workspace=created_workspace,
    )

    deploy_restricted_config(run_command, history_limit=config.session.history_limit)
    command = _build_session_command(template, session_name=name, workspace_name=workspace_name)

    try:
        sock = create_tmux_session(
            name,
            ws.workspace_path,
            command,
            linux_user,
            run_command=run_command,
            run_as_root=run_as_root,
            admin_username=vm.admin_username,
            is_admin=(mode == SessionMode.ADMIN),
        )
    except Exception:
        db.delete_session(name)
        if resolved_agent_name:
            db.delete_agent_grant(resolved_agent_name, workspace_name, "implicit", session_name=name)
        raise

    # Persist socket path
    if sock:
        db.update_session_socket_path(name, sock)

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    output.info(f"Session '{name}' started ({mode_label}, template: {template.name})")

    # Update tmuxinator config and add to console if it exists
    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=sock)


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Stop a running session. Sends C-c first, then kills after a grace period."""
    import time

    from agentworks.sessions.tmux import send_keys

    session = _require_session(db, name)
    _ws, _vm, run_command, _ = _prepare_vm(db, config, session.workspace_name, operation="session-stop")

    if session.status == SessionStatus.STOPPED.value:
        output.info(f"Session '{name}' is already stopped")
        return

    sock = _effective_socket_path(db, session)

    # Send C-c to the running process
    send_keys(name, "C-c", run_command=run_command, socket_path=sock)

    # Wait for graceful exit
    time.sleep(_STOP_GRACE_SECONDS)

    # Kill if still alive on the session's expected tmux server
    if _session_alive(name, run_command=run_command, socket_path=sock):
        _kill_session(name, run_command=run_command, socket_path=sock)

    db.update_session_status(name, SessionStatus.STOPPED)
    output.info(f"Session '{name}' stopped")


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
) -> None:
    """Restart a session. Errors if running unless --force is passed."""
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    session = _require_session(db, name)
    ws, vm, run_command, run_as_root = _prepare_vm(db, config, session.workspace_name, operation="session-restart")
    sock = _effective_socket_path(db, session)

    if _session_alive(name, run_command=run_command, socket_path=sock):
        if not force:
            raise output.SessionError(
                f"session '{name}' is still running. Stop it first, or use --force."
            )
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

    new_sock = create_tmux_session(
        name,
        ws.workspace_path,
        command,
        linux_user,
        run_command=run_command,
        run_as_root=run_as_root,
        admin_username=vm.admin_username,
        is_admin=is_admin,
    )

    # Persist socket path if it differs from what's stored. Compare against
    # session.socket_path (not the derived effective path) so migrated sessions
    # with NULL socket_path get backfilled on restart.
    if new_sock != session.socket_path:
        db.update_session_socket_path(name, new_sock)

    db.update_session_status(name, SessionStatus.RUNNING)
    output.info(f"Session '{name}' restarted")

    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=new_sock)


def restart_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | None = None,
    workspace_name: str | None = None,
    include_running: bool = False,
) -> None:
    """Restart all stopped sessions, optionally filtered by VM or workspace.

    If include_running is True, running sessions are killed and restarted too.
    """
    sessions = db.list_sessions(workspace_name=workspace_name)

    # Filter by VM if requested (requires workspace -> VM lookup)
    if vm_name is not None:
        vm_workspaces = {ws.name for ws in db.list_workspaces(vm_name=vm_name)}
        sessions = [s for s in sessions if s.workspace_name in vm_workspaces]

    # Filter by status
    if not include_running:
        sessions = [s for s in sessions if s.status == SessionStatus.STOPPED.value]

    if not sessions:
        output.info("No matching sessions to restart.")
        return

    output.info(f"Restarting {len(sessions)} session(s)...")
    failed: list[tuple[str, str]] = []
    for session in sessions:
        try:
            restart_session(
                db,
                config,
                name=session.name,
                force=include_running,
            )
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
    yes: bool = False,
) -> None:
    """Stop and delete a session."""
    session = _require_session(db, name)
    ws, vm, run_command, _ = _prepare_vm(db, config, session.workspace_name, operation="session-delete")
    sock = _effective_socket_path(db, session)

    if (
        not yes
        and _session_alive(name, run_command=run_command, socket_path=sock)
        and not output.confirm(f"Session '{name}' is still running. Delete anyway?")
    ):
        raise output.UserAbort("delete cancelled")

    _kill_session(name, run_command=run_command, socket_path=sock)

    # Remove stale socket file if the tmux server has exited.
    # If the kill failed, warn and leave the socket for debugging.
    if sock:
        import shlex

        from agentworks.sessions.tmux import session_exists

        if session_exists(name, run_command=run_command, socket_path=sock):
            output.warn(
                f"tmux session '{name}' is still running after kill. "
                f"Socket preserved at {sock}"
            )
        else:
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
    ws, vm, run_command, _ = _prepare_vm(db, config, session.workspace_name, operation=None)

    # Reconcile status with tmux
    status = _reconcile_status(session, run_command=run_command, db=db)
    if status != session.status:
        session = _require_session(db, name)

    mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

    output.info(f"Name:       {session.name}")
    output.info(f"Workspace:  {session.workspace_name}")
    output.info(f"VM:         {vm.name}")
    output.info(f"Template:   {session.template}")
    output.info(f"Mode:       {mode_label}")
    output.info(f"Status:     {session.status}")
    output.info(f"Created:    {session.created_at}")
    output.info(f"Updated:    {session.updated_at}")


def list_sessions(
    db: Database,
    config: Config,
    *,
    workspace_name: str | None = None,
    no_status: bool = False,
) -> None:
    """List sessions, optionally reconciling status with tmux."""
    sessions = db.list_sessions(workspace_name=workspace_name)
    if not sessions:
        output.info("No sessions found.")
        return

    # Build alive_map: session_name -> bool, batched by VM and parallelized
    # across VMs. Each VM gets one SSH call with all its session checks.
    alive_map: dict[str, bool] = {}

    # Cache workspace and VM lookups (used for grouping and display)
    ws_cache: dict[str, WorkspaceRow | None] = {}
    vm_cache: dict[str, VMRow | None] = {}

    def _get_ws(name: str) -> WorkspaceRow | None:
        if name not in ws_cache:
            ws_cache[name] = db.get_workspace(name)
        return ws_cache[name]

    def _get_vm(name: str) -> VMRow | None:
        if name not in vm_cache:
            vm_cache[name] = db.get_vm(name)
        return vm_cache[name]

    if not no_status:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from agentworks.sessions.tmux import batch_check_sessions

        # Group sessions by VM, precomputing socket paths on the main thread
        # (DB is not thread-safe with check_same_thread=True).
        by_vm: dict[str, list[tuple[str, str | None]]] = {}  # vm_name -> [(session_name, socket)]
        for session in sessions:
            ws = _get_ws(session.workspace_name)
            if ws is None or ws.type != "vm" or ws.vm_name is None:
                continue
            vm = _get_vm(ws.vm_name)
            if vm is None or vm.tailscale_host is None:
                continue
            sock = _effective_socket_path(db, session)
            by_vm.setdefault(ws.vm_name, []).append((session.name, sock))

        def _check_vm(vm_name: str, checks: list[tuple[str, str | None]]) -> dict[str, bool]:
            vm = _get_vm(vm_name)
            if vm is None:
                output.warn(f"VM '{vm_name}' not found, skipping status check")
                return {}
            target = admin_exec_target(vm, config)
            return batch_check_sessions(target, checks)

        # Hit all VMs in parallel (cap at 8 to avoid overwhelming SSH)
        with ThreadPoolExecutor(max_workers=min(len(by_vm), 8) or 1) as pool:
            futures = {
                pool.submit(_check_vm, name, checks): name
                for name, checks in by_vm.items()
            }
            for future in as_completed(futures):
                try:
                    alive_map.update(future.result())
                except Exception as e:
                    vm_name = futures[future]
                    output.warn(f"status check failed for VM '{vm_name}': {e}")

    # Build display rows, reconciling status from alive_map
    rows: list[tuple[str, str, str, str, str, str]] = []
    for session in sessions:
        ws = _get_ws(session.workspace_name)
        vm_name = (ws.vm_name or "-") if ws else "-"
        mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

        if session.name in alive_map:
            alive = alive_map[session.name]
            status = session.status
            if session.status == SessionStatus.RUNNING.value and not alive:
                db.update_session_status(session.name, SessionStatus.STOPPED)
                status = SessionStatus.STOPPED.value
            elif session.status == SessionStatus.STOPPED.value and alive:
                db.update_session_status(session.name, SessionStatus.RUNNING)
                status = SessionStatus.RUNNING.value
        else:
            status = session.status

        rows.append((session.name, session.workspace_name, vm_name, session.template, mode_label, status))

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
    for sname, ws_name, vm_col, tpl, mode, status in rows:
        output.info(
            f"{sname:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
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
    _ws, vm, run_command, _ = _prepare_vm(db, config, session.workspace_name, operation="session-attach")
    sock = _effective_socket_path(db, session)

    if not _session_alive(name, run_command=run_command, socket_path=sock):
        raise output.SessionError(f"session '{name}' is not running")

    q_session = shlex.quote(name)
    target = admin_exec_target(vm, config)
    sys.exit(interactive(target, tmux_cmd(f"attach -t {q_session}", sock)))


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
    _ws, _vm, run_command, _ = _prepare_vm(db, config, session.workspace_name, operation="session-logs")
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
