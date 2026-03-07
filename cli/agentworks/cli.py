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


# -- VM commands -----------------------------------------------------------


@vm_app.command("create")
def vm_create(
    name: Annotated[str | None, typer.Option("--name", help="VM name (auto-generated if omitted)")] = None,
    platform: Annotated[str | None, typer.Option("--platform", help="Platform: lima, azure, wsl2")] = None,
    vm_host: Annotated[str | None, typer.Option("--vm-host", help="VM host for Lima")] = None,
    extra_packages: Annotated[
        list[str] | None, typer.Option("--extra-packages", help="Additional apt packages")
    ] = None,
    git_hosts: Annotated[list[str] | None, typer.Option("--git-hosts", help="Git hosts to register")] = None,
) -> None:
    """Create a new VM (provision + initialize)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import create_vm

    config = load_config()
    create_vm(
        _get_db(), config,
        name=name, platform=platform, vm_host=vm_host,
        extra_packages=extra_packages, git_hosts=git_hosts,
    )


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    from agentworks.vms.manager import list_vms

    list_vms(_get_db())


@vm_app.command("start")
def vm_start(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Start a stopped VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import start_vm

    start_vm(_get_db(), load_config(), name)


@vm_app.command("stop")
def vm_stop(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Stop a running VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import stop_vm

    stop_vm(_get_db(), load_config(), name)


@vm_app.command("delete")
def vm_delete(
    name: Annotated[str, typer.Argument(help="VM name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with workspaces")] = False,
) -> None:
    """Delete a VM and clean up all resources."""
    from agentworks.config import load_config
    from agentworks.vms.manager import delete_vm

    delete_vm(_get_db(), load_config(), name, force=force)


# -- Workspace commands (placeholders) -------------------------------------


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces."""
    typer.echo("workspace list: not yet implemented")
