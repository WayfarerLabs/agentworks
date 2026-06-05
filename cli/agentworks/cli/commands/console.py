"""`agentworks console` -- named consoles: curated tmux views over sessions."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, prompt_vm

console_app = typer.Typer(
    name="console",
    help="Manage named consoles (curated tmux views of VM sessions).",
    no_args_is_help=True,
)
app.add_typer(console_app)


@console_app.command("create")
def console_create(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[
        list[str] | None,
        typer.Argument(
            help="Sessions to include. Use 'name' or 'name+N' for N default shells.",
        ),
    ] = None,
    vm: Annotated[
        str | None,
        typer.Option(
            "--vm",
            help="Target VM (inferred from listed sessions; otherwise auto-picked or prompted)",
        ),
    ] = None,
    all_sessions: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Fill in every other session on the VM (0 shells each, alphabetical) after the explicit specs",
        ),
    ] = False,
    all_running: Annotated[
        bool,
        typer.Option(
            "--all-running",
            help=(
                "Like --all but only sessions whose live tmux state is OK "
                "(one SSH probe; VM must be reachable)"
            ),
        ),
    ] = False,
    add_admin_shell: Annotated[
        bool,
        typer.Option(
            "--add-admin-shell",
            help="Include a top-level admin-shell window (legacy vm-console behavior)",
        ),
    ] = False,
) -> None:
    """Create a named console with a curated set of sessions."""
    from agentworks.sessions.multi_console import (
        create_console,
        infer_vm_from_session_specs,
        parse_session_spec,
        running_session_names,
    )

    if all_sessions and all_running:
        raise typer.BadParameter("use --all or --all-running, not both")

    # Validate every spec up front so bad input (e.g. 'bad+nope') fails before
    # we prompt for a VM or hit the network on --all-running.
    specs = list(sessions or [])
    for s in specs:
        parse_session_spec(s)

    db = get_db()
    # Resolve target VM:
    #   1. explicit --vm  -> validated by prompt_vm
    #   2. inferred from listed sessions
    #   3. fall back to interactive/auto prompt
    if vm is None:
        vm = infer_vm_from_session_specs(db, specs)
    resolved_vm = prompt_vm(db, vm)

    if all_running:
        # Live SSH probe (one round-trip per VM) so --all-running reflects
        # reality, not stale DB state. If the probe finds nothing and the
        # operator didn't list sessions or pass --add-admin-shell,
        # create_console below raises the canonical "empty console" error.
        from agentworks.config import load_config

        running = running_session_names(db, load_config(), resolved_vm.name)
        explicit_names = {parse_session_spec(s).name for s in specs}
        extras = [n for n in running if n not in explicit_names]
        specs.extend(extras)

    create_console(
        db,
        name=name,
        vm_name=resolved_vm.name,
        session_specs=specs,
        fill_all=all_sessions,
        add_admin_shell=add_admin_shell,
    )


@console_app.command("list")
def console_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
) -> None:
    """List consoles."""
    from agentworks.sessions.multi_console import list_consoles

    list_consoles(get_db(), vm_name=vm)


@console_app.command("describe")
def console_describe(
    name: Annotated[str, typer.Argument(help="Console name")],
) -> None:
    """Show a console's membership and shell layout."""
    from agentworks.sessions.multi_console import describe_console

    describe_console(get_db(), name=name)


@console_app.command("attach")
def console_attach(
    name: Annotated[str, typer.Argument(help="Console name")],
    recreate: Annotated[
        bool, typer.Option("--recreate", help="Kill and rebuild the console's tmux state")
    ] = False,
    allow_nesting: Annotated[
        bool, typer.Option("--allow-nesting", help="Allow attaching from inside an existing tmux")
    ] = False,
) -> None:
    """Attach to a named console (creating its tmux state on first attach)."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import attach_console

    attach_console(
        get_db(),
        load_config(),
        name=name,
        recreate=recreate,
        allow_nesting=allow_nesting,
    )


@console_app.command("delete")
def console_delete(
    name: Annotated[str, typer.Argument(help="Console name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Delete a console: tear down its tmux session (best-effort) and remove its DB row."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import delete_console

    delete_console(get_db(), load_config(), name=name, yes=yes)


@console_app.command("add-sessions")
def console_add_sessions(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[
        list[str],
        typer.Argument(help="Sessions to add. Use 'name' or 'name+N' for N default shells."),
    ],
) -> None:
    """Append sessions to an existing console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import add_sessions

    add_sessions(get_db(), load_config(), console_name=name, session_specs=sessions)


@console_app.command("remove-sessions")
def console_remove_sessions(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[list[str], typer.Argument(help="Session names to remove")],
) -> None:
    """Remove sessions from a console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import remove_sessions

    remove_sessions(get_db(), load_config(), console_name=name, session_names=sessions)


@console_app.command("reorder-sessions")
def console_reorder_sessions(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[
        list[str],
        typer.Argument(
            help=(
                "Sessions (already members of the console) to bump to the "
                "front, in the order they should appear after the admin-shell "
                "window (if any)."
            ),
        ),
    ],
) -> None:
    """Bump existing console members to the front of the session order."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import reorder_sessions

    reorder_sessions(
        get_db(), load_config(), console_name=name, session_names=sessions
    )


@console_app.command("add-shell")
def console_add_shell(
    name: Annotated[str, typer.Argument(help="Console name")],
    session: Annotated[str, typer.Argument(help="Session whose window gets the new pane")],
    cwd: Annotated[
        str | None,
        typer.Option("--cwd", help="Path relative to the workspace root (default = workspace root)"),
    ] = None,
    admin: Annotated[
        bool,
        typer.Option("--admin", help="Run shell as the VM admin user instead of the session's agent user"),
    ] = False,
) -> None:
    """Add a shell pane to a session window in a console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import add_shell

    add_shell(
        get_db(),
        load_config(),
        console_name=name,
        session_name=session,
        cwd=cwd,
        admin=admin,
    )


@console_app.command("restore-session")
def console_restore_session(
    name: Annotated[str, typer.Argument(help="Console name")],
    session: Annotated[str, typer.Argument(help="Session whose window to restore")],
) -> None:
    """Reconcile a session window's live shell panes against the configured list.

    Re-adds any panes you killed (e.g. accidentally), restoring each one to
    its original position. Refuses to remove panes if you have more live than
    configured; for that, use `console attach --recreate`. Consoles created
    before pane-tagging existed require `attach --recreate` once to retag from
    scratch.
    """
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import restore_session

    restore_session(
        get_db(),
        load_config(),
        console_name=name,
        session_name=session,
    )
