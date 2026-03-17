"""Agent lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import validate_name
from agentworks.ssh import ssh_target_for_vm

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow

AGENT_SEPARATOR = "--"
AGENT_SHELL = "/bin/bash"


def derive_linux_user(workspace_name: str, agent_name: str) -> str:
    """Derive the Linux username for an agent: <workspace>--<agent>."""
    return f"{workspace_name}{AGENT_SEPARATOR}{agent_name}"


def create_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Create an agent on a workspace."""
    validate_name(name)

    ws = _require_workspace(db, workspace_name)

    if db.get_agent(workspace_name, name) is not None:
        typer.echo(f"Error: agent '{name}' already exists in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    linux_user = derive_linux_user(workspace_name, name)

    if ws.type == "local":
        typer.echo("Error: agents are not supported on local workspaces", err=True)
        raise typer.Exit(1)

    if ws.type == "vm":
        vm = _require_vm_for_workspace(db, ws)
        try:
            _create_agent_on_vm(vm, config, linux_user, workspace_name)
        except Exception as e:
            typer.echo(f"Error creating agent: {e}", err=True)
            typer.echo(f"  Cleaning up user '{linux_user}'...", err=True)
            _delete_agent_on_vm(vm, config, linux_user)
            raise typer.Exit(1) from None

    agent = db.insert_agent(name, workspace_name, linux_user)

    # Regenerate tmuxinator config and add live window
    if ws.type == "vm":
        _regenerate_tmuxinator(db, config, vm, ws)
        _add_live_window(config, vm, ws.name, ws.workspace_path, agent)

    typer.echo(f"Agent '{name}' created (user: {agent.linux_user})")


def delete_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Delete an agent from a workspace."""
    ws = _require_workspace(db, workspace_name)
    agent = db.get_agent(workspace_name, name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    if ws.type == "vm":
        vm = _require_vm_for_workspace(db, ws)
        _remove_live_window(config, vm, ws.name, name)
        _delete_agent_on_vm(vm, config, agent.linux_user)
        db.delete_agent(workspace_name, name)
        # Regenerate after DB delete so the agent is excluded
        _regenerate_tmuxinator(db, config, vm, ws)
    else:
        db.delete_agent(workspace_name, name)

    typer.echo(f"Agent '{name}' deleted from workspace '{workspace_name}'")


def delete_agents_for_workspace(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
) -> None:
    """Delete all agents for a workspace (called during workspace deletion).

    Skips tmuxinator regeneration since the workspace itself is being deleted.
    """
    agents = db.delete_agents_for_workspace(ws.name)
    if not agents:
        return

    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None:
            for agent in agents:
                _delete_agent_on_vm(vm, config, agent.linux_user)

    names = ", ".join(a.name for a in agents)
    typer.echo(f"  Deleted {len(agents)} agent(s): {names}")


def list_agents(
    db: Database,
    *,
    workspace_name: str | None = None,
) -> None:
    """List agents."""
    agents = db.list_agents(workspace_name=workspace_name)
    if not agents:
        typer.echo("No agents found.")
        return

    typer.echo(f"{'NAME':<20} {'WORKSPACE':<20} {'LINUX USER':<30} {'CREATED'}")
    typer.echo("-" * 95)
    for agent in agents:
        typer.echo(
            f"{agent.name:<20} {agent.workspace_name:<20} "
            f"{agent.linux_user:<30} {agent.created_at}"
        )


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Open a shell as an agent user on a VM."""
    import os

    ws = _require_workspace(db, workspace_name)
    agent = db.get_agent(workspace_name, name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    if ws.type != "vm":
        typer.echo("Error: agents are only supported on VM workspaces", err=True)
        raise typer.Exit(1)

    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{vm.name}' has no Tailscale address", err=True)
        raise typer.Exit(1)

    # SSH as user account, then su to the agent user in the workspace directory
    ssh_cmd = ["ssh"]
    if config.user.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.user.ssh_private_key)])
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")
    ssh_cmd.extend(["-t", f"cd {ws.workspace_path} && exec su - {agent.linux_user}"])

    import subprocess
    import sys

    sys.exit(subprocess.call(ssh_cmd))


# -- Tmuxinator ------------------------------------------------------------


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
) -> None:
    """Regenerate the tmuxinator config from current DB state."""
    from agentworks.ssh import run as ssh_run
    from agentworks.workspaces.tmuxinator import generate_config

    agents = db.list_agents(workspace_name=ws.name)
    tmux_config = generate_config(ws.name, ws.workspace_path, agents=agents)
    target = ssh_target_for_vm(vm, config)

    from agentworks.ssh import write_file

    write_file(target, f"{ws.workspace_path}/.tmuxinator.yml", tmux_config)


def _add_live_window(
    config: Config,
    vm: VMRow,
    ws_name: str,
    workspace_path: str,
    agent: AgentRow,
) -> None:
    """Add a window to a running tmux session (best-effort)."""
    from functools import partial

    from agentworks.ssh import run as ssh_run
    from agentworks.workspaces.tmuxinator import add_window_to_session

    target = ssh_target_for_vm(vm, config)
    run_cmd = partial(ssh_run, target)

    add_window_to_session(
        ws_name, agent.name, agent.linux_user, workspace_path,
        run_command=run_cmd,
    )


def _remove_live_window(
    config: Config,
    vm: VMRow,
    ws_name: str,
    agent_name: str,
) -> None:
    """Remove a window from a running tmux session (best-effort)."""
    from functools import partial

    from agentworks.ssh import run as ssh_run
    from agentworks.workspaces.tmuxinator import remove_window_from_session

    target = ssh_target_for_vm(vm, config)
    run_cmd = partial(ssh_run, target)

    remove_window_from_session(ws_name, agent_name, run_command=run_cmd)


