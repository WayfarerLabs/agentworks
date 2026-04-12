"""Session lifecycle orchestration."""

from __future__ import annotations

import re
import shlex
import sys
from functools import partial
from typing import TYPE_CHECKING

import typer

from agentworks.db import SessionMode, SessionStatus
from agentworks.ssh import ssh_target_for_vm

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Template variable substitution -- uses {{var}} syntax consistent with nerftools.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.ssh import SSHLogger
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.sessions.tmux import RunCommand


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
            typer.echo(f"Error: agent '{session.agent_name}' not found (referenced by session '{session.name}')", err=True)
            raise typer.Exit(1)
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


def _kill_session_any_server(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> None:
    """Kill a session on both the agent socket and the default server.

    Handles migration from the old model (agent sessions on the admin's
    default tmux server) to the new model (per-agent sockets). Safe to call
    even if the session exists on neither or both.
    """
    from agentworks.sessions.tmux import kill_session

    if socket_path:
        kill_session(session_name, run_command=run_command, socket_path=socket_path)
    # Always try the default server too, to clean up legacy sessions
    kill_session(session_name, run_command=run_command)


def _session_exists_any_server(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
    warn_legacy: bool = True,
) -> bool:
    """Check if a session exists on either the agent socket or the default server."""
    from agentworks.sessions.tmux import session_exists

    if socket_path and session_exists(session_name, run_command=run_command, socket_path=socket_path):
        return True
    on_default = session_exists(session_name, run_command=run_command)
    if on_default and socket_path and warn_legacy:
        typer.echo(
            f"  Note: agent session '{session_name}' is running on the default tmux server "
            f"(legacy mode). Restart it to use the new per-agent socket.",
            err=True,
        )
    return on_default


def _require_workspace(db: Database, name: str) -> WorkspaceRow:
    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    if ws.type != "vm":
        typer.echo("Error: sessions are only supported on VM workspaces", err=True)
        raise typer.Exit(1)
    vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
    if vm is None:
        typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
        raise typer.Exit(1)
    return vm


def _prepare_vm(
    db: Database, config: Config, workspace_name: str, *, operation: str | None = None
) -> tuple[WorkspaceRow, VMRow, RunCommand]:
    """Validate workspace/VM, ensure running, and return (ws, vm, run_command).

    If operation is set, creates an SSHLogger and binds it into run_command.
    """
    from agentworks.ssh import SSHLogger, run

    ws = _require_workspace(db, workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{vm.name}' has no Tailscale address", err=True)
        raise typer.Exit(1)

    target = ssh_target_for_vm(vm, config)
    logger = SSHLogger(vm.name, operation) if operation else None
    run_command = partial(run, target, logger=logger)
    return ws, vm, run_command


def _require_session(db: Database, name: str) -> SessionRow:
    session = db.get_session(name)
    if session is None:
        typer.echo(f"Error: session '{name}' not found", err=True)
        raise typer.Exit(1)
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
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions)
    target = ssh_target_for_vm(vm, config)
    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", config_text, logger=logger)


def _resolve_template(config: Config, template_name: str | None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.sessions.templates import resolve_template

    try:
        return resolve_template(config, template_name)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None


def _substitute_template_vars(text: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders in a string with their values."""

    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in _KNOWN_TEMPLATE_VARS:
            typer.echo(f"Error: unknown template variable '{{{{{name}}}}}'", err=True)
            raise typer.Exit(1)
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
            typer.echo(f"Error: invalid env var name {key!r} in template '{template.name}'", err=True)
            raise typer.Exit(1)
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
    alive = _session_exists_any_server(
        session.name, run_command=run_command, socket_path=sock,
    )
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
        deploy_restricted_config,
    )

    validate_name(name)
    ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation="session-create")

    if db.get_session(name) is not None:
        typer.echo(f"Error: session '{name}' already exists", err=True)
        raise typer.Exit(1)

    # Resolve mode and linux user
    resolved_agent_name: str | None = None
    if agent_name is not None:
        mode = SessionMode.AGENT
        agent = db.get_agent(agent_name)
        if agent is None:
            typer.echo(f"Error: agent '{agent_name}' not found", err=True)
            raise typer.Exit(1)
        if agent.vm_name != vm.name:
            typer.echo(
                f"Error: agent '{agent_name}' is on VM '{agent.vm_name}', "
                f"but workspace '{workspace_name}' is on VM '{vm.name}'",
                err=True,
            )
            raise typer.Exit(1)
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
            is_admin=(mode == SessionMode.ADMIN),
        )
    except Exception:
        db.delete_session(name)
        raise

    # Persist socket path
    if sock:
        db.update_session_socket_path(name, sock)

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    typer.echo(f"Session '{name}' started ({mode_label}, template: {template.name})")

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
    _ws, _vm, run_command = _prepare_vm(db, config, session.workspace_name, operation="session-stop")

    if session.status == SessionStatus.STOPPED.value:
        typer.echo(f"Session '{name}' is already stopped")
        return

    sock = _effective_socket_path(db, session)

    # Send C-c to the running process first (try both socket and default server)
    send_keys(name, "C-c", run_command=run_command, socket_path=sock)
    if sock:
        send_keys(name, "C-c", run_command=run_command)

    # Wait for graceful exit
    time.sleep(_STOP_GRACE_SECONDS)

    # Kill if still alive (checks both servers for migration compatibility)
    if _session_exists_any_server(name, run_command=run_command, socket_path=sock, warn_legacy=False):
        _kill_session_any_server(name, run_command=run_command, socket_path=sock)

    db.update_session_status(name, SessionStatus.STOPPED)
    typer.echo(f"Session '{name}' stopped")


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
        deploy_restricted_config,
    )

    session = _require_session(db, name)
    ws, vm, run_command = _prepare_vm(db, config, session.workspace_name, operation="session-restart")
    sock = _effective_socket_path(db, session)

    if _session_exists_any_server(name, run_command=run_command, socket_path=sock, warn_legacy=False):
        if not force:
            typer.echo(
                f"Error: session '{name}' is still running. Stop it first, or use --force.",
                err=True,
            )
            raise typer.Exit(1)
        _kill_session_any_server(name, run_command=run_command, socket_path=sock)

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
        is_admin=is_admin,
    )

    # Update socket path if it changed
    if new_sock != sock:
        db.update_session_socket_path(name, new_sock)

    db.update_session_status(name, SessionStatus.RUNNING)
    typer.echo(f"Session '{name}' restarted")

    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=new_sock)


def delete_session(
    db: Database,
    config: Config,
    *,
    name: str,
    yes: bool = False,
) -> None:
    """Stop and delete a session."""
    session = _require_session(db, name)
    ws, vm, run_command = _prepare_vm(db, config, session.workspace_name, operation="session-delete")
    sock = _effective_socket_path(db, session)

    if not yes and _session_exists_any_server(name, run_command=run_command, socket_path=sock):
        typer.confirm(f"Session '{name}' is still running. Delete anyway?", abort=True)

    _kill_session_any_server(name, run_command=run_command, socket_path=sock)

    # Remove stale socket file if the tmux server has exited.
    # If the kill failed, warn and leave the socket for debugging.
    if sock:
        import shlex

        from agentworks.sessions.tmux import session_exists

        if session_exists(name, run_command=run_command, socket_path=sock):
            typer.echo(
                f"  Warning: tmux session '{name}' is still running after kill. "
                f"Socket preserved at {sock}",
                err=True,
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
    typer.echo(f"Session '{name}' deleted")

    # If this session created its workspace, offer to delete it
    if session.created_workspace:
        remaining = db.list_sessions(workspace_name=session.workspace_name)
        if remaining:
            typer.echo(
                f"  Workspace '{session.workspace_name}' was created with this session but has "
                f"{len(remaining)} other session(s), not offering to delete."
            )
        elif not yes:
            if typer.confirm(
                f"  Workspace '{session.workspace_name}' was created with this session and has no other sessions. Delete it?",
            ):
                from agentworks.workspaces.manager import delete_workspace

                delete_workspace(db, config, session.workspace_name, yes=True)
        else:
            from agentworks.workspaces.manager import delete_workspace

            typer.echo(f"  Deleting workspace '{session.workspace_name}' (created with this session)...")
            delete_workspace(db, config, session.workspace_name, yes=True)


def describe_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Show session details."""
    session = _require_session(db, name)
    ws, vm, run_command = _prepare_vm(db, config, session.workspace_name, operation=None)

    # Reconcile status with tmux
    status = _reconcile_status(session, run_command=run_command, db=db)
    if status != session.status:
        session = _require_session(db, name)

    mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"

    typer.echo(f"Name:       {session.name}")
    typer.echo(f"Workspace:  {session.workspace_name}")
    typer.echo(f"VM:         {vm.name}")
    typer.echo(f"Template:   {session.template}")
    typer.echo(f"Mode:       {mode_label}")
    typer.echo(f"Status:     {session.status}")
    typer.echo(f"Created:    {session.created_at}")
    typer.echo(f"Updated:    {session.updated_at}")


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
        typer.echo("No sessions found.")
        return

    # Group sessions by workspace to batch SSH connections
    by_workspace: dict[str, list[SessionRow]] = {}
    for session in sessions:
        by_workspace.setdefault(session.workspace_name, []).append(session)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for ws_name, ws_sessions in sorted(by_workspace.items()):
        ws = db.get_workspace(ws_name)
        vm_name = ws.vm_name or "-" if ws else "-"

        if no_status or ws is None or ws.type != "vm":
            for session in ws_sessions:
                mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
                rows.append((session.name, ws_name, vm_name, session.template, mode_label, session.status))
            continue

        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None or vm.tailscale_host is None:
            for session in ws_sessions:
                mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
                rows.append((session.name, ws_name, vm_name, session.template, mode_label, session.status))
            continue

        from agentworks.ssh import run

        target = ssh_target_for_vm(vm, config)
        run_command = partial(run, target)

        for session in ws_sessions:
            status = _reconcile_status(
                session,
                run_command=run_command,
                db=db,
            )
            mode_label = f"agent ({session.agent_name})" if session.agent_name else "admin"
            rows.append((session.name, ws_name, vm_name, session.template, mode_label, status))

    if not rows:
        typer.echo("No sessions found.")
        return

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    ws_w = max(len("WORKSPACE"), max(len(r[1]) for r in rows))
    vm_w = max(len("VM"), max(len(r[2]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[3]) for r in rows))
    mode_w = max(len("MODE"), max(len(r[4]) for r in rows))

    header = (
        f"{'NAME':<{name_w}}  {'WORKSPACE':<{ws_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  {'MODE':<{mode_w}}  STATUS"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for sname, ws_name, vm_col, tpl, mode, status in rows:
        typer.echo(
            f"{sname:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
        )


def attach_session(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Attach to a session's tmux session (interactive)."""
    from agentworks.ssh import interactive
    from agentworks.sessions.tmux import session_exists, tmux_cmd

    session = _require_session(db, name)
    _ws, vm, run_command = _prepare_vm(db, config, session.workspace_name, operation="session-attach")
    sock = _effective_socket_path(db, session)

    if not _session_exists_any_server(name, run_command=run_command, socket_path=sock):
        typer.echo(f"Error: session '{name}' is not running", err=True)
        raise typer.Exit(1)

    # Determine which server has the session. Prefer the socket; fall back
    # to the default server for legacy sessions.
    q_session = shlex.quote(name)
    target = ssh_target_for_vm(vm, config)
    if sock and session_exists(name, run_command=run_command, socket_path=sock):
        sys.exit(interactive(target, tmux_cmd(f"attach -t {q_session}", sock)))
    else:
        sys.exit(interactive(target, tmux_cmd(f"attach -t {q_session}")))


def session_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    lines: int | None = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.sessions.tmux import capture_output, session_exists

    session = _require_session(db, name)
    _ws, _vm, run_command = _prepare_vm(db, config, session.workspace_name, operation="session-logs")
    sock = _effective_socket_path(db, session)

    # For legacy sessions, fall back to the default server
    if sock and not session_exists(name, run_command=run_command, socket_path=sock):
        sock = None

    output = capture_output(
        name,
        run_command=run_command,
        lines=lines or config.session.history_limit,
        socket_path=sock,
    )
    typer.echo(output, nl=False)
