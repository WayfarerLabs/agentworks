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
    from agentworks.config import Config, TaskTemplate
    from agentworks.db import Database, TaskRow, VMRow, WorkspaceRow
    from agentworks.tasks.tmux import RunCommand


# -- Helpers ---------------------------------------------------------------

# Grace period (seconds) to wait after sending C-c before killing a session
_STOP_GRACE_SECONDS = 5


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
    db: Database, config: Config, workspace_name: str
) -> tuple[WorkspaceRow, VMRow, RunCommand]:
    """Validate workspace/VM, ensure running, and return (ws, vm, run_command)."""
    from agentworks.ssh import run

    ws = _require_workspace(db, workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{vm.name}' has no Tailscale address", err=True)
        raise typer.Exit(1)

    target = ssh_target_for_vm(vm, config)
    run_command = partial(run, target)
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
) -> None:
    """Regenerate the workspace tmuxinator config from current task state."""
    from agentworks.ssh import write_file
    from agentworks.workspaces.tmuxinator import generate_config

    tasks = db.list_tasks(workspace_name=ws.name)
    config_text = generate_config(ws.name, ws.workspace_path, tasks=tasks)
    target = ssh_target_for_vm(vm, config)
    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", config_text)


def _resolve_template(config: Config, template_name: str | None) -> TaskTemplate:
    """Resolve a task template by name.

    Selection order:
    1. Explicit template_name
    2. "default" template (built-in or user-defined)
    """
    if template_name is not None:
        tpl = config.task_templates.get(template_name)
        if tpl is None:
            typer.echo(f"Error: unknown task template '{template_name}'", err=True)
            raise typer.Exit(1)
        return tpl

    tpl = config.task_templates.get("default")
    if tpl is None:
        typer.echo("Error: no 'default' task template found", err=True)
        raise typer.Exit(1)
    return tpl


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
    template: TaskTemplate,
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

    raw_command = (template.restart_command if restart and template.restart_command else template.command)
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
    from agentworks.tasks.tmux import session_exists

    alive = session_exists(workspace_name, task.name, run_command=run_command)
    if task.status == TaskStatus.RUNNING.value and not alive:
        db.update_task_status(workspace_name, task.name, TaskStatus.STOPPED)
        return TaskStatus.STOPPED.value
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
) -> None:
    """Create and start a task."""
    from agentworks.config import validate_name
    from agentworks.tasks.tmux import (
        create_task_session,
        deploy_restricted_config,
    )

    validate_name(name)
    ws, vm, run_command = _prepare_vm(db, config, workspace_name)

    if db.get_task(workspace_name, name) is not None:
        typer.echo(f"Error: task '{name}' already exists in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    # Resolve mode and linux user
    if agent_name is not None:
        mode = TaskMode.AGENT
        agent = db.get_agent(workspace_name, agent_name)
        if agent is None:
            typer.echo(
                f"Error: agent '{agent_name}' not found in workspace '{workspace_name}'",
                err=True,
            )
            raise typer.Exit(1)
        linux_user = agent.linux_user
    else:
        mode = TaskMode.ADMIN
        linux_user = vm.admin_username

    template = _resolve_template(config, template_name)

    # Insert DB record first to avoid orphaned tmux sessions on crash
    db.insert_task(name, workspace_name, template.name, mode, linux_user)

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

    typer.echo(f"Task '{name}' started ({mode.value} mode, template: {template.name})")

    # Update tmuxinator config and add to console if it exists
    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.tasks.console import add_task_to_console

    add_task_to_console(name, workspace_name, run_command=run_command)


def stop_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Stop a running task. Sends C-c first, then kills after a grace period."""
    import time

    from agentworks.tasks.tmux import kill_task_session, session_exists

    _ws, _vm, run_command = _prepare_vm(db, config, workspace_name)
    task = _require_task(db, workspace_name, name)

    if task.status == TaskStatus.STOPPED.value:
        typer.echo(f"Task '{name}' is already stopped")
        return

    # Send C-c to the running process first
    session = shlex.quote(
        f"{workspace_name}--{name}"
    )
    run_command(f"tmux send-keys -t {session} C-c", check=False)

    # Wait for graceful exit
    time.sleep(_STOP_GRACE_SECONDS)

    # Kill if still alive
    if session_exists(workspace_name, name, run_command=run_command):
        kill_task_session(workspace_name, name, run_command=run_command)

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
        kill_task_session,
        session_exists,
    )

    ws, vm, run_command = _prepare_vm(db, config, workspace_name)
    task = _require_task(db, workspace_name, name)

    if session_exists(workspace_name, name, run_command=run_command):
        if not force:
            typer.echo(
                f"Error: task '{name}' is still running. "
                "Stop it first, or use --force.",
                err=True,
            )
            raise typer.Exit(1)
        kill_task_session(workspace_name, name, run_command=run_command)

    template = _resolve_template(config, task.template)
    deploy_restricted_config(run_command, history_limit=config.task.history_limit)

    # Use restart_command if available, otherwise fall back to command
    command = _build_task_command(
        template, task_name=name, workspace_name=workspace_name, restart=True,
    )
    is_admin = task.mode == TaskMode.ADMIN.value

    create_task_session(
        workspace_name,
        name,
        ws.workspace_path,
        command,
        task.linux_user,
        run_command=run_command,
        is_admin=is_admin,
    )

    db.update_task_status(workspace_name, name, TaskStatus.RUNNING)
    typer.echo(f"Task '{name}' restarted")

    _regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.tasks.console import add_task_to_console

    add_task_to_console(name, workspace_name, run_command=run_command)


def delete_task(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    yes: bool = False,
) -> None:
    """Stop and delete a task."""
    from agentworks.tasks.tmux import kill_task_session, session_exists

    ws, vm, run_command = _prepare_vm(db, config, workspace_name)
    _require_task(db, workspace_name, name)

    if not yes and session_exists(workspace_name, name, run_command=run_command):
        typer.confirm(f"Task '{name}' is still running. Delete anyway?", abort=True)

    kill_task_session(workspace_name, name, run_command=run_command)
    db.delete_task(workspace_name, name)

    _regenerate_tmuxinator(db, config, vm, ws)
    typer.echo(f"Task '{name}' deleted")


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
        f"{'NAME':<{name_w}}  {'WORKSPACE':<{ws_w}}  {'VM':<{vm_w}}  "
        f"{'TEMPLATE':<{tpl_w}}  {'MODE':<{mode_w}}  STATUS"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for task_name, ws_name, vm_col, tpl, mode, status in rows:
        typer.echo(
            f"{task_name:<{name_w}}  {ws_name:<{ws_w}}  {vm_col:<{vm_w}}  "
            f"{tpl:<{tpl_w}}  {mode:<{mode_w}}  {status}"
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
    from agentworks.tasks.tmux import derive_session_name, session_exists

    _ws, vm, run_command = _prepare_vm(db, config, workspace_name)
    _require_task(db, workspace_name, name)

    if not session_exists(workspace_name, name, run_command=run_command):
        typer.echo(f"Error: task '{name}' is not running", err=True)
        raise typer.Exit(1)

    session = shlex.quote(derive_session_name(workspace_name, name))
    target = ssh_target_for_vm(vm, config)
    sys.exit(interactive(target, f"tmux attach -t {session}"))


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

    _ws, _vm, run_command = _prepare_vm(db, config, workspace_name)
    _require_task(db, workspace_name, name)

    output = capture_output(
        workspace_name,
        name,
        run_command=run_command,
        lines=lines or config.task.history_limit,
    )
    typer.echo(output, nl=False)
