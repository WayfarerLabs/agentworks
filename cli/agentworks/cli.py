"""Typer CLI entrypoint for Agentworks."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.db import Database

app = typer.Typer(
    name="agentworks",
    help="Orchestrate workspace lifecycle across multiple compute targets.",
    no_args_is_help=True,
)

# -- Command groups --------------------------------------------------------

vm_host_app = typer.Typer(
    name="vm-host",
    help="Manage VM hosts (machines that run VMs).",
    no_args_is_help=True,
)
app.add_typer(vm_host_app)

vm_app = typer.Typer(
    name="vm",
    help="Manage virtual machines.",
    no_args_is_help=True,
)
app.add_typer(vm_app)

workspace_app = typer.Typer(
    name="workspace",
    help="Manage workspaces.",
    no_args_is_help=True,
)
app.add_typer(workspace_app)


# -- Helpers ---------------------------------------------------------------


def _get_db() -> Database:
    return Database()


# -- VM Host commands ------------------------------------------------------


@vm_host_app.command("add")
def vm_host_add(
    name: Annotated[str, typer.Argument(help="Name for this VM host")],
    ssh_host: Annotated[str, typer.Argument(help="SSH address (hostname or IP)")],
) -> None:
    """Register a new VM host."""
    from agentworks.vm_hosts.manager import add_vm_host

    add_vm_host(_get_db(), name, ssh_host)


@vm_host_app.command("list")
def vm_host_list() -> None:
    """List registered VM hosts."""
    from agentworks.vm_hosts.manager import list_vm_hosts

    list_vm_hosts(_get_db())


@vm_host_app.command("remove")
def vm_host_remove(
    name: Annotated[str, typer.Argument(help="Name of the VM host to remove")],
    force: Annotated[bool, typer.Option("--force", help="Remove even if VMs reference this host")] = False,
) -> None:
    """Remove a VM host."""
    from agentworks.vm_hosts.manager import remove_vm_host

    remove_vm_host(_get_db(), name, force=force)


# -- VM commands (placeholders) --------------------------------------------


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    typer.echo("vm list: not yet implemented")


# -- Workspace commands (placeholders) -------------------------------------


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces."""
    typer.echo("workspace list: not yet implemented")