# -- VM operations ---------------------------------------------------------


def _create_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    workspace_name: str,
) -> None:
    """Create an agent Linux user on a VM and run user install commands."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)

    typer.echo(f"  Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"
    groups = f"ws-{workspace_name},aw-tools"
    run_as_root(target, f"useradd -m -s {AGENT_SHELL} {linux_user}")
    run_as_root(target, f"usermod -aG {groups} {linux_user}")

    # Write a minimal .bashrc with a clear agent prompt
    bashrc = f"export PS1='[agent:{linux_user}] \\w\\$ '"
    run_as_root(target, f"printf '%s\\n' '{bashrc}' > {home}/.bashrc")
    run_as_root(target, f"chown {linux_user}:{linux_user} {home}/.bashrc")

    # Run user install commands for this agent
    _run_agent_install_commands(vm, config, linux_user, home)


def _delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
) -> None:
    """Remove an agent Linux user from a VM."""
    from agentworks.ssh import SSHError, run_as_root

    target = ssh_target_for_vm(vm, config)

    try:
        # Kill any running processes for the user
        run_as_root(target, f"pkill -u {linux_user}", check=False)
        # Remove the user and their home directory
        run_as_root(target, f"userdel -r {linux_user}")
    except SSHError as e:
        typer.echo(f"Warning: remote cleanup for '{linux_user}' failed: {e}", err=True)


def _run_agent_install_commands(
    vm: VMRow,
    config: Config,
    linux_user: str,
    home: str,
) -> None:
    """Run user install commands for an agent. Failures warn but do not abort."""
    command_names = config.agent.user_install_commands
    if not command_names:
        return

    import shlex

    from agentworks.catalog import load_catalog
    from agentworks.ssh import SSHError, run_as_root

    catalog = load_catalog(config)
    target = ssh_target_for_vm(vm, config)
    shell = config.agent.shell
    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = catalog.user_install_commands.get(name)
        if entry is None:
            typer.echo(f"  Warning: install command '{name}' not found in catalog", err=True)
            continue
        # Skip if already installed for this user
        test_cmd = _build_agent_test_command(entry, linux_user, home)
        if test_cmd:
            check = run_as_root(target, test_cmd, check=False)
            if check.returncode == 0:
                typer.echo(f"  Agent install command {i}/{total} ({name}): already installed, skipping")
                path_additions.extend(entry.path)
                continue

        truncated = entry.command[:60]
        typer.echo(f"  Agent install command {i}/{total} ({name}): {truncated}...")
        try:
            # Run as the agent user via su, in their login shell
            run_as_root(
                target,
                f"su - {shlex.quote(linux_user)} -c {shlex.quote(f'{shell} -lc {shlex.quote(entry.command)}')}",
                timeout=120,
            )
        except SSHError as e:
            typer.echo(f"  Warning: agent install command '{name}' failed: {e}", err=True)
        path_additions.extend(entry.path)

    # Write PATH additions for the agent
    if path_additions:
        typer.echo(f"  Adding {len(path_additions)} PATH entries for agent...")
        lines = ["# Managed by agentworks -- do not edit"]
        for p in path_additions:
            expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
            lines.append(f'export PATH="{expanded}:$PATH"')
        content = "\n".join(lines) + "\n"
        try:
            path_file = f"{home}/.agentworks-path.sh"
            run_as_root(
                target,
                f"printf '%s' {shlex.quote(content)} > {shlex.quote(path_file)}",
            )
            run_as_root(target, f"chown {shlex.quote(linux_user)} {shlex.quote(path_file)}")
            # Source from shell profiles
            source_line = f". {path_file}"
            rc_files = [f"{home}/.profile", f"{home}/.bashrc"]
            if shell == "zsh":
                rc_files.append(f"{home}/.zprofile")
            for rc in rc_files:
                run_as_root(
                    target,
                    f"grep -q agentworks-path.sh {shlex.quote(rc)} 2>/dev/null"
                    f" || printf '%s\\n' '{source_line}' >> {shlex.quote(rc)}",
                )
        except SSHError as e:
            typer.echo(f"  Warning: agent PATH configuration failed: {e}", err=True)


def _build_agent_test_command(
    entry: object, linux_user: str, home: str,
) -> str | None:
    """Build a test command that runs as the agent user."""
    import shlex as _shlex

    if getattr(entry, "test_exec", None):
        # Run command -v as the agent user via su
        inner = f"command -v {_shlex.quote(entry.test_exec)}"
        return (
            f"su - {_shlex.quote(linux_user)} -c {_shlex.quote(inner)}"
            " > /dev/null 2>&1"
        )
    if getattr(entry, "test_file", None):
        path = entry.test_file.replace("~", home, 1) if entry.test_file.startswith("~") else entry.test_file
        return f"test -f {_shlex.quote(path)}"
    if getattr(entry, "test_dir", None):
        path = entry.test_dir.replace("~", home, 1) if entry.test_dir.startswith("~") else entry.test_dir
        return f"test -d {_shlex.quote(path)}"
    return None


# -- Helpers ---------------------------------------------------------------


def _require_workspace(db: Database, workspace_name: str) -> WorkspaceRow:
    ws = db.get_workspace(workspace_name)
    if ws is None:
        typer.echo(f"Error: workspace '{workspace_name}' not found", err=True)
        raise typer.Exit(1)
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    assert ws.vm_name is not None
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
        raise typer.Exit(1)
    return vm
