"""Agent lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import validate_name
from agentworks.ssh import ssh_target_for_vm

if TYPE_CHECKING:
    from agentworks.catalog import UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.ssh import SSHLogger

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
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(vm.name, "agent-create")
        try:
            _create_agent_on_vm(vm, config, linux_user, workspace_name, logger=ssh_logger)
        except Exception as e:
            ssh_logger.close()
            typer.echo(f"Error creating agent: {e}", err=True)
            typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
            typer.echo(f"  Cleaning up user '{linux_user}'...", err=True)
            _delete_agent_on_vm(vm, config, linux_user, logger=ssh_logger)
            raise typer.Exit(1) from None
        ssh_logger.close()

    agent = db.insert_agent(name, workspace_name, linux_user)

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
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(vm.name, "agent-delete")
        _delete_agent_on_vm(vm, config, agent.linux_user, logger=ssh_logger)
        ssh_logger.close()

    db.delete_agent(workspace_name, name)

    typer.echo(f"Agent '{name}' deleted from workspace '{workspace_name}'")


def delete_agents_for_workspace(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
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
                _delete_agent_on_vm(vm, config, agent.linux_user, logger=logger)

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
        typer.echo(f"{agent.name:<20} {agent.workspace_name:<20} {agent.linux_user:<30} {agent.created_at}")


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Open a shell as an agent user on a VM."""
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

    import sys

    from agentworks.ssh import interactive

    target = ssh_target_for_vm(vm, config)
    sys.exit(interactive(target, f"cd {ws.workspace_path} && exec su - {agent.linux_user}"))


# -- VM operations ---------------------------------------------------------


def _create_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Create an agent Linux user on a VM and run user install commands."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    lg = logger

    typer.echo(f"  Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"
    groups = f"ws-{workspace_name},aw-tools"
    run_as_root(target, f"useradd -m -s {AGENT_SHELL} {linux_user}", logger=lg)
    run_as_root(target, f"usermod -aG {groups} {linux_user}", logger=lg)

    # Write a minimal .bashrc with a clear agent prompt
    bashrc = f"export PS1='[agent:{linux_user}] \\w\\$ '"
    run_as_root(target, f"printf '%s\\n' '{bashrc}' > {home}/.bashrc", logger=lg)
    run_as_root(target, f"chown {linux_user}:{linux_user} {home}/.bashrc", logger=lg)

    # Run user install commands for this agent
    _run_agent_install_commands(vm, config, linux_user, home)


def _delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent Linux user from a VM."""
    from agentworks.ssh import SSHError, run_as_root

    target = ssh_target_for_vm(vm, config)
    lg = logger

    try:
        # Kill any running processes for the user
        run_as_root(target, f"pkill -u {linux_user}", check=False, logger=lg)
        # Remove the user and their home directory
        run_as_root(target, f"userdel -r {linux_user}", logger=lg)
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
        # Skip if already installed for this user (short timeout)
        test_cmd = _build_agent_test_command(entry, linux_user, home)
        if test_cmd:
            try:
                check = run_as_root(target, test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    typer.echo(f"  Agent install command {i}/{total} ({name}): already installed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                typer.echo(f"  Warning: install check for '{name}' failed ({e}), assuming not installed", err=True)

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
    entry: UserInstallCommandEntry,
    linux_user: str,
    home: str,
) -> str | None:
    """Build a test command that runs as the agent user."""
    import shlex as _shlex

    test_exec: str | None = getattr(entry, "test_exec", None)
    test_file: str | None = getattr(entry, "test_file", None)
    test_dir: str | None = getattr(entry, "test_dir", None)
    if test_exec:
        # Run command -v as the agent user via su
        inner = f"command -v {_shlex.quote(test_exec)}"
        return f"su - {_shlex.quote(linux_user)} -c {_shlex.quote(inner)} > /dev/null 2>&1"
    if test_file:
        path = test_file.replace("~", home, 1) if test_file.startswith("~") else test_file
        return f"test -f {_shlex.quote(path)}"
    if test_dir:
        path = test_dir.replace("~", home, 1) if test_dir.startswith("~") else test_dir
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
