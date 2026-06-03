"""`agentworks session` -- manage sessions (persistent tmux workloads)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from agentworks.cli._app import app, require_interactive
from agentworks.cli._helpers import get_db, parse_csv_filter, prompt_vm, prompt_workspace

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow, WorkspaceRow

session_app = typer.Typer(
    name="session",
    help="Manage sessions.",
    no_args_is_help=True,
)
app.add_typer(session_app)


@session_app.command("create")
def session_create(
    name: Annotated[str, typer.Argument(help="Session name")],
    workspace: Annotated[str | None, typer.Option("--workspace", help="Existing workspace")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Session template")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Run as the VM admin user")] = False,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent name (agent mode)")] = None,
    new_workspace: Annotated[bool, typer.Option("--new-workspace", help="Create a new workspace")] = False,
    workspace_name: Annotated[str | None, typer.Option("--workspace-name", help="Name for new workspace")] = None,
    workspace_template: Annotated[
        str | None, typer.Option("--workspace-template", help="Template for new workspace")
    ] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="VM for new workspace")] = None,
    new_agent: Annotated[bool, typer.Option("--new-agent", help="Create a new agent for this session")] = False,
    agent_name: Annotated[str | None, typer.Option("--agent-name", help="Name for new agent")] = None,
    agent_template: Annotated[
        str | None, typer.Option("--agent-template", help="Template for new agent")
    ] = None,
) -> None:
    """Create and start a session in a workspace."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import create_session
    from agentworks.workspaces.manager import create_workspace

    # Validate flag combinations before any prompts
    if admin and agent:
        typer.echo("Error: --admin and --agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if admin and new_agent:
        typer.echo("Error: --admin and --new-agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if agent and new_agent:
        typer.echo("Error: --agent and --new-agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if workspace and new_workspace:
        typer.echo("Error: --workspace and --new-workspace are mutually exclusive", err=True)
        raise typer.Exit(1)
    if not new_workspace and (workspace_name or workspace_template or vm):
        typer.echo(
            "Error: --workspace-name, --workspace-template, and --vm require --new-workspace",
            err=True,
        )
        raise typer.Exit(1)
    if not new_agent and (agent_name or agent_template):
        typer.echo(
            "Error: --agent-name and --agent-template require --new-agent",
            err=True,
        )
        raise typer.Exit(1)

    db = get_db()
    config = load_config()

    resolved_vm: VMRow | None = None

    if new_workspace:
        resolved_vm = prompt_vm(db, vm)
        resolved_workspace = workspace_name  # may be None, resolved after session name

        # Resolve mode (need VM name for agent lookup). Skip the prompt when
        # --new-agent is set -- the user has already chosen to create a new agent.
        resolved_agent: str | None = agent
        if not admin and agent is None and not new_agent:
            # Look up agents on the target VM
            vm_agents = db.list_agents(vm_name=resolved_vm.name)
            if vm_agents:
                require_interactive("--admin or --agent")
                from agentworks import output

                options = ["admin"]
                for a in vm_agents:
                    label = f"agent: {a.name}"
                    if a.template:
                        label += f" [{a.template}]"
                    options.append(label)
                idx = output.choose("Run session as:", options)
                resolved_agent = None if idx == 0 else vm_agents[idx - 1].name

        resolved_ws_name = resolved_workspace or name

        create_workspace(
            db,
            config,
            name=resolved_ws_name,
            vm_name=resolved_vm.name,
            template_name=workspace_template,
        )
        resolved_workspace = resolved_ws_name
    else:
        ws = prompt_workspace(db, workspace)
        resolved_workspace = ws.name

        # Resolve mode. Skip the prompt when --new-agent is set.
        resolved_agent: str | None = agent  # type: ignore[no-redef]
        if not admin and agent is None and not new_agent:
            resolved_agent = _prompt_session_mode(db, ws)

        if new_agent:
            # Agents are VM-scoped; pick up the workspace's VM.
            vm_row = db.get_vm(ws.vm_name)
            assert vm_row is not None  # FK guarantees existence
            resolved_vm = vm_row

    if new_agent:
        assert resolved_vm is not None
        from agentworks.agents.manager import create_agent

        resolved_agent_name = agent_name or name
        create_agent(
            db,
            config,
            name=resolved_agent_name,
            vm_name=resolved_vm.name,
            template=agent_template,
        )
        resolved_agent = resolved_agent_name

    create_session(
        db,
        config,
        name=name,
        workspace_name=resolved_workspace,
        template_name=template,
        agent_name=resolved_agent,
        created_workspace=new_workspace,
        created_agent=new_agent,
    )


def _prompt_session_mode(db: Database, ws: WorkspaceRow) -> str | None:
    """Prompt for admin vs agent mode. Returns agent name or None for admin."""
    from agentworks import output

    agents = db.list_agents(vm_name=ws.vm_name)
    if not agents:
        # No agents on this VM, default to admin
        return None

    require_interactive("--admin or --agent")

    options = ["admin"]
    for a in agents:
        label = f"agent: {a.name}"
        if a.template:
            label += f" [{a.template}]"
        options.append(label)

    idx = output.choose("Run session as:", options)
    if idx == 0:
        return None
    return agents[idx - 1].name


@session_app.command("describe")
def session_describe(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Show session details."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import describe_session

    describe_session(get_db(), load_config(), name=name)


@session_app.command("list")
def session_list(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Filter by workspace (comma-separated for multiple)"),
    ] = None,
    vm: Annotated[
        str | None,
        typer.Option("--vm", help="Filter by VM (comma-separated for multiple)"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Filter by agent (agent-mode sessions only; comma-separated for multiple)",
        ),
    ] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Only admin-mode sessions (no agent)")] = False,
    no_status: Annotated[bool, typer.Option("--no-status", help="Skip SSH status check (faster)")] = False,
) -> None:
    """List sessions. Filters compose with AND; comma-separated values within a filter are OR-ed."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import list_sessions

    if admin and agent:
        typer.echo("Error: --admin and --agent are mutually exclusive", err=True)
        raise typer.Exit(1)

    list_sessions(
        get_db(),
        load_config(),
        workspace_name=parse_csv_filter(workspace),
        vm_name=parse_csv_filter(vm),
        agent_name=parse_csv_filter(agent),
        admin_only=admin,
        no_status=no_status,
    )


@session_app.command("stop")
def session_stop(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Stop all running sessions")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace (with --all)")] = None,
    force: Annotated[bool, typer.Option("--force", help="Force-stop broken sessions via PID kill")] = False,
) -> None:
    """Stop a running session, or all running sessions with --all."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import stop_all_sessions, stop_session

    if name and all_sessions:
        raise typer.BadParameter("provide a session name or --all, not both")
    if (vm or workspace) and not all_sessions:
        raise typer.BadParameter("--vm and --workspace require --all")
    if all_sessions:
        stop_all_sessions(get_db(), load_config(), vm_name=vm, workspace_name=workspace, force=force)
    elif name:
        stop_session(get_db(), load_config(), name=name, force=force)
    else:
        raise typer.BadParameter("provide a session name or use --all")


@session_app.command("restart")
def session_restart(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_stopped: Annotated[bool, typer.Option("--all-stopped", help="Restart all stopped sessions")] = False,
    all_sessions: Annotated[bool, typer.Option("--all", help="Restart all sessions (prompts for running)")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all/--all-stopped)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
) -> None:
    """Restart a session, or batch restart with --all-stopped / --all."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import restart_all_sessions, restart_session

    if name and (all_stopped or all_sessions):
        raise typer.BadParameter("provide a session name or a batch flag (--all/--all-stopped), not both")
    if all_stopped and all_sessions:
        raise typer.BadParameter("use --all or --all-stopped, not both")
    if (vm or workspace) and not (all_stopped or all_sessions):
        raise typer.BadParameter("--vm and --workspace require --all or --all-stopped")
    if all_stopped or all_sessions:
        db = get_db()
        config = load_config()
        include_running = all_sessions

        # --all without --yes: prompt if there are running sessions
        if include_running and not yes:
            from agentworks import output
            from agentworks.sessions.manager import (
                batch_check_all_sessions,
                ensure_pids_batch,
                filter_sessions,
            )

            sessions = filter_sessions(db, workspace_name=workspace, vm_name=vm)
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            from agentworks.db import SessionStatus

            status_map = batch_check_all_sessions(sessions, db=db, config=config)
            running = [s for s in sessions if status_map.get(s.name) == SessionStatus.OK]
            if running:
                names = ", ".join(s.name for s in running[:5])
                suffix = f" (and {len(running) - 5} more)" if len(running) > 5 else ""
                output.warn(
                    f"{len(running)} session(s) are running and will be restarted ({names}{suffix}).\n"
                    "Hint: use --all-stopped to restart only stopped sessions."
                )
                if not output.confirm("Continue?"):
                    from agentworks.errors import UserAbort

                    raise UserAbort("restart cancelled")

        restart_all_sessions(
            db, config, vm_name=vm, workspace_name=workspace, include_running=include_running, force=force,
        )
    elif name:
        restart_session(get_db(), load_config(), name=name, force=force, yes=yes)
    else:
        raise typer.BadParameter("provide a session name, --all-stopped, or --all")


@session_app.command("attach")
def session_attach(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Attach to a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import attach_session

    attach_session(get_db(), load_config(), name=name)


@session_app.command("delete")
def session_delete(
    name: Annotated[str, typer.Argument(help="Session name")],
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import delete_session

    delete_session(get_db(), load_config(), name=name, force=force, yes=yes)


@session_app.command("logs")
def session_logs(
    name: Annotated[str, typer.Argument(help="Session name")],
    lines: Annotated[int | None, typer.Option("--lines", "-n", help="Number of lines")] = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import session_logs as _session_logs

    _session_logs(get_db(), load_config(), name=name, lines=lines)
