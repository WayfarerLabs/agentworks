"""Typer CLI entrypoint for Agentworks."""

import typer

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

# -- Placeholder commands (replaced in later phases) -----------------------


@vm_host_app.command("list")
def vm_host_list() -> None:
    """List registered VM hosts."""
    typer.echo("vm-host list: not yet implemented")


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    typer.echo("vm list: not yet implemented")


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces."""
    typer.echo("workspace list: not yet implemented")
