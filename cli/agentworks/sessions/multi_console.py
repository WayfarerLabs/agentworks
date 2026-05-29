"""Named consoles with explicit session lists.

A console is a named tmux session on a VM that aggregates a curated subset of
the VM's sessions as windows, with optional extra shell panes per session
window. Unlike the legacy vm-console (one per VM, holds all sessions), a
console is created explicitly with a chosen set of sessions and can be
attached, modified, or deleted independently.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.sessions.tmux import tmux_cmd

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import (
        ConsoleRow,
        ConsoleSessionRow,
        Database,
        SessionRow,
        ShellEntry,
        VMRow,
    )
    from agentworks.ssh import ExecTarget

TMUX_PREFIX = "aw-console-"


def tmux_session_name(console_name: str) -> str:
    """Return the tmux session name for a console."""
    return f"{TMUX_PREFIX}{console_name}"


# -- Spec parsing ----------------------------------------------------------


@dataclass(frozen=True)
class SessionSpec:
    """A session name plus a default-shell count requested via '+N' shorthand."""

    name: str
    shells: int


def parse_session_spec(spec: str) -> SessionSpec:
    """Parse 'session' or 'session+N' into a SessionSpec.

    The shell count N must be a non-negative integer. Raises ValidationError
    on syntax errors or invalid session names; does not check existence.
    """
    parts = spec.split("+")
    if len(parts) == 1:
        name = parts[0]
        shells = 0
    elif len(parts) == 2:
        name = parts[0]
        try:
            shells = int(parts[1])
        except ValueError:
            raise output.ValidationError(
                f"invalid session spec '{spec}': shell count must be a non-negative integer"
            ) from None
        if shells < 0:
            raise output.ValidationError(
                f"invalid session spec '{spec}': shell count must be >= 0"
            )
    else:
        raise output.ValidationError(
            f"invalid session spec '{spec}': use 'name' or 'name+N'"
        )
    try:
        validate_name(name)
    except output.ValidationError as exc:
        raise output.ValidationError(f"invalid session spec '{spec}': {exc}") from None
    return SessionSpec(name=name, shells=shells)


def default_shells(count: int) -> list[ShellEntry]:
    """Build N default shell entries (agent user, workspace root)."""
    return [{"cwd": None, "admin": False} for _ in range(count)]


# -- Helpers ---------------------------------------------------------------


def _require_console(db: Database, name: str) -> ConsoleRow:
    console = db.get_console(name)
    if console is None:
        raise output.ConsoleError(f"console '{name}' not found")
    return console


def _vm_sessions(db: Database, vm_name: str) -> list[SessionRow]:
    """All sessions belonging to workspaces on the given VM."""
    sessions: list[SessionRow] = []
    for ws in db.list_workspaces(vm_name=vm_name):
        sessions.extend(db.list_sessions(workspace_name=ws.name))
    return sessions


def _verify_session_on_vm(db: Database, session_name: str, vm_name: str) -> None:
    """Raise if the session does not exist or is not on the given VM."""
    session = db.get_session(session_name)
    if session is None:
        raise output.SessionError(f"session '{session_name}' not found")
    ws = db.get_workspace(session.workspace_name)
    if ws is None or ws.vm_name != vm_name:
        raise output.ConsoleError(
            f"session '{session_name}' is not on VM '{vm_name}'"
        )


def _dedupe_specs(specs: list[SessionSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise output.ConsoleError(f"session '{spec.name}' listed more than once")
        seen.add(spec.name)


def _shell_summary(shells: list[ShellEntry]) -> str:
    if not shells:
        return "no extra shells"
    parts = []
    for s in shells:
        cwd = s.get("cwd") or "<workspace>"
        user_tag = "admin" if s.get("admin", False) else "agent"
        parts.append(f"{user_tag}:{cwd}")
    return f"{len(shells)} shell(s): " + ", ".join(parts)


# -- Orchestration (DB only; tmux side handled by companion module) -------


def create_console(
    db: Database,
    *,
    name: str,
    vm_name: str,
    session_specs: list[str],
    fill_all: bool = False,
    add_admin_shell: bool = False,
) -> None:
    """Create a new console with the given sessions.

    Explicit *session_specs* keep their argument order. When *fill_all* is
    True, every other session on the VM is appended in alphabetical order
    with zero shells. When *add_admin_shell* is True, the tmux build will
    also include a window 0 running a login shell as the VM admin (legacy
    vm-console behavior) -- useful when you want a top-level shell alongside
    the curated session windows. All inserts run in one transaction; the
    console is not created if any step fails.
    """
    validate_name(name)

    if db.get_console(name) is not None:
        raise output.ConsoleError(f"console '{name}' already exists")
    if db.get_vm(vm_name) is None:
        raise output.VMError(f"VM '{vm_name}' not found")

    specs = [parse_session_spec(s) for s in session_specs]
    _dedupe_specs(specs)
    for spec in specs:
        _verify_session_on_vm(db, spec.name, vm_name)

    if fill_all:
        explicit_names = {s.name for s in specs}
        extras = sorted(
            s.name for s in _vm_sessions(db, vm_name) if s.name not in explicit_names
        )
        specs.extend(SessionSpec(name=n, shells=0) for n in extras)

    if not specs and not add_admin_shell:
        # Almost certainly a typo / misunderstanding rather than an empty console.
        detail = (
            f"VM '{vm_name}' has no sessions"
            if fill_all
            else "specify at least one session, pass --all, or pass --add-admin-shell"
        )
        raise output.ConsoleError(
            f"refusing to create empty console '{name}' ({detail})"
        )

    with db.transaction():
        db.insert_console(name, vm_name, admin_shell=add_admin_shell)
        for spec in specs:
            db.add_console_session(name, spec.name, default_shells(spec.shells))

    extras_note = " + admin shell" if add_admin_shell else ""
    output.info(f"Console '{name}' created with {len(specs)} session(s){extras_note}.")


def add_sessions(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_specs: list[str],
) -> None:
    """Append sessions to an existing console in argument order. Atomic at the
    DB layer; if the console's tmux session is live, also adds the windows
    immediately (best-effort)."""
    console = _require_console(db, console_name)
    specs = [parse_session_spec(s) for s in session_specs]
    _dedupe_specs(specs)

    for spec in specs:
        _verify_session_on_vm(db, spec.name, console.vm_name)
        if db.get_console_session(console_name, spec.name) is not None:
            raise output.ConsoleError(
                f"session '{spec.name}' is already a member of console '{console_name}'"
            )

    with db.transaction():
        for spec in specs:
            db.add_console_session(console_name, spec.name, default_shells(spec.shells))

    output.info(f"Added {len(specs)} session(s) to console '{console_name}'.")

    with _live_best_effort(f"add-session to '{console_name}'"):
        live = _live_target(db, config, console.vm_name)
        if live is None:
            return
        vm, target = live
        if not _console_tmux_exists(target, console_name):
            return
        for spec in specs:
            member = db.get_console_session(console_name, spec.name)
            assert member is not None
            _add_session_window(
                target, db, console_name=console_name, member=member, vm=vm
            )


def remove_sessions(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_names: list[str],
) -> None:
    """Remove sessions from a console. Raises if any are not members. Atomic
    at the DB layer; if the console is live, also kills the corresponding
    windows (best-effort)."""
    console = _require_console(db, console_name)
    for n in session_names:
        if db.get_console_session(console_name, n) is None:
            raise output.ConsoleError(
                f"session '{n}' is not a member of console '{console_name}'"
            )
    with db.transaction():
        for n in session_names:
            db.remove_console_session(console_name, n)
    output.info(
        f"Removed {len(session_names)} session(s) from console '{console_name}'."
    )

    with _live_best_effort(f"remove-session from '{console_name}'"):
        live = _live_target(db, config, console.vm_name)
        if live is None:
            return
        _vm, target = live
        if not _console_tmux_exists(target, console_name):
            return
        q_con = shlex.quote(tmux_session_name(console_name))
        for n in session_names:
            q_win = shlex.quote(n)
            target.run(
                f"tmux kill-window -t {q_con}:{q_win}",
                check=False,
            )


def delete_console_record(db: Database, *, name: str) -> None:
    """Delete the DB record for a console. Cascade handles its session list.

    Tmux teardown is the caller's responsibility.
    """
    _require_console(db, name)
    db.delete_console(name)
    output.info(f"Console '{name}' deleted.")


def _validate_cwd(cwd: str | None) -> None:
    """Reject working directories that escape the workspace root (absolute path or .. segments)."""
    if cwd is None:
        return
    if not cwd:
        raise output.ValidationError("cwd may not be empty (omit it for workspace root)")
    if cwd.startswith("/"):
        raise output.ValidationError(
            f"cwd '{cwd}' must be relative to the workspace root, not absolute"
        )
    if ".." in cwd.split("/"):
        raise output.ValidationError(
            f"cwd '{cwd}' may not contain '..' segments"
        )


def add_shell(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_name: str,
    cwd: str | None = None,
    admin: bool = False,
) -> None:
    """Append a single shell entry to a session's window in a console. If the
    console is live, also splits the pane immediately (best-effort)."""
    _validate_cwd(cwd)
    console = _require_console(db, console_name)
    cs = db.get_console_session(console_name, session_name)
    if cs is None:
        raise output.ConsoleError(
            f"session '{session_name}' is not a member of console '{console_name}'"
        )
    new_shell: ShellEntry = {"cwd": cwd, "admin": admin}
    new_shells = [*cs.shells, new_shell]
    db.update_console_shells(console_name, session_name, new_shells)
    user_tag = "admin" if admin else "agent"
    output.info(
        f"Added {user_tag} shell at {cwd or '<workspace>'} to '{session_name}' "
        f"in console '{console_name}'."
    )

    with _live_best_effort(f"add-shell to '{console_name}:{session_name}'"):
        live = _live_target(db, config, console.vm_name)
        if live is None:
            return
        vm, target = live
        if not _console_tmux_exists(target, console_name):
            return
        session = db.get_session(session_name)
        if session is None:
            return
        workspace_path = _resolve_workspace_path(db, session)
        if workspace_path is None:
            return
        session_user = _session_linux_user(db, session, vm)
        _split_shell_pane(
            target,
            console_name=console_name,
            window_name=session_name,
            workspace_path=workspace_path,
            shell=new_shell,
            session_user=session_user,
            admin_user=vm.admin_username,
        )
        q_con = shlex.quote(tmux_session_name(console_name))
        q_win = shlex.quote(session_name)
        target.run(
            f"tmux select-layout -t {q_con}:{q_win} tiled",
            check=False,
        )


# -- Read-side helpers ----------------------------------------------------


def list_consoles(db: Database, *, vm_name: str | None = None) -> None:
    """Print a table of consoles, optionally filtered by VM."""
    consoles = db.list_consoles_with_counts(vm_name=vm_name)
    if not consoles:
        output.info("No consoles found.")
        return

    rows = [(c.name, c.vm_name, str(n)) for c, n in consoles]
    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    vm_w = max(len("VM"), max(len(r[1]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'VM':<{vm_w}}  SESSIONS"
    output.info(header)
    output.info("-" * len(header))
    for n, vm, count in rows:
        output.info(f"{n:<{name_w}}  {vm:<{vm_w}}  {count}")


def describe_console(db: Database, *, name: str) -> None:
    """Print console membership and shell layout."""
    console = _require_console(db, name)
    members = db.list_console_sessions(name)

    output.info(f"Name:        {console.name}")
    output.info(f"VM:          {console.vm_name}")
    output.info(f"Admin shell: {'yes' if console.admin_shell else 'no'}")
    output.info(f"Created:     {console.created_at}")
    output.info(f"Updated:     {console.updated_at}")
    output.info(f"Sessions:    {len(members)}")

    if not members:
        return

    output.info("")
    for i, m in enumerate(members):
        output.info(f"  [{i}] {m.session_name}  ({_shell_summary(m.shells)})")


# -- Tmux orchestration ----------------------------------------------------


def _session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user that owns a session's tmux server."""
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise output.AgentError(
                f"agent '{session.agent_name}' not found "
                f"(referenced by session '{session.name}')"
            )
        return agent.linux_user
    return vm.admin_username


