"""Live-tmux probing and high-level lifecycle/list/describe entrypoints.

``kill_session_windows``, ``_console_tmux_presence``, and ``_live_access`` are
monkeypatched by tests directly on the ``agentworks.sessions.multi_console``
package object (they intercept, e.g., the live-sync path of
``crud.remove_sessions`` without a live VM). A patch on the package object only rebinds the package's
own attribute, not this module's global, so even the calls below from one
function in this file to another function in this same file go through the
package object at call time (``_mc.<name>(...)``) rather than a bare
reference, matching the calls made from other submodules for the same
reason.

The console's tmux server is the admin's, so every request here runs on the
admin's access.
"""

from __future__ import annotations

import contextlib
import os
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.errors import (
    AgentworksError,
    NotFoundError,
    StateError,
    UserAbort,
)
from agentworks.list_sorting import nullable_sort_value, sort_rows
from agentworks.name_filters import validate_name_filters
from agentworks.runtime_time import derive_uptime_seconds, format_uptime
from agentworks.sessions.tmux import ProbeStatus, exact_tmux_target, tmux_cmd

from ._helpers import _require_console, _shell_summary, tmux_session_name, tmux_staging_name
from ._status import ConsoleStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from agentworks.config import Config
    from agentworks.db import ConsoleSessionRow, Database, SessionRow, ShellEntry, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.manager import SessionTmuxServer

    from ._access import ConsoleAccess

# NAME-column truncation cap for ``console list``. Console names are freeform
# (validated at cap 64) and display-only, so this is a deliberately tighter,
# table-friendly bound rather than a mirror of the validation cap; a name
# beyond it truncates with an ellipsis in the list view only.
_NAME_CELL_WIDTH = 50


@dataclass(frozen=True)
class ConsoleListRow:
    name: str
    vm_name: str
    session_count: int
    status: str
    desired_runtime_state: str | None = None


@dataclass(frozen=True)
class ConsoleListing:
    consoles: tuple[ConsoleListRow, ...]


@dataclass(frozen=True)
class ConsoleShell:
    cwd: str | None
    admin: bool


@dataclass(frozen=True)
class ConsoleMember:
    position: int
    session_name: str
    shells: tuple[ConsoleShell, ...]


@dataclass(frozen=True)
class ConsoleDescription:
    name: str
    vm_name: str
    admin_shell: bool
    created_at: str
    updated_at: str
    sessions: tuple[ConsoleMember, ...]
    status: str
    desired_runtime_state: str | None = None
    last_started_at: str | None = None
    uptime_seconds: int | None = None


def console_listing_data(listing: ConsoleListing) -> JsonObject:
    """Project console list facts into the closed JSON v1 shape."""
    return {
        "consoles": [
            {
                "name": console.name,
                "vm_name": console.vm_name,
                "session_count": console.session_count,
                "status": console.status,
                "desired_runtime_state": console.desired_runtime_state,
            }
            for console in listing.consoles
        ],
    }


def console_description_data(description: ConsoleDescription) -> JsonObject:
    """Project console detail facts into the closed JSON v1 shape."""
    return {
        "console": {
            "name": description.name,
            "vm_name": description.vm_name,
            "admin_shell": description.admin_shell,
            "created_at": description.created_at,
            "last_started_at": description.last_started_at,
            "uptime_seconds": description.uptime_seconds,
            "updated_at": description.updated_at,
            "status": description.status,
            "desired_runtime_state": description.desired_runtime_state,
            "sessions": [
                {
                    "position": member.position,
                    "session_name": member.session_name,
                    "shells": [{"cwd": shell.cwd, "admin": shell.admin} for shell in member.shells],
                }
                for member in description.sessions
            ],
        },
    }


def _session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user that owns a session's tmux server."""
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' not found (referenced by session '{session.name}')",
                entity_kind="agent",
                entity_name=session.agent_name,
            )
        return agent.linux_user
    return vm.admin_username


