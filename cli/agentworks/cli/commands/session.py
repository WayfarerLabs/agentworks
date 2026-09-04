"""`agentworks session` -- manage sessions (persistent tmux workloads)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, ordinary_tty_interaction_policy, parse_csv_filter
from agentworks.machine_output import OutputFormat

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy

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
    spec: Annotated[
        str | None,
        typer.Option("--spec", help="Inline JSON instance spec applied after the selected template"),
    ] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Run as the VM admin user")] = False,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent name (agent mode)")] = None,
    new_workspace: Annotated[bool, typer.Option("--new-workspace", help="Create a new workspace")] = False,
    workspace_name: Annotated[str | None, typer.Option("--workspace-name", help="Name for new workspace")] = None,
    workspace_template: Annotated[
        str | None, typer.Option("--workspace-template", help="Template for new workspace")
    ] = None,
    workspace_spec: Annotated[
        str | None,
        typer.Option(
            "--workspace-spec",
            help="Inline JSON workspace instance spec applied after its selected template",
        ),
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
    agent_template: Annotated[str | None, typer.Option("--agent-template", help="Template for new agent")] = None,
    agent_spec: Annotated[
        str | None,
        typer.Option(
            "--agent-spec",
            help="Inline JSON agent instance spec applied after its selected template",
        ),
    ] = None,
) -> None:
    """Create and start a session in a workspace."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.sessions.manager import create_session

    create_session(
        get_db(),
        load_config(),
        name=name,
        template_name=template,
        spec=spec,
        workspace=workspace,
        new_workspace=new_workspace,
        workspace_name=workspace_name,
        workspace_template=workspace_template,
        workspace_spec=workspace_spec,
        agent=agent,
        new_agent=new_agent,
        agent_name=agent_name,
        agent_template=agent_template,
        agent_spec=agent_spec,
        admin=admin,
        vm_name=vm,
        interaction=interaction,
    )


@session_app.command("describe")
def session_describe(
    name: Annotated[str, typer.Argument(help="Session name")],
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show session details."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import describe_session, session_description

    config = load_config(warn_issues=output_format is OutputFormat.HUMAN)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks import output
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.sessions.manager._queries import session_description_data

        with output.suppress_presentation():
            description = session_description(get_db(), config, name=name)
        write_json_envelope(
            MachineOutputCommand.SESSION_DESCRIBE,
            session_description_data(description),
            get_binary_stream("stdout"),
        )
        return
    describe_session(get_db(), config, name=name)


@session_app.command("list")
def session_list(
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Filter by agent (agent-mode sessions only)"),
    ] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Only admin-mode sessions (no agent)")] = False,
    status: Annotated[bool, typer.Option("--status", help="Include live runtime status")] = False,
    no_status: Annotated[bool, typer.Option("--no-status", hidden=True)] = False,
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one session name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """List sessions. Filters compose with AND; name filters accept comma-separated values for OR-within-filter."""
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")
    if status and names_only:
        raise typer.BadParameter("cannot be used with --names-only", param_hint="--status")
    if status and no_status:
        raise typer.BadParameter("cannot be used with --no-status", param_hint="--status")
    if no_status:
        from agentworks import output

        output.deprecation("`session list --no-status` is deprecated; use plain `session list`.")

    from agentworks.config import load_config
    from agentworks.sessions.manager import list_sessions, session_listing

    # Validate against the parsed filter, not the raw flag value, so inputs
    # that normalize to "no filter" (whitespace, lone commas) don't falsely
    # trip the mutex.
    parsed_agent = parse_csv_filter(agent)
    if admin and parsed_agent is not None:
        raise typer.BadParameter("--admin and --agent are mutually exclusive")

    db = get_db()
    config = load_config(warn_issues=output_format is OutputFormat.HUMAN)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks import output
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.sessions.manager._queries import session_listing_data

        with output.suppress_presentation():
            listing = session_listing(
                db,
                config,
                workspace_name=parse_csv_filter(workspace),
                vm_name=parse_csv_filter(vm),
                agent_name=parsed_agent,
                admin_only=admin,
                include_status=status,
            )
        write_json_envelope(
            MachineOutputCommand.SESSION_LIST,
            session_listing_data(listing),
            get_binary_stream("stdout"),
        )
        return
    list_sessions(
        db,
        config,
        workspace_name=parse_csv_filter(workspace),
        vm_name=parse_csv_filter(vm),
        agent_name=parsed_agent,
        admin_only=admin,
        include_status=status,
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
        bool,
        typer.Option("--admin", help="Only admin-mode sessions (with --all)"),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Recover broken session state before stopping")] = False,
) -> None:
    """Stop a running session, or all running sessions with --all.

    Filters compose with AND. ``--vm``, ``--workspace``, and ``--agent``
    accept a single value or a comma-separated list (e.g.
    ``--vm vm1,vm2``); commas within a filter are OR-ed together.
    """
    interaction = ordinary_tty_interaction_policy()
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
            interaction=interaction,
        )
    elif name:
        stop_session(
            get_db(),
            load_config(),
            name=name,
            force=force,
            interaction=interaction,
        )
    else:
        raise typer.BadParameter("provide a session name or use --all")