def _attach_loop_wrapper(session_name: str, socket_path: str | None) -> str:
    """Build the shell snippet that holds a console window open for the given
    session.

    Two phases:
    1. Entry: if the session isn't up yet, clear the pane and show a "Waiting..."
       banner, then poll silently until the session appears.
    2. Main loop: attach. On exit, distinguish a tmux detach (session still
       alive -> re-attach silently next iteration) from a session-end (print
       a one-line exit notice in-place so the last terminal content stays
       visible for scroll-back, then poll silently for the next start).

    The wrapper never exits on its own; users dismiss dead windows with their
    console's kill-window binding. Names are validated to [a-z0-9_-]+, so
    embedding the raw session_name inside the single-quoted strings is safe.
    """
    q = shlex.quote(session_name)
    has = tmux_cmd(f"has-session -t {q}", socket_path)
    att = tmux_cmd(f"attach -t {q}", socket_path)
    return f"""\
unset TMUX

# Entry: if the session isn't up yet, show a banner and wait for it.
if ! {has} 2>/dev/null; then
    clear
    echo 'Waiting for session {session_name} to come up...'
    echo 'Waiting for session to restart...'
    while ! {has} 2>/dev/null; do sleep 2; done
fi

# Main loop: attach; on exit, distinguish detach (re-attach silently) from
# session-end (print a one-line notice, keep terminal content, then wait).
while true; do
    clear
    {att}
    rc=$?
    if {has} 2>/dev/null; then
        continue
    fi
    echo
    if [ "$rc" -eq 0 ]; then
        echo 'Session {session_name} ended cleanly.'
    else
        echo "Session {session_name} ended (status $rc)."
    fi
    while ! {has} 2>/dev/null; do sleep 2; done
done
"""


