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
SSH_AUTH_SOCK_PATH = "/run/agentworks/ssh-agent.sock"


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

    if ws.type == "vm":
        vm = _require_vm_for_workspace(db, ws)
        _create_agent_on_vm(vm, config, linux_user, workspace_name)
    elif ws.type == "local":
        typer.echo("Error: agents are not supported on local workspaces", err=True)
        raise typer.Exit(1)

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
        # Remove live window before killing the user
        _remove_live_window(config, vm, ws.name, name)
        _delete_agent_on_vm(vm, config, agent.linux_user)

    db.delete_agent(workspace_name, name)

    # Regenerate tmuxinator config (after DB delete so agent is excluded)
    if ws.type == "vm":
        _regenerate_tmuxinator(db, config, vm, ws)

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
    assert vm.tailscale_host is not None

    # SSH as user account, then su to the agent user in the workspace directory
    ssh_cmd = ["ssh"]
    if config.user.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.user.ssh_private_key)])
    ssh_cmd.append(f"{vm.vm_user}@{vm.tailscale_host}")
    ssh_cmd.extend(["-t", f"cd {ws.workspace_path} && exec su - {agent.linux_user}"])

    os.execvp("ssh", ssh_cmd)


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

    ssh_run(
        target,
        f"cat > {ws.workspace_path}/.tmuxinator.yml << 'TMUX_EOF'\n{tmux_config}TMUX_EOF",
    )


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
    """Create an agent Linux user on a VM."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    shell = config.user.shell

    typer.echo(f"  Creating user '{linux_user}' on VM '{vm.name}'...")
    run_as_root(target, f"useradd -m -s $(which {shell}) {linux_user}")
    run_as_root(
        target,
        f"usermod -aG ws-{workspace_name},agentworks-ssh,aw-tools {linux_user}",
    )

    # Set SSH_AUTH_SOCK so agent can use shared git credentials
    run_as_root(
        target,
        f"mkdir -p /home/{linux_user}/.config/environment.d",
    )
    run_as_root(
        target,
        f"bash -c \"echo 'SSH_AUTH_SOCK={SSH_AUTH_SOCK_PATH}' "
        f"> /home/{linux_user}/.config/environment.d/ssh-agent.conf\"",
    )
    run_as_root(
        target,
        f"chown -R {linux_user}:{linux_user} /home/{linux_user}/.config",
    )


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