def _pane_runs_as_admin(vm: VMRow, session_user: str, *, admin_shell: bool) -> bool:
    """Whether a console shell pane runs as the VM's admin rather than as its session's agent.

    ``session_user`` is the session's owner (``_session_linux_user``). A
    shell marked ``admin`` runs as the admin, and so does every shell on an
    admin-mode session, whose owner is the admin.
    """
    return admin_shell or session_user == vm.admin_username


def _attach_loop_wrapper(session_name: str, server: SessionTmuxServer | None) -> str:
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

    The admin's console pane runs the wrapper. ``server`` is the session's
    tmux server as ``resolve_session_tmux_server`` validated it, ``None`` for
    a legacy row on the admin's default server. An agent session's server is
    the agent's, ``server.owner``, and no other account is ever its client:
    each ``has-session`` and ``attach`` elevates only to switch to the agent
    behind the socket guard (``as_socket_owner_from_admin``), the attach on a
    relay terminal of its own. A poll the guard refuses reads as down, and
    the window says why once per refused stretch rather than on every poll.
    """
    from agentworks.sessions.tmux import SOCKET_GUARD_REFUSED, as_socket_owner_from_admin

    q = exact_tmux_target(session_name)
    socket_path = server.socket_path if server is not None else None
    # The probe needs no stdin or stdout; neither reaches the server's account.
    has = tmux_cmd(f"has-session -t {q}", socket_path)
    att = tmux_cmd(f"attach -t {q}", socket_path)
    if server is None or server.owner is None:
        probe = f"agw_up() {{ {has} </dev/null >/dev/null 2>&1; }}"
    else:
        has = as_socket_owner_from_admin(has, socket_path=server.socket_path, owner=server.owner)
        att = as_socket_owner_from_admin(att, socket_path=server.socket_path, owner=server.owner, terminal=True)
        reason = shlex.quote(
            f"Not attaching to session '{session_name}': its tmux socket is not a socket that '{server.owner}' owns."
        )
        probe = f"""\
agw_refused=
agw_up() {{
    {has} </dev/null >/dev/null 2>&1
    agw_rc=$?
    if [ "$agw_rc" -eq {SOCKET_GUARD_REFUSED} ]; then
        [ -n "$agw_refused" ] || echo {reason}
        agw_refused=1
    else
        agw_refused=
    fi
    return "$agw_rc"
}}"""
    return f"""\
unset TMUX
{probe}

# Entry: if the session isn't up yet, show a banner and wait for it.
clear
if ! agw_up; then
    echo 'Waiting for session {session_name} to come up...'
    while ! agw_up; do sleep 2; done
fi

# Main loop: attach; on exit, distinguish detach (re-attach silently) from
# session-end (print a one-line notice, keep terminal content, then wait).
while true; do
    clear
    {att}
    rc=$?
    if agw_up; then
        continue
    fi
    echo
    if [ "$rc" -eq 0 ]; then
        echo 'Session {session_name} exited cleanly.'
    else
        echo "Session {session_name} exited (status $rc)."
    fi
    echo 'Waiting for session to start again...'
    while ! agw_up; do sleep 2; done
