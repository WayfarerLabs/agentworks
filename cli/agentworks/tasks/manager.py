"""Task lifecycle orchestration."""

from __future__ import annotations

import re
import shlex
import sys
from functools import partial
from typing import TYPE_CHECKING

import typer

from agentworks.db import TaskMode, TaskStatus
from agentworks.ssh import ssh_target_for_vm

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Template variable substitution -- uses {{var}} syntax consistent with nerftools.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"task_name", "workspace_name"}

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, TaskRow, VMRow, WorkspaceRow
    from agentworks.ssh import SSHLogger
    from agentworks.tasks.templates import ResolvedTaskTemplate
    from agentworks.tasks.tmux import RunCommand


# -- Helpers ---------------------------------------------------------------

# Grace period (seconds) to wait after sending C-c before killing a session
_STOP_GRACE_SECONDS = 5


def _resolve_task_linux_user(db: Database, task: TaskRow, vm: VMRow) -> str:
    """Resolve the Linux user for a task.

    Agent-mode tasks look up the agent by name. Admin-mode tasks use the VM admin.
    """
    if task.agent_name:
        agent = db.get_agent(task.agent_name)
        if agent is None:
            typer.echo(f"Error: agent '{task.agent_name}' not found (referenced by task '{task.name}')", err=True)
            raise typer.Exit(1)
        return agent.linux_user
    return vm.admin_username


def _agent_linux_user_lookup(db: Database):
    """Return a callable that resolves agent name to linux_user via DB."""

    def _lookup(agent_name: str) -> str | None:
        agent = db.get_agent(agent_name)
        return agent.linux_user if agent else None

    return _lookup


def _socket_path_for_task(db: Database, task: TaskRow) -> str | None:
    """Return the agent socket path for an agent-mode task, or None for admin."""
    if not task.agent_name:
        return None
    from agentworks.tasks.tmux import agent_socket_path

    linux_user = _agent_linux_user_lookup(db)(task.agent_name)
    if linux_user is None:
        return None
    return agent_socket_path(linux_user, task.workspace_name, task.name)


