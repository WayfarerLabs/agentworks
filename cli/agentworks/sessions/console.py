"""VM console management.

The console is a VM-level tmux session that provides a unified view of all
tasks running on the VM. It has full tmux controls (the operator can split
panes, create windows, rearrange layout). Each task appears as a window
that attaches to the task's locked-down tmux session.
"""

from __future__ import annotations

import shlex
import sys
from functools import partial
from typing import TYPE_CHECKING

import typer

from agentworks.db import TaskStatus
from agentworks.sessions.tmux import derive_session_name, tmux_cmd

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, TaskRow, VMRow
    from agentworks.sessions.tmux import RunCommand

CONSOLE_SESSION_NAME = "vm-console"


def console_exists(*, run_command: RunCommand) -> bool:
    """Check if the console tmux session exists on the VM."""
    result = run_command(f"tmux has-session -t {CONSOLE_SESSION_NAME} 2>/dev/null", check=False)
    return getattr(result, "ok", False)


def create_console(
    running_tasks: list[TaskRow],
    *,
    run_command: RunCommand,
    admin_username: str,
    socket_paths: dict[str, str | None] | None = None,
    recreate: bool = False,
) -> None:
    """Create the VM console session with one window per running task.

    When *recreate* is True, kills any existing console session first.
    """
    if recreate:
        run_command(f"tmux kill-session -t {CONSOLE_SESSION_NAME}", check=False)

    # Create the session with a login shell as the initial window
    run_command(
        f"tmux new-session -d -s {CONSOLE_SESSION_NAME} "
        f"-n admin-shell "
        f"{shlex.quote('exec sudo su --login ' + shlex.quote(admin_username))}"
    )

    # Keep windows open when attached task command exits
    run_command(f"tmux set -t {CONSOLE_SESSION_NAME} remain-on-exit on", check=False)

    # Add a window for each running task
    paths = socket_paths or {}
    typer.echo(f"Adding {len(running_tasks)} task(s) to console...")
    for task in running_tasks:
        key = derive_session_name(task.workspace_name, task.name)
        _add_task_window(
            task.workspace_name, task.name,
            run_command=run_command,
            socket_path=paths.get(key),
        )


def _add_task_window(
    workspace_name: str,
    task_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> None:
    """Add a single task window to the console."""
    session_name = derive_session_name(workspace_name, task_name)
    q_session = shlex.quote(session_name)
    # Unset TMUX to allow nesting (console -> task session), then loop
    # re-attach while the task session is alive.
    has_cmd = tmux_cmd(f"has-session -t {q_session}", socket_path)
    attach_cmd = tmux_cmd(f"attach -t {q_session}", socket_path)
    wrapper = (
        f"unset TMUX; "
        f"while {has_cmd} 2>/dev/null; do "
        f"{attach_cmd}; "
        f"sleep 0.5; "
        f"done; "
        f"echo 'Task session {q_session} has ended. Press enter to close.'; "
        f"read"
    )
    result = run_command(
        f"tmux new-window -t {CONSOLE_SESSION_NAME} -n {q_session} {shlex.quote(wrapper)}",
        check=False,
    )
    ok = getattr(result, "ok", True)
    stderr = getattr(result, "stderr", "")
    if not ok:
        typer.echo(f"  Warning: failed to add window for '{session_name}': {stderr}", err=True)


def add_task_to_console(
    task_name: str,
    workspace_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> None:
    """Add a task window to an existing console (best-effort)."""
    if not console_exists(run_command=run_command):
        return

    _add_task_window(workspace_name, task_name, run_command=run_command, socket_path=socket_path)




def attach_console(
    db: Database,
    config: Config,
    *,
    vm_name: str,
    recreate: bool = False,
    allow_nesting: bool = False,
) -> None:
    """Attach to (or create) the VM console."""
    import os

    if os.environ.get("TMUX") and not allow_nesting:
        typer.echo(
            "Error: already inside a tmux session.\n"
            "Nesting is not recommended (prefix key conflicts,\n"
            "confusing detach behavior).\n"
            "Pass --allow-nesting to override.",
            err=True,
        )
        raise typer.Exit(1)

    vm = db.get_vm(vm_name)
    if vm is None:
        typer.echo(f"Error: VM '{vm_name}' not found", err=True)
        raise typer.Exit(1)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{vm_name}' has no Tailscale address", err=True)
        raise typer.Exit(1)

    from agentworks.ssh import interactive, run, ssh_target_for_vm

    target = ssh_target_for_vm(vm, config)
    run_command = partial(run, target)

    # Get running tasks for this VM
    running_tasks = _get_running_tasks_for_vm(db, vm)

    # Build socket path map for agent-mode tasks
    from agentworks.sessions.tmux import build_socket_paths

    def _agent_lookup(agent_name: str) -> str | None:
        agent = db.get_agent(agent_name)
        return agent.linux_user if agent else None

    paths = build_socket_paths(running_tasks, _agent_lookup)

    if recreate or not console_exists(run_command=run_command):
        create_console(
            running_tasks,
            run_command=run_command,
            admin_username=vm.admin_username,
            socket_paths=paths,
            recreate=recreate,
        )

    sys.exit(interactive(target, f"tmux attach -t {CONSOLE_SESSION_NAME}"))


def _get_running_tasks_for_vm(db: Database, vm: VMRow) -> list[TaskRow]:
    """Get all running tasks across all workspaces on a VM."""
    workspaces = db.list_workspaces(vm_name=vm.name)
    tasks: list[TaskRow] = []
    for ws in workspaces:
        ws_tasks = db.list_tasks(workspace_name=ws.name)
        tasks.extend(t for t in ws_tasks if t.status == TaskStatus.RUNNING.value)
    return tasks