def _console_tmux_exists(target: ExecTarget, console_name: str) -> bool:
    q = shlex.quote(tmux_session_name(console_name))
    return target.run(f"tmux has-session -t {q} 2>/dev/null", check=False).ok


def _kill_console_tmux(target: ExecTarget, console_name: str) -> None:
    q = shlex.quote(tmux_session_name(console_name))
    target.run(f"tmux kill-session -t {q}", check=False)


def _resolve_workspace_path(db: Database, session: SessionRow) -> str | None:
    ws = db.get_workspace(session.workspace_name)
    return ws.workspace_path if ws else None


def _split_shell_pane(
    target: ExecTarget,
    *,
    console_name: str,
    window_name: str,
    workspace_path: str,
    shell: ShellEntry,
    session_user: str,
    admin_user: str,
) -> None:
    """Split off one shell pane in an existing console window."""
    cwd = shell["cwd"]
    full_path = posixpath.join(workspace_path, cwd) if cwd else workspace_path
    q_full = shlex.quote(full_path)
    q_con = shlex.quote(tmux_session_name(console_name))
    q_win = shlex.quote(window_name)
    use_admin = shell["admin"] or session_user == admin_user

    # Login shell in both branches keeps profile/aliases consistent with the
    # session pane behavior (sessions use $SHELL -l via create_session).
    # Diagnostic on cd failure so a missing cwd shows the actual path.
    if use_admin:
        bootstrap = (
            f'cd {q_full} || echo "cwd missing: {full_path}"; '
            f'exec "$SHELL" -l'
        )
        cmd = f"tmux split-window -t {q_con}:{q_win} -c {q_full} {shlex.quote(bootstrap)}"
    else:
        q_user = shlex.quote(session_user)
        bootstrap = (
            f'cd {q_full} || echo "cwd missing: {full_path}"; '
            f'exec "$SHELL" -l'
        )
        pane_cmd = (
            f"exec sudo --login -u {q_user} bash -c {shlex.quote(bootstrap)}"
        )
        cmd = (
            f"tmux split-window -t {q_con}:{q_win} -c {q_full} {shlex.quote(pane_cmd)}"
        )

    res = target.run(cmd, check=False)
    if not res.ok:
        output.warn(
            f"failed to add shell pane in '{window_name}': {res.stderr.strip()}"
        )