done
"""


def _tmux_session_presence(admin: VMAccess, tmux_name: str) -> ProbeStatus:
    """Probe an exact console tmux name without conflating failure and absence.

    One dispatch within the observation deadline; a probe that does not
    complete raises its typed error rather than reading as absent.
    """
    from agentworks.sessions.tmux import _tmux_presence_from_result, access_run_command
    from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS

    probe = access_run_command(admin, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False)
    return _tmux_presence_from_result(
        probe(f"tmux has-session -t {exact_tmux_target(tmux_name)}", check=False),
        missing_target_is_absent=True,
    )


def _console_tmux_presence(admin: VMAccess, console_name: str) -> ProbeStatus:
    return _mc._tmux_session_presence(admin, tmux_session_name(console_name))


def _console_runtime_presence(admin: VMAccess, console_name: str) -> tuple[ProbeStatus, ProbeStatus]:
    """Return canonical and staging presence, refusing no state implicitly."""
    return (
        _mc._console_tmux_presence(admin, console_name),
        _mc._tmux_session_presence(admin, tmux_staging_name(console_name)),
    )


def _teardown_console_tmux(admin: VMAccess, console_name: str) -> None:
    """Remove and verify the canonical and staging console runtimes."""
    from agentworks.sessions.tmux import (
        _test_presence_from_result,
        access_run_command,
        probe_tmux_session_after_teardown,
    )

    run = access_run_command(admin, deadline=None, elevated=False)
    names = (tmux_session_name(console_name), tmux_staging_name(console_name))
    initial = {name: _tmux_session_presence(admin, name) for name in names}
    if any(presence is ProbeStatus.UNKNOWN for presence in initial.values()):
        raise StateError(
            f"could not determine console '{console_name}' tmux state",
            entity_kind="console",
            entity_name=console_name,
        )
    for tmux_name, presence in initial.items():
        if presence is ProbeStatus.ABSENT:
            continue
        killed = run(f"tmux kill-session -t {exact_tmux_target(tmux_name)}", check=False)
        if _test_presence_from_result(killed) is ProbeStatus.UNKNOWN:
            raise StateError(
                f"could not remove console '{console_name}' tmux state",
                entity_kind="console",
                entity_name=console_name,
            )
    final = {name: probe_tmux_session_after_teardown(name, run_command=run, socket_path=None) for name in names}
    if any(presence is not ProbeStatus.ABSENT for presence in final.values()):
        raise StateError(
            f"failed to verify removal of console '{console_name}' tmux state",
            entity_kind="console",
            entity_name=console_name,
            hint="Inspect the canonical and staging tmux sessions before retrying.",
        )


def _teardown_console_staging(admin: VMAccess, console_name: str) -> None:
    from agentworks.sessions.tmux import (
        _test_presence_from_result,
        access_run_command,
        probe_tmux_session_after_teardown,
    )

    run = access_run_command(admin, deadline=None, elevated=False)
    staging_name = tmux_staging_name(console_name)
    presence = _tmux_session_presence(admin, staging_name)
    if presence is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not determine staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )
    if presence is ProbeStatus.ABSENT:
        return
    killed = run(f"tmux kill-session -t {exact_tmux_target(staging_name)}", check=False)
    if _test_presence_from_result(killed) is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not remove staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )
    if probe_tmux_session_after_teardown(staging_name, run_command=run, socket_path=None) is not ProbeStatus.ABSENT:
        raise StateError(
            f"failed to remove staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )


def kill_session_windows(
    admin: VMAccess,
    *,
    pairs: list[tuple[str, str]],
) -> None:
    """Best-effort: kill each ``(console_name, session_name)`` window in live tmux, on the admin's access.

    Used by every code path that removes a session from a console
    (``session delete``, ``workspace delete --force``, ``agent delete --force``,
    ``console remove-sessions``). Pairs are grouped by console so we probe
    ``has-session`` once per console rather than once per pair
    (``_tmux_session_presence``). ``kill-window`` runs with
    ``check=False`` so a console that's live but lacks the window (operator
    killed it manually) doesn't fail the cleanup.

    AgentworksError propagates, a request that does not complete included;
    other surprises are warned and swallowed because the DB has already
    settled by the time we reach here.
    """
    from agentworks.sessions.tmux import access_run_command

    if not pairs:
        return
    by_console: dict[str, list[str]] = {}
    for con, sess in pairs:
        by_console.setdefault(con, []).append(sess)
    run = access_run_command(admin, deadline=None, elevated=False)
    try:
        for console_name, session_names in by_console.items():
            presence = _mc._console_tmux_presence(admin, console_name)
            if presence is ProbeStatus.UNKNOWN:
                raise StateError(
                    f"could not determine console '{console_name}' tmux state",
                    entity_kind="console",
                    entity_name=console_name,
                )
            if presence is ProbeStatus.ABSENT:
                continue
            q_con = exact_tmux_target(tmux_session_name(console_name))
            for session_name in session_names:
                run(f"tmux kill-window -t {q_con}:{shlex.quote(session_name)}", check=False)
    except AgentworksError:
        raise
    except Exception as exc:
        affected = sorted({c for c, _ in pairs})
        recovery = "; ".join(f"agw console restart {shlex.quote(c)}" for c in affected)
        output.warn(f"live console window cleanup failed: {exc}. Stale windows may persist; rebuild with: {recovery}")


@contextlib.contextmanager
def _prepare_console_access(
    db: Database,
    config: Config,
    vm_name: str,
    *,
    registry: Registry,
    interaction: TtyInteractionPolicy,
    held_native_guard: NativeMutationGuard,
    resolver: Resolver,
    targets: Callable[[VMRow], Sequence[SecretTarget]],
    members: Sequence[ConsoleSessionRow],
) -> Iterator[ConsoleAccess]:
    """Activate the VM, check the panes' agents, run the operation's one boundary resolve, and yield its accesses.

    The missing-VM and missing-address refusals come first, with nothing
    resolved. ``targets`` gives the operation's pane secret targets from the
    VM's row; they register on ``resolver`` before the activation gate opens.
    Once the VM is active, :func:`console_access` checks the direct SSH of
    every agent whose shell panes ``members`` configure, so a refused agent
    stops the operation before the resolve can prompt; then the one resolve
    runs, before the caller's first request, so every pane value is in
    ``resolver.values`` before anything changes.
    """
    import agentworks.execution as execution
    from agentworks.execution.access import Admin
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.vms.manager import require_vm_ssh_boundary
    from agentworks.vms.manager.runtime import activate_vm_runtime, prepare_vm_runtime

    from ._access import console_access
    from .lifecycle import preflight_console_vm

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(f"VM '{vm_name}' not found", entity_kind="vm", entity_name=vm_name)
    if vm.tailscale_host is None:
        raise StateError(f"VM '{vm.name}' has no Tailscale address", entity_kind="vm", entity_name=vm.name)
    context = None
    try:
        with native_mutation_guard(db.path, vm_name, held=held_native_guard) as guard:
            require_vm_ssh_boundary(db, config, vm)
            node = preflight_console_vm(
                db,
                config,
                registry,
                vm,
                held_native_guard=guard,
                interaction=interaction,
            )
            resolver.register_targets(targets(vm), allow_transient_auto_declare=True)
            context = prepare_vm_runtime(
                db,
                config,
                registry,
                vm,
                resolve_secret=resolver.resolve_gate,
                held_native_guard=guard,
                vm_node=node,
            )
            activate_vm_runtime(context)
            with context.node.hold_active():
                access = console_access(db, config, vm, execution.vm_access(vm, config, Admin()), members)
                resolver.resolve()
                yield access
    finally:
        if context is not None:
            context.clear_values()


def _live_access(db: Database, config: Config, vm_name: str) -> tuple[VMRow, VMAccess] | None:
    """Return (vm, the admin's access) for best-effort live sync without auto-starting the VM.

    Returns None if the VM record is missing or has no Tailscale address.
    The first request will surface a connection error if the VM is offline;
    callers should wrap that in _live_best_effort.
    """
    import agentworks.execution as execution
    from agentworks.execution.access import Admin
    from agentworks.vms.manager import require_vm_ssh_boundary

    vm = db.get_vm(vm_name)
    if vm is None or vm.tailscale_host is None:
        return None
    require_vm_ssh_boundary(db, config, vm)
    return vm, execution.vm_access(vm, config, Admin())


@contextlib.contextmanager
def _live_best_effort(action: str, *, console_name: str) -> Iterator[None]:
    """Wrap best-effort live tmux work. User-facing AgentworksError exceptions
    propagate; transport-level surprises are warned and swallowed.

    The DB has already mutated by the time we reach here, so any partial
    live-tmux failure leaves DB and tmux out of sync until the operator
    runs ``console restart``. The warning includes the actual console name
    so the suggested recovery command can be copy/pasted as-is.
    """
    try:
        yield
    except AgentworksError:
        raise
    except Exception as exc:
        q_name = shlex.quote(console_name)
        output.warn(
            f"live console sync failed ({action}): {exc}. "
            f"DB state was updated; run `agw console restart {q_name}` "
            f"to rebuild tmux from the new state."
        )


# -- Read-side helpers ----------------------------------------------------


def console_listing(
    db: Database,
    config: Config | None = None,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    include_status: bool = False,
    sort_keys: tuple[str, ...] | None = None,
) -> ConsoleListing:
    """Collect ordered console list facts, optionally filtered by DB relationships.

    Workspace/agent filters match a console if any of its member sessions
    match; see `Database.list_consoles_with_counts` for full semantics.
    Filters compose with AND.

    An unknown name in any filter raises ``NotFoundError`` rather than
    matching nothing (issue #304).
    """
    validate_name_filters(
        db,
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
    )
    consoles = sort_rows(
        db.list_consoles_with_counts(
            vm_name=vm_name,
            workspace_name=workspace_name,
            agent_name=agent_name,
        ),
        sort_keys=sort_keys,
        key_functions={
            "alpha": lambda item: (item[0].name,),
            "creation": lambda item: nullable_sort_value(item[0].created_at),
            "vm": lambda item: nullable_sort_value(item[0].vm_name),
        },
        entity_kind="console",
    )
    statuses: dict[str, ConsoleStatus] = (
        {console.name: ConsoleStatus.UNKNOWN for console, _count in consoles} if include_status else {}
    )
    if include_status and consoles:
        if config is None:
            raise StateError("console status observation requires config")
        observable = [console for console, _count in consoles if db.get_vm(console.vm_name) is not None]
        output.info(
            f"Checking status for {output.count(len(consoles), 'console')} "
            f"across {output.count(len({console.vm_name for console in observable}), 'VM')}..."
        )
        if observable:
            statuses.update(_mc.observe_console_statuses(db, config, observable))

    return ConsoleListing(
        consoles=tuple(
            ConsoleListRow(
                name=console.name,
                vm_name=console.vm_name,
                session_count=session_count,
                desired_runtime_state=console.desired_runtime_state,
                status=(statuses.get(console.name, ConsoleStatus.UNKNOWN).value if include_status else "unavailable"),
            )
            for console, session_count in consoles
        )
    )


def render_console_listing(
    listing: ConsoleListing,
    *,
    names_only: bool = False,
    include_status: bool = False,
) -> None:
    """Render console list facts with the legacy human layout."""
    consoles = listing.consoles
    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No consoles found" line below is
        # for human readers only.
        for console in consoles:
            output.info(console.name)
        return

    if not consoles:
        output.info("No consoles found.")
        return

    # Keep the list-view name cap while the shared renderer sizes short rows down.
    rows = [
        (
            console.name,
            console.vm_name,
            str(console.session_count),
            console.desired_runtime_state or "adoption-required",
            console.status,
        )
        for console in consoles
    ]
    headers = ["NAME", "VM", "SESSIONS", "TARGET"]
    if include_status:
        headers.append("STATUS")
    projected_rows = [row if include_status else row[:-1] for row in rows]
    for line in output.render_table(headers, projected_rows, max_col_width=_NAME_CELL_WIDTH):
        output.info(line)
    if include_status:
        unknown_by_vm: dict[str, list[str]] = {}
        for console in consoles:
            if console.status == ConsoleStatus.UNKNOWN.value:
                unknown_by_vm.setdefault(console.vm_name, []).append(console.name)
        if unknown_by_vm:
            output.info("")
            groups = "; ".join(f"{vm}: {', '.join(names)}" for vm, names in unknown_by_vm.items())
            output.warn(f"Console status is unknown by VM: {groups}.")


def console_description(
    db: Database,
    config: Config,
    *,
    name: str,
) -> ConsoleDescription:
    """Collect configured console facts plus non-activating live status."""
    console = _require_console(db, name)
    members = tuple(
        ConsoleMember(
            position=member.position,
            session_name=member.session_name,
            shells=tuple(ConsoleShell(cwd=shell["cwd"], admin=shell["admin"]) for shell in member.shells),
        )
        for member in db.list_console_sessions(name)
    )
    output.info(f"Checking console '{name}' runtime...")
    status = _mc.observe_console_statuses(db, config, [console])[console.name]
    if status is ConsoleStatus.UNKNOWN:
        output.warn(f"Console '{name}' runtime status could not be determined.")
    return ConsoleDescription(
        name=console.name,
        vm_name=console.vm_name,
        admin_shell=console.admin_shell,
        created_at=console.created_at,
        updated_at=console.updated_at,
        sessions=members,
        status=status.value,
        desired_runtime_state=console.desired_runtime_state,
        last_started_at=console.last_started_at,
        uptime_seconds=derive_uptime_seconds(
            console.last_started_at,
            running=status is ConsoleStatus.RUNNING,
            entity_kind="console",
            entity_name=console.name,
        ),
    )


def render_console_description(description: ConsoleDescription) -> None:
    """Render console detail facts with the legacy human layout."""

    output.info(f"Name:           {description.name}")
    output.info(f"VM:             {description.vm_name}")
    output.info(f"Admin shell:    {'yes' if description.admin_shell else 'no'}")
    output.info(f"Created:        {description.created_at}")
    output.info(f"Last Started:   {description.last_started_at or 'unknown'}")
    output.info(
        f"Uptime:         "
        f"{format_uptime(description.uptime_seconds, running=description.status == ConsoleStatus.RUNNING.value)}"
    )
    output.info(f"Updated:        {description.updated_at}")
    output.info(f"Target:         {description.desired_runtime_state or 'adoption-required'}")
    output.info(f"Status:         {description.status}")
    output.info("")
    output.info(f"Configured sessions: {len(description.sessions)}")

    if not description.sessions:
        return

    output.info("")
    for index, member in enumerate(description.sessions):
        shells = cast(
            "list[ShellEntry]",
            [{"cwd": shell.cwd, "admin": shell.admin} for shell in member.shells],
        )
        output.info(f"[{index}] {member.session_name}  ({_shell_summary(shells)})")


def list_consoles(
    db: Database,
    config: Config | None = None,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    names_only: bool = False,
    include_status: bool = False,
    sort_keys: tuple[str, ...] | None = None,
) -> None:
    """Print the legacy console list presentation."""
    render_console_listing(
        console_listing(
            db,
            config,
            vm_name=vm_name,
            workspace_name=workspace_name,
            agent_name=agent_name,
            include_status=include_status,
            sort_keys=sort_keys,
        ),
        names_only=names_only,
        include_status=include_status,
    )


def describe_console(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Print the legacy console detail presentation."""
    render_console_description(console_description(db, config, name=name))


# -- High-level entrypoints ------------------------------------------------


def _realize_console(
    db: Database,
    config: Config,
    *,
    name: str,
    replace_running: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start a console, replacing its runtime only when requested."""
    from .lifecycle import realize_console

    realize_console(db, config, name=name, replace_running=replace_running, interaction=interaction)


def start_console(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> None:
    _realize_console(db, config, name=name, replace_running=False, interaction=interaction)


def restart_console(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> None:
    _realize_console(db, config, name=name, replace_running=True, interaction=interaction)


def stop_console(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> None:
    from .lifecycle import stop_console_runtime

    stop_console_runtime(db, config, name=name, interaction=interaction)


def refuse_console_nesting(*, allow_nesting: bool) -> None:
    """Refuse a nested console operation before any lifecycle mutation."""
    if os.environ.get("TMUX") and not allow_nesting:
        raise StateError(
            "already inside a tmux session. Nesting is not recommended "
            "(prefix key conflicts, confusing detach behavior).",
            hint="Pass --allow-nesting to override.",
        )


def attach_console(
    db: Database,
    config: Config,
    *,
    name: str,
    allow_nesting: bool = False,
    interaction: TtyInteractionPolicy,
) -> int:
    """Attach to an already-running named console.

    Returns the interactive attach's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service).
    """
    refuse_console_nesting(allow_nesting=allow_nesting)

    from .lifecycle import attach_running_console

    return attach_running_console(db, config, name=name, interaction=interaction)


def delete_console(
    db: Database,
    config: Config,
    *,
    name: str,
    yes: bool = False,
    held_native_guard: NativeMutationGuard | None = None,
) -> None:
    """Delete a console: tear down its tmux session (best-effort), then DB row."""
    from ._helpers import guarded_console_selection

    with guarded_console_selection(db, name, held_native_guard=held_native_guard) as (console, _guard):
        console = _require_console(db, name)
        if not yes and not output.confirm(f"Delete console '{name}'?"):
            raise UserAbort("delete cancelled")

        # Best-effort tmux teardown. Don't block the DB delete on VM reachability.
        teardown_failed = False
        try:
            live = _mc._live_access(db, config, console.vm_name)
            if live is not None:
                _vm, admin = live
                _teardown_console_tmux(admin, name)
        except UserAbort:
            raise
        except Exception as exc:
            teardown_failed = True
            output.warn(f"failed to tear down tmux session for '{name}': {exc}")

        db.delete_console(name)
        if teardown_failed:
            output.info(
                f"Console '{name}' removed from database. Inspect the VM for stale "
                f"'{tmux_session_name(name)}' or '{tmux_staging_name(name)}' tmux sessions."
            )
        else:
            output.result(f"Console '{name}' deleted.")


def offer_delete_if_empty_consoles(
    db: Database,
    config: Config,
    console_names: Iterable[str],
    *,
    yes: bool,
    held_native_guard: NativeMutationGuard | None = None,
) -> None:
    """For each named console now left with no configured sessions, offer to
    delete it (interactive) or report-but-keep it (--yes / non-interactive).

    An empty console cannot be started. This is the shared "console is now empty"
    treatment both ``session delete`` (via the FK cascade, issue #248/#261) and
    ``console remove-sessions`` (issue #265) reach after removing a session's
    console membership; both route here so the two paths stay byte-identical.

    ``console_names`` is the set of candidate consoles (each caller passes the
    consoles it just touched); a console is only acted on when
    ``list_console_sessions`` reports it empty, the same predicate
    ``console describe`` uses for its "Configured sessions" count. Emptiness is
    read live from the DB here, so callers may pass consoles that still have
    members and they are skipped.

    The offer/auto-delete/report policy (including the TTY gate that keeps a
    scripted caller at exit 0 instead of EOF-aborting) is owned by
    ``cleanup_now_empty_resource``; this wrapper only carries the console
    parametrization. A console is operator-authored and never created by the
    calling command, so ``created=False``: it is never auto-deleted under
    ``--yes``.
    """
    from functools import partial

    from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource

    from ._helpers import guarded_console_selection

    for console_name in console_names:
        if db.get_console(console_name) is None:
            continue
        with guarded_console_selection(db, console_name, held_native_guard=held_native_guard) as (_console, guard):
            if db.list_console_sessions(console_name):
                continue
            cleanup_now_empty_resource(
                kind="console",
                name=console_name,
                created=False,
                # Route through the package object so tests that monkeypatch
                # ``multi_console.delete_console`` intercept the confirmed delete.
                delete=partial(_mc.delete_console, db, config, name=console_name, yes=True, held_native_guard=guard),
                manual_command=f"agw console delete {console_name}",
                yes=yes,
                empty_clause="has no configured sessions left",
                report_clause="now has no configured sessions",
            )