def _kill_task_any_server(
    workspace_name: str,
    task_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> None:
    """Kill a task session on both the agent socket and the default server.

    Handles migration from the old model (agent sessions on the admin's
    default tmux server) to the new model (per-agent sockets). Safe to call
    even if the session exists on neither or both.
    """
    from agentworks.tasks.tmux import kill_task_session

    if socket_path:
        kill_task_session(workspace_name, task_name, run_command=run_command, socket_path=socket_path)
    # Always try the default server too, to clean up legacy sessions
    kill_task_session(workspace_name, task_name, run_command=run_command)


def _session_exists_any_server(
    workspace_name: str,
    task_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> bool:
    """Check if a task session exists on either the agent socket or the default server."""
    from agentworks.tasks.tmux import session_exists

    if socket_path and session_exists(workspace_name, task_name, run_command=run_command, socket_path=socket_path):
        return True
    on_default = session_exists(workspace_name, task_name, run_command=run_command)
    if on_default and socket_path:
        typer.echo(
            f"  Note: agent task '{task_name}' is running on the default tmux server "
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
        typer.echo("Error: tasks are only supported on VM workspaces", err=True)
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


def _require_task(db: Database, workspace_name: str, name: str) -> TaskRow:
    task = db.get_task(workspace_name, name)
    if task is None:
        typer.echo(f"Error: task '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)
    return task


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Regenerate the workspace tmuxinator config from current task state."""
    from agentworks.ssh import write_file
    from agentworks.tasks.tmux import build_socket_paths
    from agentworks.workspaces.tmuxinator import generate_config

    tasks = db.list_tasks(workspace_name=ws.name)
    paths = build_socket_paths(tasks, _agent_linux_user_lookup(db))
    config_text = generate_config(ws.name, ws.workspace_path, tasks=tasks, socket_paths=paths)
    target = ssh_target_for_vm(vm, config)
    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", config_text, logger=logger)


def _resolve_template(config: Config, template_name: str | None) -> ResolvedTaskTemplate:
    """Resolve a task template by name, applying inheritance."""
    from agentworks.tasks.templates import resolve_template

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


def _build_task_command(
    template: ResolvedTaskTemplate,
    *,
    task_name: str,
    workspace_name: str,
    restart: bool = False,
) -> str:
    """Build the shell command string for a task from its template.

    Returns an empty string if the template has no command (login shell only).
    Uses restart_command (if defined) when restart=True.
    """
    variables = {
        "task_name": task_name,
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
    task: TaskRow,
    *,
    run_command: RunCommand,
    workspace_name: str,
    db: Database,
) -> str:
    """Check tmux session and reconcile task status in the DB."""
    sock = _socket_path_for_task(db, task)
    alive = _session_exists_any_server(workspace_name, task.name, run_command=run_command, socket_path=sock)
    if task.status == TaskStatus.RUNNING.value and not alive:
        db.update_task_status(workspace_name, task.name, TaskStatus.STOPPED)
        return TaskStatus.STOPPED.value
    if task.status == TaskStatus.STOPPED.value and alive:
        db.update_task_status(workspace_name, task.name, TaskStatus.RUNNING)
        return TaskStatus.RUNNING.value
    return task.status


# -- Public API ------------------------------------------------------------


def create_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    template_name: str | None = None,
    agent_name: str | None = None,
    created_workspace: bool = False,
) -> None:
    """Create and start a task."""
    from agentworks.config import validate_name
    from agentworks.tasks.tmux import (
        create_task_session,
        deploy_restricted_config,
    )

    validate_name(name)
    ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-create")

    if db.get_task(workspace_name, name) is not None:
        typer.echo(f"Error: task '{name}' already exists in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    # Resolve mode and linux user
    resolved_agent_name: str | None = None
    if agent_name is not None:
        mode = TaskMode.AGENT
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
        db.insert_agent_grant(agent_name, workspace_name, "implicit", task_name=name)
    else:
        mode = TaskMode.ADMIN
        linux_user = vm.admin_username

    template = _resolve_template(config, template_name)

    # Insert DB record first to avoid orphaned tmux sessions on crash
    db.insert_task(
        name,
        workspace_name,
        template.name,
        mode,
        agent_name=resolved_agent_name,
        created_workspace=created_workspace,
    )

    deploy_restricted_config(run_command, history_limit=config.task.history_limit)
    command = _build_task_command(template, task_name=name, workspace_name=workspace_name)

    try:
        create_task_session(
            workspace_name,
            name,
            ws.workspace_path,
            command,
            linux_user,
            run_command=run_command,
            is_admin=(mode == TaskMode.ADMIN),
        )
    except Exception:
        db.delete_task(workspace_name, name)
        raise

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    typer.echo(f"Task '{name}' started ({mode_label}, template: {template.name})")

    # Update tmuxinator config and add to console if it exists
    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.tasks.console import add_task_to_console

    sock = None
    if mode == TaskMode.AGENT:
        from agentworks.tasks.tmux import agent_socket_path

        sock = agent_socket_path(linux_user, workspace_name, name)
    add_task_to_console(name, workspace_name, run_command=run_command, socket_path=sock)


def stop_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Stop a running task. Sends C-c first, then kills after a grace period."""
    import time

    from agentworks.tasks.tmux import send_keys

    _ws, _vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-stop")
    task = _require_task(db, workspace_name, name)

    if task.status == TaskStatus.STOPPED.value:
        typer.echo(f"Task '{name}' is already stopped")
        return

    sock = _socket_path_for_task(db, task)

    # Send C-c to the running process first (try both socket and default server)
    send_keys(workspace_name, name, "C-c", run_command=run_command, socket_path=sock)
    if sock:
        send_keys(workspace_name, name, "C-c", run_command=run_command)

    # Wait for graceful exit
    time.sleep(_STOP_GRACE_SECONDS)

    # Kill if still alive (checks both servers for migration compatibility)
    if _session_exists_any_server(workspace_name, name, run_command=run_command, socket_path=sock):
        _kill_task_any_server(workspace_name, name, run_command=run_command, socket_path=sock)

    db.update_task_status(workspace_name, name, TaskStatus.STOPPED)
    typer.echo(f"Task '{name}' stopped")


def restart_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    force: bool = False,
) -> None:
    """Restart a task. Errors if running unless --force is passed."""
    from agentworks.tasks.tmux import (
        create_task_session,
        deploy_restricted_config,
    )

    ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-restart")
    task = _require_task(db, workspace_name, name)
    sock = _socket_path_for_task(db, task)

    if _session_exists_any_server(workspace_name, name, run_command=run_command, socket_path=sock):
        if not force:
            typer.echo(
                f"Error: task '{name}' is still running. Stop it first, or use --force.",
                err=True,
            )
            raise typer.Exit(1)
        _kill_task_any_server(workspace_name, name, run_command=run_command, socket_path=sock)

    template = _resolve_template(config, task.template)
    deploy_restricted_config(run_command, history_limit=config.task.history_limit)

    # Use restart_command if available, otherwise fall back to command
    command = _build_task_command(
        template,
        task_name=name,
        workspace_name=workspace_name,
        restart=True,
    )
    is_admin = task.mode == TaskMode.ADMIN.value
    linux_user = _resolve_task_linux_user(db, task, vm)

    create_task_session(
        workspace_name,
        name,
        ws.workspace_path,
        command,
        linux_user,
        run_command=run_command,
        is_admin=is_admin,
    )

    db.update_task_status(workspace_name, name, TaskStatus.RUNNING)
    typer.echo(f"Task '{name}' restarted")

    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.tasks.console import add_task_to_console

    add_task_to_console(name, workspace_name, run_command=run_command, socket_path=sock)


def delete_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    yes: bool = False,
) -> None:
    """Stop and delete a task."""
    ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-delete")
    task = _require_task(db, workspace_name, name)
    sock = _socket_path_for_task(db, task)

    if not yes and _session_exists_any_server(workspace_name, name, run_command=run_command, socket_path=sock):
        typer.confirm(f"Task '{name}' is still running. Delete anyway?", abort=True)

    _kill_task_any_server(workspace_name, name, run_command=run_command, socket_path=sock)
    db.delete_task(workspace_name, name)

    # Clean up implicit grant for this task
    if task and task.agent_name:
        db.delete_agent_grant(task.agent_name, workspace_name, "implicit", task_name=name)
        # If no grants remain, remove from workspace group
        if not db.has_any_grant(task.agent_name, workspace_name):
            from agentworks.agents.manager import _remove_from_workspace_group

            agent = db.get_agent(task.agent_name)
            if agent:
                _remove_from_workspace_group(vm, config, agent.linux_user, workspace_name)

    _regenerate_tmuxinator(db, config, vm, ws)
    typer.echo(f"Task '{name}' deleted")

    # If this task created its workspace, offer to delete it
    if task and task.created_workspace:
        remaining = db.list_tasks(workspace_name=workspace_name)
        if remaining:
            typer.echo(
                f"  Workspace '{workspace_name}' was created with this task but has "
                f"{len(remaining)} other task(s), not offering to delete."
            )
        elif not yes:
            if typer.confirm(
                f"  Workspace '{workspace_name}' was created with this task and has no other tasks. Delete it?",
            ):
                from agentworks.workspaces.manager import delete_workspace

                delete_workspace(db, config, workspace_name, yes=True)
        else:
            from agentworks.workspaces.manager import delete_workspace

            typer.echo(f"  Deleting workspace '{workspace_name}' (created with this task)...")
            delete_workspace(db, config, workspace_name, yes=True)


def describe_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Show task details."""
    task = _require_task(db, workspace_name, name)
    ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation=None)

    # Reconcile status with tmux
    from agentworks.tasks.tmux import session_exists

    live = session_exists(workspace_name, name, run_command=run_command)
    if live and task.status != TaskStatus.RUNNING.value:
        db.update_task_status(workspace_name, name, TaskStatus.RUNNING)
        task = _require_task(db, workspace_name, name)
    elif not live and task.status == TaskStatus.RUNNING.value:
        db.update_task_status(workspace_name, name, TaskStatus.STOPPED)
        task = _require_task(db, workspace_name, name)

    mode_label = f"agent: {task.agent_name}" if task.agent_name else "admin"

    typer.echo(f"Name:       {task.name}")
    typer.echo(f"Workspace:  {task.workspace_name}")
    typer.echo(f"VM:         {vm.name}")
    typer.echo(f"Template:   {task.template}")
    typer.echo(f"Mode:       {mode_label}")
    typer.echo(f"Status:     {task.status}")
    typer.echo(f"Created:    {task.created_at}")
    typer.echo(f"Updated:    {task.updated_at}")


def list_tasks(
    db: Database,
    config: Config,
    *,
    workspace_name: str | None = None,
) -> None:
    """List tasks, reconciling status with tmux."""
    tasks = db.list_tasks(workspace_name=workspace_name)
    if not tasks:
        typer.echo("No tasks found.")
        return

    # Group tasks by workspace to batch SSH connections
    by_workspace: dict[str, list[TaskRow]] = {}
    for task in tasks:
        by_workspace.setdefault(task.workspace_name, []).append(task)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for ws_name, ws_tasks in sorted(by_workspace.items()):
        ws = db.get_workspace(ws_name)
        vm_name = ws.vm_name or "-" if ws else "-"

        if ws is None or ws.type != "vm":
            for task in ws_tasks:
                rows.append((task.name, ws_name, vm_name, task.template, task.mode, task.status))
            continue

        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None or vm.tailscale_host is None:
            for task in ws_tasks:
                rows.append((task.name, ws_name, vm_name, task.template, task.mode, task.status))
            continue

        from agentworks.ssh import run

        target = ssh_target_for_vm(vm, config)
        run_command = partial(run, target)

        for task in ws_tasks:
            status = _reconcile_status(
                task,
                run_command=run_command,
                workspace_name=ws_name,
                db=db,
            )
            rows.append((task.name, ws_name, vm_name, task.template, task.mode, status))

    if not rows:
        typer.echo("No tasks found.")
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
    for task_name, ws_name, vm_col, tpl, mode, status in rows:
        typer.echo(
            f"{task_name:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  {tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
        )


def attach_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Attach to a task's tmux session (interactive)."""
    from agentworks.ssh import interactive
    from agentworks.tasks.tmux import derive_session_name, session_exists, tmux_cmd

    _ws, vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-attach")
    task = _require_task(db, workspace_name, name)
    sock = _socket_path_for_task(db, task)

    if not session_exists(workspace_name, name, run_command=run_command, socket_path=sock):
        typer.echo(f"Error: task '{name}' is not running", err=True)
        raise typer.Exit(1)

    session = shlex.quote(derive_session_name(workspace_name, name))
    target = ssh_target_for_vm(vm, config)
    sys.exit(interactive(target, tmux_cmd(f"attach -t {session}", sock)))


def task_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    lines: int | None = None,
) -> None:
    """Dump the scrollback buffer for a task."""
    from agentworks.tasks.tmux import capture_output

    _ws, _vm, run_command = _prepare_vm(db, config, workspace_name, operation="task-logs")
    task = _require_task(db, workspace_name, name)
    sock = _socket_path_for_task(db, task)

    output = capture_output(
        workspace_name,
        name,
        run_command=run_command,
        lines=lines or config.task.history_limit,
        socket_path=sock,
    )
    typer.echo(output, nl=False)