def _launch_sessions(
    name: str | None,
    *,
    all_sessions: bool,
    vm: str | None,
    workspace: str | None,
    agent: str | None,
    admin: bool,
    force: bool,
    force_new: bool,
    replace_running: bool,
    interaction: TtyInteractionPolicy,
    db: Database | None = None,
    config: Config | None = None,
) -> None:
    """Validate and execute one canonical session launch operation."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import (
        restart_all_sessions,
        restart_session,
        start_all_sessions,
        start_session,
    )

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
        db = db or get_db()
        config = config or load_config()
        batch_operation = restart_all_sessions if replace_running else start_all_sessions
        batch_operation(
            db,
            config,
            vm_name=parsed_vm,
            workspace_name=parsed_workspace,
            agent_name=parsed_agent,
            admin_only=admin,
            force=force,
            force_new=force_new,
            interaction=interaction,
        )
    elif name:
        db = db or get_db()
        config = config or load_config()
        operation = restart_session if replace_running else start_session
        operation(
            db,
            config,
            name=name,
            force=force,
            force_new=force_new,
            interaction=interaction,
        )
    else:
        raise typer.BadParameter("provide a session name or use --all")


def _canonical_launch_options(
    name: str | None,
    *,
    all_sessions: bool,
    vm: str | None,
    workspace: str | None,
    agent: str | None,
    admin: bool,
    force: bool,
    force_new: bool,
    replace_running: bool,
) -> None:
    _launch_sessions(
        name,
        all_sessions=all_sessions,
        vm=vm,
        workspace=workspace,
        agent=agent,
        admin=admin,
        force=force,
        force_new=force_new,
        replace_running=replace_running,
        interaction=ordinary_tty_interaction_policy(),
    )


def _confirm_legacy_resume_replacement(
    db: Database,
    config: Config,
    *,
    name: str | None,
    vm_name: str | list[str] | None,
    workspace_name: str | list[str] | None,
    agent_name: str | list[str] | None,
    admin_only: bool,
) -> None:
    """Preserve the 0.18 running-session confirmation at the CLI shim."""
    from agentworks import output
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.errors import UserAbort
    from agentworks.sessions.manager import (
        filter_sessions,
        observe_session_statuses,
    )

    if name is not None:
        session = db.get_session(name)
        sessions = [] if session is None else [session]
    else:
        sessions = filter_sessions(
            db,
            vm_name=vm_name,
            workspace_name=workspace_name,
            agent_name=agent_name,
            admin_only=admin_only,
        )

    status_map = observe_session_statuses(sessions, db=db, config=config)
    running: list[str] = []
    for session in sessions:
        status = status_map.get(session.name)
        if status is SessionStatus.RUNNING:
            running.append(session.name)
            continue
        if (
            status in (None, SessionStatus.UNKNOWN)
            and session.socket_path is not None
            and session.pid != PID_STOPPED
            and (session.pid is None or session.boot_id is None or session.tmux_server_start_ticks is None)
        ):
            # The read-only batch probe cannot classify an incomplete
            # dedicated-runtime row. Confirm conservatively rather than
            # repairing durable identity before the operator consents.
            running.append(session.name)
    running.sort()
    if not running:
        return

    if not output.is_interactive():
        raise typer.BadParameter(
            "required when replacing running sessions without interactive input", param_hint="--yes"
        )

    if name is not None:
        if not output.confirm(f"Session '{name}' is running. Restart it?"):
            raise UserAbort("resume cancelled")
        return

    names = ", ".join(running[:5])
    suffix = f" (and {len(running) - 5} more)" if len(running) > 5 else ""
    output.warn(f"{len(running)} session(s) are running and will be restarted ({names}{suffix}).")
    if not output.confirm("Continue? (--all-stopped starts only the stopped sessions)"):
        raise UserAbort("resume cancelled")


@session_app.command("start")
def session_start(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Start all sessions")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace (with --all)")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Filter by agent (with --all)")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Only admin-mode sessions (with --all)")] = False,
    force: Annotated[bool, typer.Option("--force", help="Recover broken session state")] = False,
    force_new: Annotated[bool, typer.Option("--force-new", help="Launch a fresh harness conversation")] = False,
) -> None:
    """Start a session, or all sessions with --all."""
    _canonical_launch_options(
        name,
        all_sessions=all_sessions,
        vm=vm,
        workspace=workspace,
        agent=agent,
        admin=admin,
        force=force,
        force_new=force_new,
        replace_running=False,
    )


@session_app.command("restart")
def session_restart(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Restart all sessions")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace (with --all)")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Filter by agent (with --all)")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Only admin-mode sessions (with --all)")] = False,
    force: Annotated[bool, typer.Option("--force", help="Recover broken session state")] = False,
    force_new: Annotated[bool, typer.Option("--force-new", help="Launch a fresh harness conversation")] = False,
) -> None:
    """Restart a session, or all sessions with --all."""
    _canonical_launch_options(
        name,
        all_sessions=all_sessions,
        vm=vm,
        workspace=workspace,
        agent=agent,
        admin=admin,
        force=force,
        force_new=force_new,
        replace_running=True,
    )


@session_app.command("resume", hidden=True)
def session_resume(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_stopped: Annotated[bool, typer.Option("--all-stopped")] = False,
    all_sessions: Annotated[bool, typer.Option("--all")] = False,
    vm: Annotated[str | None, typer.Option("--vm")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    agent: Annotated[str | None, typer.Option("--agent")] = None,
    admin: Annotated[bool, typer.Option("--admin")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Compatibility wrapper for the 0.18 session resume grammar.

    Removed in 0.20.
    """
    from agentworks import output
    from agentworks.config import load_config

    batch = all_stopped or all_sessions
    if all_stopped and all_sessions:
        raise typer.BadParameter("use --all or --all-stopped, not both")
    parsed_vm = parse_csv_filter(vm)
    parsed_workspace = parse_csv_filter(workspace)
    parsed_agent = parse_csv_filter(agent)
    if name and batch:
        raise typer.BadParameter("provide a session name or a batch flag (--all/--all-stopped), not both")
    if admin and parsed_agent is not None:
        raise typer.BadParameter("--admin and --agent are mutually exclusive")
    if (parsed_vm or parsed_workspace or parsed_agent or admin) and not batch:
        raise typer.BadParameter("--vm, --workspace, --agent, and --admin require --all or --all-stopped")
    if name is None and not batch:
        raise typer.BadParameter("provide a session name, --all-stopped, or --all")

    if all_stopped:
        replacement = "session start --all"
    elif all_sessions:
        replacement = "session restart --all"
    else:
        replacement = "session restart"
    output.deprecation(f"`agw session resume` is deprecated; this invocation maps to `{replacement}`.")

    db = get_db()
    config = load_config()
    if not yes and not all_stopped:
        _confirm_legacy_resume_replacement(
            db,
            config,
            name=name,
            vm_name=parsed_vm,
            workspace_name=parsed_workspace,
            agent_name=parsed_agent,
            admin_only=admin,
        )
    _launch_sessions(
        name,
        all_sessions=batch,
        vm=vm,
        workspace=workspace,
        agent=agent,
        admin=admin,
        force=force,
        force_new=False,
        replace_running=not all_stopped,
        interaction=ordinary_tty_interaction_policy(),
        db=db,
        config=config,
    )


@session_app.command("attach")
def session_attach(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Attach to a session."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.sessions.manager import attach_session

    raise typer.Exit(
        attach_session(
            get_db(),
            load_config(),
            name=name,
            interaction=interaction,
        )
    )


@session_app.command("delete")
def session_delete(
    name: Annotated[str, typer.Argument(help="Session name")],
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a session."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.sessions.manager import delete_session

    delete_session(
        get_db(),
        load_config(),
        name=name,
        force=force,
        yes=yes,
        interaction=interaction,
    )


@session_app.command("logs")
def session_logs(
    name: Annotated[str, typer.Argument(help="Session name")],
    lines: Annotated[int | None, typer.Option("--lines", "-n", help="Number of lines")] = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.sessions.manager import session_logs as _session_logs

    _session_logs(
        get_db(),
        load_config(),
        name=name,
        lines=lines,
        interaction=interaction,
    )
