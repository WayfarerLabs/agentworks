"""`agentworks session` -- manage sessions (persistent tmux workloads)."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, parse_csv_filter

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
    vm: Annotated[
        str | None,
        typer.Option(
            "--vm",
            help=(
                "VM anchor. Optional. When omitted: pinned by the workspace or "
                "agent if either resolves to one; otherwise auto-selected from "
                "the single usable VM, or prompted from the list. Required only "
                "in non-interactive mode when nothing else pins the VM and more "
                "than one usable VM exists. When passed alongside other anchors, "
                "must agree with them."
            ),
        ),
    ] = None,
    new_agent: Annotated[bool, typer.Option("--new-agent", help="Create a new agent for this session")] = False,
    agent_name: Annotated[str | None, typer.Option("--agent-name", help="Name for new agent")] = None,
    agent_template: Annotated[
        str | None, typer.Option("--agent-template", help="Template for new agent")
    ] = None,
) -> None:
    """Create and start a session in a workspace."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import create_session

    create_session(
        get_db(),
        load_config(),
        name=name,
        template_name=template,
        workspace=workspace,
        new_workspace=new_workspace,
        workspace_name=workspace_name,
        workspace_template=workspace_template,
        agent=agent,
        new_agent=new_agent,
        agent_name=agent_name,
        agent_template=agent_template,
        admin=admin,
        vm_name=vm,
    )


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
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Filter by agent (agent-mode sessions only)"),
    ] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Only admin-mode sessions (no agent)")] = False,
    no_status: Annotated[bool, typer.Option("--no-status", help="Skip SSH status check (faster)")] = False,
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one session name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
) -> None:
    """List sessions. Filters compose with AND; name filters accept comma-separated values for OR-within-filter."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import list_sessions

    # Validate against the parsed filter, not the raw flag value, so inputs
    # that normalize to "no filter" (whitespace, lone commas) don't falsely
    # trip the mutex.
    parsed_agent = parse_csv_filter(agent)
    if admin and parsed_agent is not None:
        raise typer.BadParameter("--admin and --agent are mutually exclusive")

    list_sessions(
        get_db(),
        load_config(),
        workspace_name=parse_csv_filter(workspace),
        vm_name=parse_csv_filter(vm),
        agent_name=parsed_agent,
        admin_only=admin,
        no_status=no_status,
        names_only=names_only,
    )


@session_app.command("stop")
def session_stop(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Stop all running sessions")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace (with --all)")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Filter by agent (with --all)")] = None,
    admin: Annotated[
        bool, typer.Option("--admin", help="Only admin-mode sessions (with --all)"),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Force-stop broken sessions via PID kill")] = False,
) -> None:
    """Stop a running session, or all running sessions with --all.

    Filters compose with AND. ``--vm``, ``--workspace``, and ``--agent``
    accept a single value or a comma-separated list (e.g.
    ``--vm vm1,vm2``); commas within a filter are OR-ed together.
    """
    from agentworks.config import load_config
    from agentworks.sessions.manager import stop_all_sessions, stop_session

    parsed_vm = parse_csv_filter(vm)
    parsed_workspace = parse_csv_filter(workspace)
    parsed_agent = parse_csv_filter(agent)

    if name and all_sessions:
        raise typer.BadParameter("provide a session name or --all, not both")
    if admin and parsed_agent is not None:
        raise typer.BadParameter("--admin and --agent are mutually exclusive")
    if (parsed_vm or parsed_workspace or parsed_agent or admin) and not all_sessions:
        raise typer.BadParameter("--vm, --workspace, --agent, and --admin require --all")
    if all_sessions:
        stop_all_sessions(
            get_db(),
            load_config(),
            vm_name=parsed_vm,
            workspace_name=parsed_workspace,
            agent_name=parsed_agent,
            admin_only=admin,
            force=force,
        )
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
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Filter by workspace (with --all/--all-stopped)"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Filter by agent (with --all/--all-stopped)"),
    ] = None,
    admin: Annotated[
        bool,
        typer.Option("--admin", help="Only admin-mode sessions (with --all/--all-stopped)"),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
) -> None:
    """Restart a session, or batch restart with --all-stopped / --all.

    Filters compose with AND. ``--vm``, ``--workspace``, and ``--agent``
    accept a single value or a comma-separated list (e.g.
    ``--vm vm1,vm2``); commas within a filter are OR-ed together.
    """
    from agentworks.config import load_config
    from agentworks.sessions.manager import restart_all_sessions, restart_session

    parsed_vm = parse_csv_filter(vm)
    parsed_workspace = parse_csv_filter(workspace)
    parsed_agent = parse_csv_filter(agent)

    if name and (all_stopped or all_sessions):
        raise typer.BadParameter("provide a session name or a batch flag (--all/--all-stopped), not both")
    if all_stopped and all_sessions:
        raise typer.BadParameter("use --all or --all-stopped, not both")
    if admin and parsed_agent is not None:
        raise typer.BadParameter("--admin and --agent are mutually exclusive")
    if (parsed_vm or parsed_workspace or parsed_agent or admin) and not (all_stopped or all_sessions):
        raise typer.BadParameter(
            "--vm, --workspace, --agent, and --admin require --all or --all-stopped"
        )
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

            sessions = filter_sessions(
                db,
                workspace_name=parsed_workspace,
                vm_name=parsed_vm,
                agent_name=parsed_agent,
                admin_only=admin,
            )
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            from agentworks.db import SessionStatus

            status_map = batch_check_all_sessions(sessions, db=db, config=config)
            running = [s for s in sessions if status_map.get(s.name) == SessionStatus.OK]
            if running:
                names = ", ".join(s.name for s in running[:5])
                suffix = f" (and {len(running) - 5} more)" if len(running) > 5 else ""
                output.warn(
                    f"{len(running)} session(s) are running and will be restarted ({names}{suffix})."
                )
                if not output.confirm(
                    "Continue? (--all-stopped restarts only the stopped sessions)"
                ):
                    from agentworks.errors import UserAbort

                    raise UserAbort("restart cancelled")

        restart_all_sessions(
            db,
            config,
            vm_name=parsed_vm,
            workspace_name=parsed_workspace,
            agent_name=parsed_agent,
            admin_only=admin,
            include_running=include_running,
            force=force,
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

    raise typer.Exit(attach_session(get_db(), load_config(), name=name))


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
