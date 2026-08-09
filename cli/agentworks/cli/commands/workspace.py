"""`agentworks workspace` -- manage workspaces (project scopes on VMs)."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, parse_csv_filter, prompt_vm
from agentworks.machine_output import OutputFormat, select_request_output

workspace_app = typer.Typer(
    name="workspace",
    help="Manage workspaces.",
    no_args_is_help=True,
)
app.add_typer(workspace_app)


@workspace_app.command("create")
def workspace_create(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Workspace template")] = None,
    open_vscode: Annotated[bool, typer.Option("--open-vscode", help="Open in VS Code")] = False,
) -> None:
    """Create a workspace on a VM."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import create_workspace

    db = get_db()
    resolved_vm = prompt_vm(db, vm)

    create_workspace(
        db,
        load_config(),
        name=name,
        vm_name=resolved_vm.name,
        template_name=template,
        open_vscode=open_vscode,
    )


@workspace_app.command("list")
def workspace_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one workspace name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """List workspaces. --vm accepts comma-separated values for OR-within-filter."""
    select_request_output(output_format)
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")

    from agentworks.workspaces.manager import render_workspace_listing, workspace_listing

    listing = workspace_listing(get_db(), vm_name=parse_csv_filter(vm))
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.workspaces.manager.create import workspace_listing_data

        write_json_envelope(
            MachineOutputCommand.WORKSPACE_LIST,
            workspace_listing_data(listing),
            get_binary_stream("stdout"),
        )
        return
    render_workspace_listing(listing, names_only=names_only)


@workspace_app.command("describe")
def workspace_describe(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show workspace details, sessions, and agent access."""
    select_request_output(output_format)
    from agentworks.workspaces.manager import render_workspace_description, workspace_description

    description = workspace_description(get_db(), name)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.workspaces.manager.create import workspace_description_data

        write_json_envelope(
            MachineOutputCommand.WORKSPACE_DESCRIBE,
            workspace_description_data(description),
            get_binary_stream("stdout"),
        )
        return
    render_workspace_description(description)


@workspace_app.command("rehome")
def workspace_rehome(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    target: Annotated[
        str | None, typer.Option("--target", help="Target path (default: configured workspace dir)")
    ] = None,
    remove_old: Annotated[
        bool, typer.Option("--remove-old", help="Remove the old directory after verified copy")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Move a workspace to a new directory path."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import rehome_workspace

    rehome_workspace(get_db(), load_config(), name, target_path=target, remove_old=remove_old, yes=yes)


@workspace_app.command("repair")
def workspace_repair(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Repair workspace infrastructure: group, permissions, ACLs, agent access.

    Idempotent. Converges live VM workspace state (group existence, directory
    ownership, permissions, ACLs, parent traversal, agent access, and git
    identity) to match what the DB declares for this workspace. The workspace
    analog of the `vm reinit` / `agent reinit` convergence, named `repair`
    because reconciling that on-VM infrastructure is what it does.
    """
    from agentworks.config import load_config
    from agentworks.workspaces.manager import repair_workspace

    repair_workspace(get_db(), load_config(), name)


@workspace_app.command("delete")
def workspace_delete(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with sessions")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import delete_workspace

    delete_workspace(get_db(), load_config(), name, force=force, yes=yes)


@workspace_app.command("copy")
def workspace_copy(
    source: Annotated[str, typer.Argument(help="Source workspace name")],
    name: Annotated[str, typer.Argument(help="New workspace name")],
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
) -> None:
    """Copy a workspace to a new VM workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import copy_workspace

    copy_workspace(
        get_db(),
        load_config(),
        source,
        dest_name=name,
        vm_name=vm,
    )
