"""VM console management.

The console is a VM-level tmux session that provides a unified view of all
sessions running on the VM. It has full tmux controls (the operator can split
panes, create windows, rearrange layout). Each session appears as a window
that attaches to the session's locked-down tmux session.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import NotFoundError, StateError
from agentworks.sessions.tmux import tmux_cmd
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.sessions.tmux import RunCommand

CONSOLE_SESSION_NAME = "vm-console"


def console_exists(*, run_command: RunCommand) -> bool:
    """Check if the console tmux session exists on the VM."""
    result = run_command(f"tmux has-session -t {CONSOLE_SESSION_NAME} 2>/dev/null", check=False)
    return getattr(result, "ok", False)


def create_console(
    sessions: list[SessionRow],
    *,
    run_command: RunCommand,
    admin_username: str,
    recreate: bool = False,
) -> None:
    """Create the VM console session with one window per session.

    When *recreate* is True, kills any existing console session first.
    """
    if recreate:
        run_command(f"tmux kill-session -t {CONSOLE_SESSION_NAME}", check=False)

    # Create the session with a login shell as the initial window.
    # No sudo wrapper: post FRD R1 the SSH user IS the admin user; sudo
    # to admin was a no-op user-switch that wiped env (left over from the
    # pre-direct-target-user-SSH era).
    run_command(
        f"tmux new-session -d -s {CONSOLE_SESSION_NAME} "
        f"-n admin-shell "
        f"{shlex.quote('exec $SHELL -l')}"
    )

    # Keep windows open when attached session command exits
    run_command(f"tmux set -t {CONSOLE_SESSION_NAME} remain-on-exit on", check=False)

    # Add a window for each session (wrapper loop handles ended sessions)
    output.info(f"Adding {len(sessions)} session(s) to console...")
    for session in sessions:
        _add_session_window(
            session.name,
            run_command=run_command,
            socket_path=session.socket_path,
        )


def _add_session_window(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> None:
    """Add a single session window to the console."""
    q_session = shlex.quote(session_name)
    # Unset TMUX to allow nesting (console -> session). The wrapper holds the
    # window open forever: a banner-and-wait entry phase for sessions that
    # aren't up yet, then a main loop that attaches and shows a one-line
    # exit notice on session-end (terminal content preserved). Users dismiss
    # dead windows with their console's kill-window binding.
    has_cmd = tmux_cmd(f"has-session -t {q_session}", socket_path)
    attach_cmd = tmux_cmd(f"attach -t {q_session}", socket_path)
    wrapper = f"""\
unset TMUX

# Entry: if the session isn't up yet, show a banner and wait for it.
if ! {has_cmd} 2>/dev/null; then
    clear
    echo 'Waiting for session {session_name} to come up...'
    while ! {has_cmd} 2>/dev/null; do sleep 2; done
fi

# Main loop: attach; on exit, distinguish detach (re-attach silently) from
# session-end (print a one-line notice, keep terminal content, then wait).
while true; do
    clear
    {attach_cmd}
    rc=$?
    if {has_cmd} 2>/dev/null; then
        continue
    fi
    echo
    if [ "$rc" -eq 0 ]; then
        echo 'Session {session_name} exited cleanly.'
    else
        echo "Session {session_name} exited (status $rc)."
    fi
    echo 'Waiting for session to restart...'
    while ! {has_cmd} 2>/dev/null; do sleep 2; done
done
"""
    result = run_command(
        f"tmux new-window -t {CONSOLE_SESSION_NAME} -n {q_session} {shlex.quote(wrapper)}",
        check=False,
    )
    ok = getattr(result, "ok", True)
    stderr = getattr(result, "stderr", "")
    if not ok:
        output.warn(f"failed to add window for '{session_name}': {stderr}")


def add_session_to_console(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> None:
    """Add a session window to an existing console (best-effort)."""
    if not console_exists(run_command=run_command):
        return

    _add_session_window(session_name, run_command=run_command, socket_path=socket_path)


def attach_console(
    db: Database,
    config: Config,
    *,
    vm_name: str,
    recreate: bool = False,
    allow_nesting: bool = False,
) -> int:
    """Attach to (or create) the VM console.

    Returns the interactive attach's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service).

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph
    derives from the VM's row, the activation gate replaces this
    command's ``bind_platform`` + ``ensure_active`` pair (opening
    BEFORE the preflight sweep), and the gate's held-active span
    covers the console build and the interactive attach, exactly the
    ``vm_active`` hold the imperative body opened. The console itself
    is not a node: attaching provisions nothing, so the graph is the
    live VM alone. No env-chain target registers: the attach joins an
    existing tmux server and composes no env.
    """
    import os

    if os.environ.get("TMUX") and not allow_nesting:
        raise StateError(
            "already inside a tmux session. Nesting is not recommended "
            "(prefix key conflicts, confusing detach behavior).",
            hint="Pass --allow-nesting to override.",
        )

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )

    # Cheap row validation stays pre-gate: a VM with no Tailscale
    # address can never be attached to, so it must fail with zero
    # prompts and zero VM starts. (The imperative body checked this
    # after its gate; the gate cannot populate the address on the
    # already-loaded row, so this command's outcome is identical. The
    # hoist does forgo one accidental heal: the post-gate order could
    # start a stopped VM whose rejoin repopulated the row's address,
    # letting a RETRY succeed; now the retry keeps failing until an
    # explicit vm start or reinit.)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm_name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm_name,
        )

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)

    with gated_vm_boundary(db, config, registry, vm):
        from agentworks.transports import transport

        target = transport(vm, config)

        # Get sessions for this VM (console wrapper handles dead sessions)
        vm_sessions = _get_sessions_for_vm(db, vm)

        if recreate or not console_exists(run_command=target.run):
            create_console(
                vm_sessions,
                run_command=target.run,
                admin_username=vm.admin_username,
                recreate=recreate,
            )

        return target.interactive(f"tmux attach -t {CONSOLE_SESSION_NAME}")


def _get_sessions_for_vm(db: Database, vm: VMRow) -> list[SessionRow]:
    """Get all sessions across all workspaces on a VM."""
    workspaces = db.list_workspaces(vm_name=vm.name)
    sessions: list[SessionRow] = []
    for ws in workspaces:
        sessions.extend(db.list_sessions(workspace_name=ws.name))
    return sessions