def _add_session_window(
    target: ExecTarget,
    db: Database,
    *,
    console_name: str,
    member: ConsoleSessionRow,
    vm: VMRow,
) -> None:
    """Create one session window in the console and attach its shell panes.

    Missing or off-VM sessions are skipped with a warning; this keeps the
    console attach functional even if a session has been deleted out from
    under it.
    """
    session = db.get_session(member.session_name)
    if session is None:
        output.warn(
            f"session '{member.session_name}' is in console '{console_name}' "
            f"but no longer exists; skipping window"
        )
        return
    workspace_path = _resolve_workspace_path(db, session)
    if workspace_path is None:
        output.warn(
            f"workspace for session '{member.session_name}' is missing; "
            f"skipping window"
        )
        return

    q_con = shlex.quote(tmux_session_name(console_name))
    q_session = shlex.quote(session.name)
    wrapper = _attach_loop_wrapper(session.name, session.socket_path)

    res = target.run(
        f"tmux new-window -t {q_con} -n {q_session} {shlex.quote(wrapper)}",
        check=False,
    )
    if not res.ok:
        output.warn(
            f"failed to add window for '{session.name}': {res.stderr.strip()}"
        )
        return

    if not member.shells:
        return

    session_user = _session_linux_user(db, session, vm)
    for shell in member.shells:
        _split_shell_pane(
            target,
            console_name=console_name,
            window_name=session.name,
            workspace_path=workspace_path,
            shell=shell,
            session_user=session_user,
            admin_user=vm.admin_username,
        )
    target.run(
        f"tmux select-layout -t {q_con}:{q_session} tiled",
        check=False,
    )


