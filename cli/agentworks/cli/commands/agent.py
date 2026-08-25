"""`agentworks agent` -- manage agents (isolated Linux users) on VMs."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import (
    get_db,
    ordinary_tty_interaction_policy,
    parse_csv_filter,
    prompt_vm,
)
from agentworks.machine_output import OutputFormat

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
    spec: Annotated[str | None, typer.Option("--spec", help="Inline JSON instance spec")] = None,
    grant_all_workspaces: Annotated[
        bool,
        typer.Option("--grant-all-workspaces", help="Grant access to all workspaces"),
    ] = False,
) -> None:
    """Create an agent (isolated Linux user) on a VM."""
    interaction = ordinary_tty_interaction_policy()
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
        spec=spec,
        grant_all_workspaces=grant_all_workspaces,
        interaction=interaction,
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
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """List agents. --vm accepts comma-separated values for OR-within-filter.

    A workspace name suffixed with * in the WORKSPACE GRANTS column holds
    only an implicit grant (from a session), not a standing one.
    """
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")

    from agentworks.agents.manager import agent_listing, list_agents, render_agent_listing

    if names_only:
        list_agents(get_db(), vm_name=parse_csv_filter(vm), names_only=True)
        return

    listing = agent_listing(get_db(), vm_name=parse_csv_filter(vm))
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.agents.manager.inspect import agent_listing_data
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(MachineOutputCommand.AGENT_LIST, agent_listing_data(listing), get_binary_stream("stdout"))
        return
    render_agent_listing(listing, names_only=names_only)


@agent_app.command("describe")
def agent_describe(
    name: Annotated[str, typer.Argument(help="Agent name")],
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show detailed information about an agent."""
    from agentworks.agents.manager import agent_description, render_agent_description

    description = agent_description(get_db(), name=name)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.agents.manager.inspect import agent_description_data
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(
            MachineOutputCommand.AGENT_DESCRIBE,
            agent_description_data(description),
            get_binary_stream("stdout"),
        )
        return
    render_agent_description(description)


@agent_app.command("reinit")
def agent_reinit(
    name: Annotated[str, typer.Argument(help="Agent name")],
    update_template: Annotated[
        str | None,
        typer.Option(
            "--update-template",
            help="Re-point this agent to a different template, then reinit to apply it",
        ),
    ] = None,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec",
            help="Inline JSON instance spec; '{}' or an exact empty string clears it (whitespace is invalid)",
        ),
    ] = None,
) -> None:
    """Re-run agent setup using the stored template and final instance spec."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.agents.manager import reinit_agent
    from agentworks.config import load_config

    reinit_agent(
        get_db(),
        load_config(),
        name=name,
        update_template=update_template,
        spec=spec,
        interaction=interaction,
    )


@agent_app.command("grant-workspaces")
def agent_grant_workspaces(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[
        list[str] | None,
        typer.Argument(help="Workspace names (omit when using --all)"),
    ] = None,
    all_workspaces: Annotated[bool, typer.Option("--all", help="Grant access to all workspaces")] = False,
) -> None:
    """Grant an agent explicit access to workspaces."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.agents.grants import grant_workspaces
    from agentworks.config import load_config

    grant_workspaces(
        get_db(),
        load_config(),
        agent_name=name,
        workspace_names=list(workspaces or []),
        grant_all=all_workspaces,
        interaction=interaction,
    )


@agent_app.command("revoke-workspaces")
def agent_revoke_workspaces(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[
        list[str] | None,
        typer.Argument(help="Workspace names (omit when using --all)"),
    ] = None,
    all_workspaces: Annotated[bool, typer.Option("--all", help="Remove all explicit grants")] = False,
) -> None:
    """Revoke explicit workspace grants from an agent."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.agents.grants import revoke_workspaces
    from agentworks.config import load_config

    revoke_workspaces(
        get_db(),
        load_config(),
        agent_name=name,
        workspace_names=list(workspaces or []),
        revoke_all=all_workspaces,
        interaction=interaction,
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
    interaction = ordinary_tty_interaction_policy()
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
            interaction=interaction,
        )
    )


@agent_app.command("shell")
def agent_shell(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str | None, typer.Option("--workspace", help="cd into a workspace")] = None,
) -> None:
    """Open a shell as an agent user."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.agents.manager import shell_agent
    from agentworks.config import load_config

    raise typer.Exit(
        shell_agent(
            get_db(),
            load_config(),
            name=name,
            workspace_name=workspace,
            interaction=interaction,
        )
    )


@agent_app.command("delete")
def agent_delete(
    name: Annotated[str, typer.Argument(help="Agent name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with sessions")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete an agent."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.agents.manager import delete_agent
    from agentworks.config import load_config

    delete_agent(
        get_db(),
        load_config(),
        name=name,
        force=force,
        yes=yes,
        interaction=interaction,
    )
