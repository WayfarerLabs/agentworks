"""Live-tmux probing and high-level lifecycle/list/describe entrypoints.

``kill_session_windows``, ``_console_tmux_presence``, ``_prepare_vm_target``,
and ``_live_target`` are monkeypatched by tests directly on the
``agentworks.sessions.multi_console`` package object (they intercept, e.g., the
live-sync path of ``crud.remove_sessions`` or ``restore.restore_session``
without a live VM). A patch on the package object only rebinds the package's
own attribute, not this module's global, so even the calls below from one
function in this file to another function in this same file go through the
package object at call time (``_mc.<name>(...)``) rather than a bare
reference, matching the calls made from other submodules for the same
reason.
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
from agentworks.name_filters import validate_name_filters
from agentworks.resources.access import named_console_template
from agentworks.sessions.tmux import ProbeStatus, tmux_cmd
from agentworks.vms.manager import gated_vm_boundary

from ._helpers import _require_console, _shell_summary, tmux_session_name, tmux_staging_name
from .tmux_build import _build_console_tmux

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, ShellEntry, VMRow
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.transports import Transport

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


def console_listing_data(listing: ConsoleListing) -> JsonObject:
    """Project console list facts into the closed JSON v1 shape."""
    return {
        "consoles": [
            {"name": console.name, "vm_name": console.vm_name, "session_count": console.session_count}
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
            "updated_at": description.updated_at,
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
    q = shlex.quote(f"={session_name}")
    has = tmux_cmd(f"has-session -t {q}", socket_path)
    att = tmux_cmd(f"attach -t {q}", socket_path)
    return f"""\
unset TMUX

# Entry: if the session isn't up yet, show a banner and wait for it.
if ! {has} 2>/dev/null; then
    clear
    echo 'Waiting for session {session_name} to come up...'
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
        echo 'Session {session_name} exited cleanly.'
    else
        echo "Session {session_name} exited (status $rc)."
    fi
    echo 'Waiting for session to start again...'
    while ! {has} 2>/dev/null; do sleep 2; done