def _build_console_tmux(
    target: ExecTarget,
    db: Database,
    console: ConsoleRow,
    vm: VMRow,
) -> None:
    """Kill any existing tmux session, then rebuild it from current DB state."""
    members = db.list_console_sessions(console.name)
    if not members and not console.admin_shell:
        # create_console rejects this; belt-and-suspenders for future caller paths.
        output.warn(f"console '{console.name}' has no members; skipping tmux build")
        return

    tmux_name = tmux_session_name(console.name)
    q_con = shlex.quote(tmux_name)

    _kill_console_tmux(target, console.name)

    if console.admin_shell:
        # Window 0 is the admin shell -- matches legacy vm-console behavior.
        target.run(
            f"tmux new-session -d -s {q_con} -n admin-shell "
            f"{shlex.quote('exec sudo su --login ' + shlex.quote(vm.admin_username))}"
        )
        placeholder_used = False
        placeholder = ""
    else:
        # tmux requires at least one window at all times. Create a transient
        # placeholder; '--' is forbidden by validate_name so it cannot collide
        # with any user-chosen session.
        placeholder = "aw--placeholder"
        target.run(f"tmux new-session -d -s {q_con} -n {shlex.quote(placeholder)}")
        placeholder_used = True

    if members:
        output.info(
            f"Adding {len(members)} session window(s) to console '{console.name}'..."
        )
    for member in members:
        _add_session_window(
            target,
            db,
            console_name=console.name,
            member=member,
            vm=vm,
        )

    if not placeholder_used:
        return

    # Drop the placeholder once at least one real session window is in.
    # If every member failed to attach (unusual), keep the placeholder so the
    # tmux session survives for investigation.
    result = target.run(f"tmux list-windows -t {q_con} -F '#W'", check=False)
    if not result.ok:
        output.warn(
            f"could not list windows in console '{console.name}' to confirm "
            f"placeholder cleanup ({result.stderr.strip() or 'transport error'}); "
            f"placeholder may persist until next --recreate"
        )
        return

    windows = [w.strip() for w in result.stdout.strip().splitlines() if w.strip()]
    if any(w != placeholder for w in windows):
        target.run(
            f"tmux kill-window -t {q_con}:{shlex.quote(placeholder)}",
            check=False,
        )
    else:
        output.warn(
            f"console '{console.name}' has no usable session windows; "
            f"placeholder kept so the tmux session survives"
        )


