"""`agentworks workspace` -- manage workspaces (project scopes on VMs)."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, parse_csv_filter, prompt_vm

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


@workspace_app.command("shell")
def workspace_shell(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Open a plain shell into a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import shell_workspace

    shell_workspace(get_db(), load_config(), name)


@workspace_app.command("console")
def workspace_console(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    recreate: Annotated[bool, typer.Option("--recreate", help="Kill and rebuild the console")] = False,
    allow_nesting: Annotated[bool, typer.Option("--allow-nesting", help="Allow running inside tmux")] = False,
) -> None:
    """Open the workspace console (tmux session with sessions)."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import console_workspace

    console_workspace(
        get_db(),
        load_config(),
        name,
        allow_nesting=allow_nesting,
        recreate=recreate,
    )


@workspace_app.command("list")
def workspace_list(
    vm: Annotated[
        str | None,
        typer.Option("--vm", help="Filter by VM (comma-separated for multiple)"),
    ] = None,
) -> None:
    """List workspaces."""
    from agentworks.workspaces.manager import list_workspaces

    list_workspaces(get_db(), vm_name=parse_csv_filter(vm))


@workspace_app.command("describe")
def workspace_describe(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Show workspace details, sessions, and agent access."""
    from agentworks.workspaces.manager import describe_workspace

    describe_workspace(get_db(), name)


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


@workspace_app.command("reinit")
def workspace_reinit(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Re-run workspace initialization: group, permissions, ACLs, agent access.

    Idempotent. Converges live VM state (group existence, directory ownership,
    permissions, ACLs, parent traversal, agent group membership) to match what
    the DB declares for this workspace. Same semantic as `vm reinit` and
    `agent reinit`.
    """
    from agentworks.config import load_config
    from agentworks.workspaces.manager import reinit_workspace

    reinit_workspace(get_db(), load_config(), name)


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