done
"""


def _tmux_session_presence(target: Transport, tmux_name: str) -> ProbeStatus:
    """Probe an exact console tmux name without conflating failure and absence."""
    from agentworks.sessions.tmux import _tmux_presence_from_result

    q = shlex.quote(f"={tmux_name}")
    return _tmux_presence_from_result(
        target.run(f"tmux has-session -t {q}", check=False),
        missing_target_is_absent=True,
    )


def _console_tmux_presence(target: Transport, console_name: str) -> ProbeStatus:
    return _tmux_session_presence(target, tmux_session_name(console_name))


def _console_runtime_presence(target: Transport, console_name: str) -> tuple[ProbeStatus, ProbeStatus]:
    """Return canonical and staging presence, refusing no state implicitly."""
    return (
        _console_tmux_presence(target, console_name),
        _tmux_session_presence(target, tmux_staging_name(console_name)),
    )


def _teardown_console_tmux(target: Transport, console_name: str) -> None:
    """Remove and verify the canonical and staging console runtimes."""
    from agentworks.sessions.tmux import (
        ProbeStatus,
        _test_presence_from_result,
        probe_tmux_session_after_teardown,
    )

    names = (tmux_session_name(console_name), tmux_staging_name(console_name))
    initial = {name: _tmux_session_presence(target, name) for name in names}
    if any(presence is ProbeStatus.UNKNOWN for presence in initial.values()):
        raise StateError(
            f"could not determine console '{console_name}' tmux state",
            entity_kind="console",
            entity_name=console_name,
        )
    for tmux_name, presence in initial.items():
        if presence is ProbeStatus.ABSENT:
            continue
        q = shlex.quote(f"={tmux_name}")
        killed = target.run(f"tmux kill-session -t {q}", check=False)
        if _test_presence_from_result(killed) is ProbeStatus.UNKNOWN:
            raise StateError(
                f"could not remove console '{console_name}' tmux state",
                entity_kind="console",
                entity_name=console_name,
            )
    final = {name: probe_tmux_session_after_teardown(name, run_command=target.run, socket_path=None) for name in names}
    if any(presence is not ProbeStatus.ABSENT for presence in final.values()):
        raise StateError(
            f"failed to verify removal of console '{console_name}' tmux state",
            entity_kind="console",
            entity_name=console_name,
            hint="Inspect the canonical and staging tmux sessions before retrying.",
        )


def _teardown_console_staging(target: Transport, console_name: str) -> None:
    from agentworks.sessions.tmux import (
        ProbeStatus,
        _test_presence_from_result,
        probe_tmux_session_after_teardown,
    )

    staging_name = tmux_staging_name(console_name)
    presence = _tmux_session_presence(target, staging_name)
    if presence is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not determine staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )
    if presence is ProbeStatus.ABSENT:
        return
    q = shlex.quote(f"={staging_name}")
    killed = target.run(f"tmux kill-session -t {q}", check=False)
    if _test_presence_from_result(killed) is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not remove staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )
    if (
        probe_tmux_session_after_teardown(staging_name, run_command=target.run, socket_path=None)
        is not ProbeStatus.ABSENT
    ):
        raise StateError(
            f"failed to remove staging tmux state for console '{console_name}'",
            entity_kind="console",
            entity_name=console_name,
        )


def kill_session_windows(
    target: Transport,
    *,
    pairs: list[tuple[str, str]],
) -> None:
    """Best-effort: kill each ``(console_name, session_name)`` window in live tmux.

    Used by every code path that removes a session from a console
    (``session delete``, ``workspace delete --force``, ``agent delete --force``,
    ``console remove-sessions``). Pairs are grouped by console so we probe
    ``has-session`` once per console rather than once per pair. ``kill-window``
    runs with ``check=False`` so a console that's live but lacks the window
    (operator killed it manually) doesn't fail the cleanup.

    AgentworksError propagates; transport-level surprises are warned and
    swallowed because the DB has already settled by the time we reach here.
    """
    if not pairs:
        return
    by_console: dict[str, list[str]] = {}
    for con, sess in pairs:
        by_console.setdefault(con, []).append(sess)
    try:
        for console_name, session_names in by_console.items():
            presence = _mc._console_tmux_presence(target, console_name)
            if presence is ProbeStatus.UNKNOWN:
                raise StateError(
                    f"could not determine console '{console_name}' tmux state",
                    entity_kind="console",
                    entity_name=console_name,
                )
            if presence is ProbeStatus.ABSENT:
                continue
            q_con = shlex.quote(f"={tmux_session_name(console_name)}")
            for session_name in session_names:
                target.run(
                    f"tmux kill-window -t {q_con}:{shlex.quote(session_name)}",
                    check=False,
                )
    except AgentworksError:
        raise
    except Exception as exc:
        affected = sorted({c for c, _ in pairs})
        recovery = "; ".join(f"agw console restart {shlex.quote(c)}" for c in affected)
        output.warn(f"live console window cleanup failed: {exc}. Stale windows may persist; rebuild with: {recovery}")


@contextlib.contextmanager
def _prepare_vm_target(
    db: Database,
    config: Config,
    vm_name: str,
    *,
    registry: Registry,
    interaction: TtyInteractionPolicy,
) -> Iterator[tuple[VMRow, Transport]]:
    """Ensure the VM is running (starting it if needed) and yield
    ``(vm, target)`` inside the activation gate's held-active span.

    Use this for explicit user-driven console operations where booting a
    stopped VM is acceptable. Raises on failure. Orchestrated
    (``vms.manager.gated_vm_boundary``): the gate replaces the
    imperative ``bind_platform`` + ``ensure_active`` pair (opening
    BEFORE the preflight sweep), and the span it yields within is the
    ``vm_active`` hold the callers used to open themselves, covering
    their SSH-heavy bodies and interactive attaches. The console is
    not a node: these operations provision nothing, so the graph is the live
    VM alone, and no env-chain target registers (console build panes
    resolve their own targets on the documented conditional-need
    path).
    """
    from agentworks.transports import transport

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
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        yield vm, transport(vm, config)


def _live_target(db: Database, config: Config, vm_name: str) -> tuple[VMRow, Transport] | None:
    """Return (vm, target) for best-effort live sync without auto-starting the VM.

    Returns None if the VM record is missing or has no Tailscale address.
    The first SSH command will surface a transport error if the VM is offline;
    callers should wrap that in _live_best_effort.
    """
    from agentworks.transports import transport

    vm = db.get_vm(vm_name)
    if vm is None or vm.tailscale_host is None:
        return None
    from agentworks.vms.manager import require_vm_ssh_boundary

    require_vm_ssh_boundary(db, config, vm)
    return vm, transport(vm, config)


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
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
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
    consoles = db.list_consoles_with_counts(
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
    )

    return ConsoleListing(
        consoles=tuple(
            ConsoleListRow(name=console.name, vm_name=console.vm_name, session_count=session_count)
            for console, session_count in consoles
        )
    )


def render_console_listing(listing: ConsoleListing, *, names_only: bool = False) -> None:
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

    # Console names are freeform (cap 64) and display-only, but a very long one
    # would balloon this dynamically-sized column, so cap the NAME cell at a
    # bounded, table-friendly width and let short names size the column down.
    rows = [
        (output.truncate(console.name, _NAME_CELL_WIDTH), console.vm_name, str(console.session_count))
        for console in consoles
    ]
    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    vm_w = max(len("VM"), max(len(r[1]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'VM':<{vm_w}}  SESSIONS"
    output.info(header)
    output.info("-" * len(header))
    for n, vm, count in rows:
        output.info(f"{n:<{name_w}}  {vm:<{vm_w}}  {count}")


def console_description(db: Database, *, name: str) -> ConsoleDescription:
    """Collect a console's configured membership and shell facts.

    Output describes the DB-declared target state; live tmux state may
    differ (panes can be killed, layouts changed in tmux, etc.). The next
    `restart` / `restore-session` reconciles live
    state back to what's shown here.
    """
    console = _require_console(db, name)
    return ConsoleDescription(
        name=console.name,
        vm_name=console.vm_name,
        admin_shell=console.admin_shell,
        created_at=console.created_at,
        updated_at=console.updated_at,
        sessions=tuple(
            ConsoleMember(
                position=member.position,
                session_name=member.session_name,
                shells=tuple(ConsoleShell(cwd=shell["cwd"], admin=shell["admin"]) for shell in member.shells),
            )
            for member in db.list_console_sessions(name)
        ),
    )


def render_console_description(description: ConsoleDescription) -> None:
    """Render console detail facts with the legacy human layout."""

    output.info(f"Name:        {description.name}")
    output.info(f"VM:          {description.vm_name}")
    output.info(f"Admin shell: {'yes' if description.admin_shell else 'no'}")
    output.info(f"Created:     {description.created_at}")
    output.info(f"Updated:     {description.updated_at}")
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
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """Print the legacy console list presentation."""
    render_console_listing(
        console_listing(db, vm_name=vm_name, workspace_name=workspace_name, agent_name=agent_name),
        names_only=names_only,
    )


def describe_console(db: Database, *, name: str) -> None:
    """Print the legacy console detail presentation."""
    render_console_description(console_description(db, name=name))


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
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets import resolve_for_command

    console = _require_console(db, name)
    if not db.list_console_sessions(name) and not console.admin_shell:
        raise StateError(
            f"console '{name}' has no configured windows",
            entity_kind="console",
            entity_name=name,
            hint="Add a session or enable the admin shell before starting it.",
        )
    registry = load_request_registry(config, live_database=db)
    with _mc._prepare_vm_target(
        db,
        config,
        console.vm_name,
        registry=registry,
        interaction=interaction,
    ) as (vm, target):
        canonical, staging = _console_runtime_presence(target, name)
        if ProbeStatus.UNKNOWN in {canonical, staging}:
            raise StateError(
                f"could not determine console '{name}' tmux state",
                entity_kind="console",
                entity_name=name,
            )
        if not replace_running:
            if staging is ProbeStatus.PRESENT:
                _teardown_console_staging(target, name)
            if canonical is ProbeStatus.PRESENT:
                output.result(f"Console '{name}' is already running")
                return
        secret_values = resolve_for_command(
            _mc._console_build_secret_targets(db, registry, console=console, vm=vm),
            config,
            registry,
            allow_transient_auto_declare=True,
            interaction=interaction,
        )
        if replace_running:
            _teardown_console_tmux(target, name)
        _build_console_tmux(
            target,
            db,
            registry,
            console,
            vm,
            values=secret_values,
            layout=named_console_template(registry).tmux_layout,
        )
    output.result(f"Console '{name}' {'restarted' if replace_running else 'started'}")


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
    from agentworks.bootstrap import load_request_registry

    console = _require_console(db, name)
    registry = load_request_registry(config, live_database=db)
    with _mc._prepare_vm_target(
        db,
        config,
        console.vm_name,
        registry=registry,
        interaction=interaction,
    ) as (_vm, target):
        _teardown_console_tmux(target, name)
    output.result(f"Console '{name}' stopped")


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

    from agentworks.bootstrap import load_request_registry

    console = _require_console(db, name)
    registry = load_request_registry(config, live_database=db)
    # The gate's held-active span covers the build and the interactive
    # attach (the hold this caller used to open itself).
    with _mc._prepare_vm_target(
        db,
        config,
        console.vm_name,
        registry=registry,
        interaction=interaction,
    ) as (_vm, target):
        canonical, staging = _console_runtime_presence(target, name)
        if ProbeStatus.UNKNOWN in {canonical, staging}:
            raise StateError(
                f"could not determine console '{name}' tmux state",
                entity_kind="console",
                entity_name=name,
                hint="Retry after transport access is reliable.",
            )
        if canonical is not ProbeStatus.PRESENT or staging is not ProbeStatus.ABSENT:
            raise StateError(
                f"console '{name}' is not ready to attach",
                entity_kind="console",
                entity_name=name,
                hint=f"Run `agw console start {name}`.",
            )

        from agentworks.terminal import clear_screen_on_detach

        tmux_name = tmux_session_name(name)
        # A console attach is a full-screen tmux; clear the local screen on
        # detach where we don't trust the terminal to restore cleanly.
        return target.interactive(
            f"tmux attach -t {shlex.quote(f'={tmux_name}')}",
            clear_screen_on_exit=clear_screen_on_detach(config.terminal.clear_on_detach),
        )


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
        raise UserAbort("delete cancelled")

    # Best-effort tmux teardown. Don't block the DB delete on VM reachability.
    teardown_failed = False
    try:
        live = _mc._live_target(db, config, console.vm_name)
        if live is not None:
            _vm, target = live
            _teardown_console_tmux(target, name)
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

    for console_name in console_names:
        if db.list_console_sessions(console_name):
            continue
        cleanup_now_empty_resource(
            kind="console",
            name=console_name,
            created=False,
            # Route through the package object so tests that monkeypatch
            # ``multi_console.delete_console`` intercept the confirmed delete.
            delete=partial(_mc.delete_console, db, config, name=console_name, yes=True),
            manual_command=f"agw console delete {console_name}",
            yes=yes,
            empty_clause="has no configured sessions left",
            report_clause="now has no configured sessions",
        )
