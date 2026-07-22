"""Live-tmux probing, the attach loop, and the high-level attach/delete/list
entrypoints for named consoles.

``kill_session_windows``, ``_console_tmux_exists``, ``_prepare_vm_target_for_attach``,
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
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.errors import (
    AgentworksError,
    NotFoundError,
    StateError,
    UserAbort,
)
from agentworks.resources.access import named_console_template
from agentworks.sessions.tmux import tmux_cmd
from agentworks.vms.manager import gated_vm_boundary

from ._helpers import _require_console, _shell_summary, tmux_session_name
from .tmux_build import _build_console_tmux

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


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
    q = shlex.quote(session_name)
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
    echo 'Waiting for session to restart...'
    while ! {has} 2>/dev/null; do sleep 2; done
done
"""


def _console_tmux_exists(target: Transport, console_name: str) -> bool:
    q = shlex.quote(tmux_session_name(console_name))
    return target.run(f"tmux has-session -t {q} 2>/dev/null", check=False).ok


def _kill_console_tmux(target: Transport, console_name: str) -> None:
    q = shlex.quote(tmux_session_name(console_name))
    target.run(f"tmux kill-session -t {q}", check=False)


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
            if not _mc._console_tmux_exists(target, console_name):
                continue
            q_con = shlex.quote(tmux_session_name(console_name))
            for session_name in session_names:
                target.run(
                    f"tmux kill-window -t {q_con}:{shlex.quote(session_name)}",
                    check=False,
                )
    except AgentworksError:
        raise
    except Exception as exc:
        affected = sorted({c for c, _ in pairs})
        recovery = "; ".join(f"agw console attach {shlex.quote(c)} --recreate" for c in affected)
        output.warn(f"live console window cleanup failed: {exc}. Stale windows may persist; rebuild with: {recovery}")


@contextlib.contextmanager
def _prepare_vm_target_for_attach(
    db: Database, config: Config, vm_name: str, *, registry: Registry
) -> Iterator[tuple[VMRow, Transport]]:
    """Ensure the VM is running (starting it if needed) and yield
    ``(vm, target)`` inside the activation gate's held-active span.

    Use this only for explicit user-driven attach flows where booting a
    stopped VM is acceptable. Raises on failure. Orchestrated
    (``vms.manager.gated_vm_boundary``): the gate replaces the
    imperative ``bind_platform`` + ``ensure_active`` pair (opening
    BEFORE the preflight sweep), and the span it yields within is the
    ``vm_active`` hold the callers used to open themselves, covering
    their SSH-heavy bodies and interactive attaches. The console is
    not a node: attaching provisions nothing, so the graph is the live
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
    with gated_vm_boundary(db, config, registry, vm):
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
    return vm, transport(vm, config)


@contextlib.contextmanager
def _live_best_effort(action: str, *, console_name: str) -> Iterator[None]:
    """Wrap best-effort live tmux work. User-facing AgentworksError exceptions
    propagate; transport-level surprises are warned and swallowed.

    The DB has already mutated by the time we reach here, so any partial
    live-tmux failure leaves DB and tmux out of sync until the operator
    reattaches with --recreate. The warning includes the actual console name
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
            f"DB state was updated; run `agw console attach {q_name} --recreate` "
            f"to rebuild tmux from the new state."
        )


# -- Read-side helpers ----------------------------------------------------


def list_consoles(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """Print a table of consoles, optionally filtered by VM, workspace, or agent.

    Workspace/agent filters match a console if any of its member sessions
    match; see `Database.list_consoles_with_counts` for full semantics.
    Filters compose with AND.

    With ``names_only=True``, emit one console name per line and skip
    the table render. Used by shell completion (see issue #147).
    """
    consoles = db.list_consoles_with_counts(
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
    )

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No consoles found" line below is
        # for human readers only.
        for c, _ in consoles:
            output.info(c.name)
        return

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
    """Print a console's configured membership and shell list.

    Output describes the DB-declared target state; live tmux state may
    differ (panes can be killed, layouts changed in tmux, etc.). The next
    `attach` / `attach --recreate` / `restore-session` reconciles live
    state back to what's shown here.
    """
    console = _require_console(db, name)
    members = db.list_console_sessions(name)

    output.info(f"Name:        {console.name}")
    output.info(f"VM:          {console.vm_name}")
    output.info(f"Admin shell: {'yes' if console.admin_shell else 'no'}")
    output.info(f"Created:     {console.created_at}")
    output.info(f"Updated:     {console.updated_at}")
    output.info("")
    output.info(f"Configured sessions: {len(members)}")

    if not members:
        return

    output.info("")
    for i, m in enumerate(members):
        output.info(f"[{i}] {m.session_name}  ({_shell_summary(m.shells)})")


# -- High-level entrypoints ------------------------------------------------


def attach_console(
    db: Database,
    config: Config,
    *,
    name: str,
    recreate: bool = False,
    allow_nesting: bool = False,
) -> int:
    """Attach to a named console, building or rebuilding tmux state as needed.

    Returns the interactive attach's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service).
    """
    if os.environ.get("TMUX") and not allow_nesting:
        raise StateError(
            "already inside a tmux session. Nesting is not recommended "
            "(prefix key conflicts, confusing detach behavior).",
            hint="Pass --allow-nesting to override.",
        )

    from agentworks.bootstrap import build_registry

    console = _require_console(db, name)
    registry = build_registry(config)
    # The gate's held-active span covers the build and the interactive
    # attach (the hold this caller used to open itself).
    with _mc._prepare_vm_target_for_attach(db, config, console.vm_name, registry=registry) as (vm, target):
        exists = _mc._console_tmux_exists(target, name)
        layout = named_console_template(registry).tmux_layout

        # Eager-prompting orchestration: the
        # build path opens new shells (admin shell + helper shell panes
        # per session window). Resolve every referenced secret BEFORE
        # _build_console_tmux issues the first tmux command. The plain
        # attach path (tmux session already exists) opens no new shells
        # so it skips eager-resolve: console attach joins existing
        # shells and consumes no secrets.
        # Conditional-need exception to the one-boundary-resolve
        # contract: whether a build is needed is only knowable from live
        # tmux state, post-boundary (the gate and its boundary resolve
        # already ran inside _prepare_vm_target_for_attach above). The
        # --recreate half of the guard IS knowable pre-boundary; it
        # deliberately shares this late resolve so both build paths stay
        # one code shape rather than forking the target computation
        # across the boundary.
        if recreate or not exists:
            from agentworks.secrets import resolve_for_command

            secret_values = resolve_for_command(
                _mc._console_build_secret_targets(db, registry, console=console, vm=vm),
                config,
                registry,
            )

        if recreate and exists:
            output.info(f"Rebuilding console '{name}' (--recreate)...")
            _build_console_tmux(
                target,
                db,
                registry,
                console,
                vm,
                values=secret_values,
                layout=layout,
            )
        elif not exists:
            output.info(f"Building console '{name}' on first attach...")
            _build_console_tmux(
                target,
                db,
                registry,
                console,
                vm,
                values=secret_values,
                layout=layout,
            )
        else:
            output.info(f"Attaching to running console '{name}'.")

        tmux_name = tmux_session_name(name)
        return target.interactive(f"tmux attach -t {shlex.quote(tmux_name)}")


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
            _kill_console_tmux(target, name)
    except AgentworksError:
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
        output.result(f"Console '{name}' deleted.")
