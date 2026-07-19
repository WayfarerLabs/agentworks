"""`agentworks agent` -- manage agents (isolated Linux users) on VMs."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, parse_csv_filter, prompt_vm

agent_app = typer.Typer(
    name="agent",
    help="Manage agents (isolated users on VMs).",
    no_args_is_help=True,
)
app.add_typer(agent_app)


@agent_app.command("create")
def agent_create(
    name: Annotated[str, typer.Argument(help="Agent name")],
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Agent template")] = None,
    grant_all_workspaces: Annotated[
        bool,
        typer.Option("--grant-all-workspaces", help="Grant access to all workspaces"),
    ] = False,
) -> None:
    """Create an agent (isolated Linux user) on a VM."""
    from agentworks.agents.manager import create_agent
    from agentworks.config import load_config

    db = get_db()
    resolved_vm = prompt_vm(db, vm)

    create_agent(
        db,
        load_config(),
        name=name,
        vm_name=resolved_vm.name,
        template=template,
        grant_all_workspaces=grant_all_workspaces,
    )


@agent_app.command("list")
def agent_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one agent name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
) -> None:
    """List agents. --vm accepts comma-separated values for OR-within-filter."""
    from agentworks.agents.manager import list_agents

    list_agents(get_db(), vm_name=parse_csv_filter(vm), names_only=names_only)


@agent_app.command("describe")
def agent_describe(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Show detailed information about an agent."""
    from agentworks.agents.manager import describe_agent

    describe_agent(get_db(), name=name)


@agent_app.command("reinit")
def agent_reinit(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Re-run agent setup using the stored template."""
    from agentworks.agents.manager import reinit_agent
    from agentworks.config import load_config

    reinit_agent(get_db(), load_config(), name=name)


@agent_app.command("grant-workspaces")
def agent_grant_workspaces(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[
        list[str] | None,
        typer.Argument(help="Workspace names (omit when using --all)"),
    ] = None,
    all_workspaces: Annotated[
        bool, typer.Option("--all", help="Grant access to all workspaces")
    ] = False,
) -> None:
    """Grant an agent explicit access to workspaces."""
    from agentworks.agents.grants import grant_workspaces
    from agentworks.config import load_config

    grant_workspaces(
        get_db(),
        load_config(),
        agent_name=name,
        workspace_names=list(workspaces or []),
        grant_all=all_workspaces,
    )


@agent_app.command("revoke-workspaces")
def agent_revoke_workspaces(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[
        list[str] | None,
        typer.Argument(help="Workspace names (omit when using --all)"),
    ] = None,
    all_workspaces: Annotated[
        bool, typer.Option("--all", help="Remove all explicit grants")
    ] = False,
) -> None:
    """Revoke explicit workspace grants from an agent."""
    from agentworks.agents.grants import revoke_workspaces
    from agentworks.config import load_config

    revoke_workspaces(
        get_db(),
        load_config(),
        agent_name=name,
        workspace_names=list(workspaces or []),
        revoke_all=all_workspaces,
    )


@agent_app.command("exec", context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def agent_exec(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Run from a workspace"),
    ] = None,
) -> None:
    """Execute a command as an agent user."""
    from agentworks.agents.manager import exec_agent
    from agentworks.config import load_config

    if not ctx.args:
        typer.echo("Error: missing command", err=True)
        raise typer.Exit(1)
    raise typer.Exit(
        exec_agent(
            get_db(),
            load_config(),
            name=name,
            command=ctx.args,
            workspace_name=workspace,
        )
    )


@agent_app.command("shell")
def agent_shell(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str | None, typer.Option("--workspace", help="cd into a workspace")] = None,
) -> None:
    """Open a shell as an agent user."""
    from agentworks.agents.manager import shell_agent
    from agentworks.config import load_config

    raise typer.Exit(
        shell_agent(get_db(), load_config(), name=name, workspace_name=workspace)
    )


@agent_app.command("delete")
def agent_delete(
    name: Annotated[str, typer.Argument(help="Agent name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with sessions")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete an agent."""
    from agentworks.agents.manager import delete_agent
    from agentworks.config import load_config

    delete_agent(get_db(), load_config(), name=name, force=force, yes=yes)
