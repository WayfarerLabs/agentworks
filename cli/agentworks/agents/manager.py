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
        _delete_agent_on_vm(vm, config, agent.linux_user)

    db.delete_agent(workspace_name, name)
    typer.echo(f"Agent '{name}' deleted from workspace '{workspace_name}'")


def delete_agents_for_workspace(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
) -> None:
    """Delete all agents for a workspace (called during workspace deletion)."""
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