def _prepare_vm_target_for_attach(
    db: Database, config: Config, vm_name: str
) -> tuple[VMRow, ExecTarget]:
    """Ensure the VM is running (starting it if needed) and return (vm, target).

    Use this only for explicit user-driven attach flows where booting a stopped
    VM is acceptable. Raises on failure.
    """
    from agentworks.ssh import admin_exec_target
    from agentworks.workspaces.manager import _ensure_vm_running

    vm = db.get_vm(vm_name)
    if vm is None:
        raise output.VMError(f"VM '{vm_name}' not found")
    _ensure_vm_running(db, config, vm)
    if vm.tailscale_host is None:
        raise output.VMError(f"VM '{vm.name}' has no Tailscale address")
    return vm, admin_exec_target(vm, config)


def _live_target(
    db: Database, config: Config, vm_name: str
) -> tuple[VMRow, ExecTarget] | None:
    """Return (vm, target) for best-effort live sync without auto-starting the VM.

    Returns None if the VM record is missing or has no Tailscale address.
    The first SSH command will surface a transport error if the VM is offline;
    callers should wrap that in _live_best_effort.
    """
    from agentworks.ssh import admin_exec_target

    vm = db.get_vm(vm_name)
    if vm is None or vm.tailscale_host is None:
        return None
    return vm, admin_exec_target(vm, config)


@contextlib.contextmanager
def _live_best_effort(action: str) -> Iterator[None]:
    """Wrap best-effort live tmux work. User-facing AgentworksError exceptions
    propagate; transport-level surprises are warned and swallowed."""
    try:
        yield
    except output.AgentworksError:
        raise
    except Exception as exc:
        output.warn(f"live console sync failed ({action}): {exc}")


# -- High-level entrypoints ------------------------------------------------


def attach_console(
    db: Database,
    config: Config,
    *,
    name: str,
    recreate: bool = False,
    allow_nesting: bool = False,
) -> None:
    """Attach to a named console, building or rebuilding tmux state as needed."""
    from agentworks.ssh import interactive

    if os.environ.get("TMUX") and not allow_nesting:
        raise output.ConsoleError(
            "already inside a tmux session.\n"
            "Nesting is not recommended (prefix key conflicts, "
            "confusing detach behavior).\n"
            "Pass --allow-nesting to override."
        )

    console = _require_console(db, name)
    vm, target = _prepare_vm_target_for_attach(db, config, console.vm_name)

    exists = _console_tmux_exists(target, name)
    if recreate and exists:
        output.info(f"Rebuilding console '{name}' (--recreate)...")
        _build_console_tmux(target, db, console, vm)
    elif not exists:
        output.info(f"Building console '{name}' on first attach...")
        _build_console_tmux(target, db, console, vm)
    else:
        output.info(f"Attaching to running console '{name}'.")

    tmux_name = tmux_session_name(name)
    sys.exit(interactive(target, f"tmux attach -t {shlex.quote(tmux_name)}"))


def delete_console(
    db: Database,
    config: Config,
    *,
    name: str,
    yes: bool = False,
) -> None:
    """Delete a console: tear down its tmux session (best-effort), then DB row."""
    console = _require_console(db, name)
    if not yes and not output.confirm(f"Delete console '{name}'?"):
        raise output.UserAbort("delete cancelled")

    # Best-effort tmux teardown. Don't block the DB delete on VM reachability.
    teardown_failed = False
    try:
        live = _live_target(db, config, console.vm_name)
        if live is not None:
            _vm, target = live
            _kill_console_tmux(target, name)
    except output.AgentworksError:
        raise
    except Exception as exc:
        teardown_failed = True
        output.warn(f"failed to tear down tmux session for '{name}': {exc}")

    db.delete_console(name)
    if teardown_failed:
        output.info(
            f"Console '{name}' removed from database. Any stale tmux session on "
            f"the VM will be replaced on next 'aw console attach'."
        )
    else:
        output.info(f"Console '{name}' deleted.")
