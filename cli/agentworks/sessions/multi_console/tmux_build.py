"""Tmux-side mechanics for building and repairing a console's live session:
splitting a tagged shell pane, adding a session window, and the from-scratch
console rebuild.

``attach.py`` imports ``_build_console_tmux`` from this module at load time
(the attach entrypoint needs it), so this module keeps its own references
back into ``attach`` (``_attach_loop_wrapper``, ``_session_linux_user``)
as function-local imports rather than module-level ones, to avoid a circular
import between the two modules.

Every request runs on the admin's access (``ConsoleAccess.admin``), since the
console's tmux server is the admin's; a shell pane parks its environment on
its own identity's access (``ConsoleAccess.pane``).

``_resolve_pane_env`` (defined in ``secrets_env``) is also monkeypatched by
tests directly on the ``agentworks.sessions.multi_console`` package object,
so the one call site below goes through the package object at call time
(``_mc._resolve_pane_env(...)``) rather than a direct reference, matching the
pattern used across the rest of the package for patched names.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.errors import AgentworksError, ExternalError, StateError, UserAbort
from agentworks.sessions.tmux import exact_tmux_target

from ._helpers import ADMIN_SHELL_WINDOW, tmux_session_name, tmux_staging_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agentworks.db import ConsoleRow, ConsoleSessionRow, Database, SessionRow, ShellEntry, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.execution.requests import LoginShell
    from agentworks.resources.registry import Registry
    from agentworks.sessions.tmux import RunCommand

    from ._access import ConsoleAccess


def _resolve_workspace_path(db: Database, session: SessionRow) -> str | None:
    ws = db.get_workspace(session.workspace_name)
    return ws.workspace_path if ws else None


@dataclass(frozen=True)
class SessionWindowBuild:
    """Outcome of building one session window in a console.

    ``built`` is the structural result: True when the window itself was
    created (``tmux new-window`` succeeded and the session, workspace, and
    agent were all resolvable), False when the whole window was skipped with a
    warning. ``failed_shells`` lists the configured shell indices whose panes
    could not be split or tagged; it is only meaningful when ``built`` is True,
    since a skipped window never reaches its shells.

    Two kinds of caller read this differently:

    - The staged whole-console builder (``_build_console_tmux``) rejects a
      partial build so it cannot publish incomplete state. Live
      ``add_sessions`` remains best effort after the durable membership update.
    - ``restore_session``, rebuilding a single missing window on the operator's
      behalf, inspects both fields and raises ``ExternalError`` if the window
      was skipped or any shell failed, so a partial rebuild is loud rather than
      a silent exit-0 that would require a full console restart.
    """

    built: bool
    failed_shells: list[int] = field(default_factory=list)


class _PaneNotCreated(Exception):
    """A split tmux refused, or whose pane never collected its request: the pane has no shell."""


def _split_parked(
    run: RunCommand,
    parking: VMAccess,
    request: LoginShell,
    *,
    window: str,
    switch_user: bool,
    redactions: Iterable[str],
) -> str:
    """Park ``request`` on ``parking``, split ``window`` with a pane that collects it, and return tmux's output.

    The split runs inside the parking block, so a split tmux refuses lets the
    parked request go at once; one whose pane does not collect the request
    within the handover's wait started no shell.
    """
    from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

    with parking.park(request) as handover:
        command = handover.command(switch_user=switch_user)
        res = run(f"tmux split-window -t {window} -P -F '#{{pane_id}}' {shlex.quote(command)}", check=False)
        if not res.ok:
            raise _PaneNotCreated(sanitize_setup_diagnostic(res.stderr.strip(), redactions=redactions))
    if not handover.collected:
        raise _PaneNotCreated("the pane did not collect its shell's request")
    return res.stdout


def _split_shell_pane(
    access: ConsoleAccess,
    db: Database,
    registry: Registry,
    *,
    values: Mapping[str, str],
    console_name: str,
    window_name: str,
    workspace_path: str,
    shell: ShellEntry,
    session: SessionRow,
    vm: VMRow,
    session_user: str,
    admin_user: str,
    config_index: int,
    tmux_name: str | None = None,
) -> str | None:
    """Split off one shell pane in an existing console window and tag the new
    pane with its position in the configured shell list. The tag lets
    restore-session detect which specific shell (out of an ordered list) is
    missing after an accidental kill.

    The pane's environment reaches it by the terminal handover: its login
    shell, with ``pane_env`` and the working directory, is parked on the pane
    identity's own access (the admin's, or the agent's, which
    ``console_access`` checked), and the pane's command is the handover's,
    which collects it. An agent's pane in the admin's tmux server switches to
    the agent with ``sudo -n -u`` first, so the environment never crosses
    sudo and no value reaches a tmux argument or the SSH command line. A
    working directory that cannot be entered warns and opens the shell in
    the home directory instead.

    Returns the new pane id on full success (split, collection and tag all
    completed), or None with a warning when the pane could not be created,
    did not start its shell, or could not be tagged. A request that does not
    complete counts as such a failure; a trust or readiness error, or a
    refused identity, propagates, since every later pane would fail the same
    way. `add_shell` ignores the return value (best-effort).
    `_add_session_window` records which config indices came back None in its
    `SessionWindowBuild.failed_shells`, and `restore_session` checks each
    return so a partial restore is loud rather than a silent exit-0."""
    from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, RequestRefused
    from agentworks.execution.requests import LoginShell
    from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic, sanitize_setup_error
    from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
    from agentworks.sessions.tmux import access_run_command
    from agentworks.vms.initializer.steps import describe_failure

    cwd = shell["cwd"]
    full_path = posixpath.join(workspace_path, cwd) if cwd else workspace_path
    q_con = exact_tmux_target(tmux_name or tmux_session_name(console_name))
    window = f"{q_con}:{shlex.quote(window_name)}"
    use_admin = shell["admin"] or session_user == admin_user

    pane_env = _mc._resolve_pane_env(
        db,
        registry,
        values=values,
        vm=vm,
        session=session,
        pane_user=admin_user if use_admin else session_user,
        is_admin_pane=use_admin,
    )
    run = access_run_command(access.admin, deadline=None, elevated=False)
    parking = access.pane(None if use_admin else session_user)

    def redact(text: str) -> str:
        return sanitize_setup_diagnostic(text, redactions=values.values())

    def split(request: LoginShell) -> str:
        return _split_parked(
            run, parking, request, window=window, switch_user=not use_admin, redactions=values.values()
        )

    try:
        try:
            out = split(LoginShell(cwd=full_path, env=pane_env))
        except RequestRefused as refused:
            if refused.code != "cwd":
                raise
            output.warn(f"cannot enter {full_path} for a shell pane in '{window_name}'; it opens in the home directory")
            out = split(LoginShell(env=pane_env))
    except _PaneNotCreated as failure:
        output.warn(f"failed to add shell pane in '{window_name}': {failure}")
        return None
    except (ExecutionIncomplete, CommandFailed, RequestRefused) as error:
        sanitize_setup_error(error, redactions=values.values())
        output.warn(f"failed to add shell pane in '{window_name}': {describe_failure(error, redact)}")
        return None

    q_console = shlex.quote(console_name)
    pane_id = out.strip()
    if not pane_id:
        # tmux is supposed to print the new pane id on stdout under `-P -F`;
        # an empty stdout means we lost the handle and can't tag the pane.
        # The pane is live but invisible to restore-session, which will
        # later refuse to repair this window (untagged-pane strict check).
        output.warn(
            f"added shell pane in '{window_name}' but couldn't capture its id; "
            f"the pane is untagged. restore-session won't be able to repair "
            f"this window; use `agw console restart {q_console}` "
            f"if you need clean tag state."
        )
        return None
    q_pane = shlex.quote(pane_id)
    try:
        tag_res = run(f"tmux set-option -p -t {q_pane} {SHELL_INDEX_OPTION} {config_index}", check=False)
        tag_failure = None if tag_res.ok else redact(tag_res.stderr.strip()) or "tmux refused set-option"
    except ExecutionIncomplete as error:
        tag_failure = describe_failure(error, redact)
    if tag_failure is not None:
        # The split happened (the pane is live) but tagging failed. Treat this
        # as a failure: callers like restore_session must know the pane is
        # untagged so the operator gets a loud signal rather than a future
        # untagged-pane error on the next restore-session.
        output.warn(
            f"added shell pane in '{window_name}' but tagging failed "
            f"({tag_failure}); "
            f"the pane is untagged. Use `agw console restart "
            f"{q_console}` to rebuild and retag from scratch."
        )
        return None
    return pane_id


def _last_window_index(run: RunCommand, q_con: str) -> int | None:
    """Return the highest live window index in the console, or None if the
    listing failed or produced nothing parseable.

    Lets a caller target ``tmux new-window`` at the slot just past the last
    window so the new window lands at the tail rather than filling a reclaimed
    low slot. That matters under the stock ``renumber-windows off``: a console
    whose transient placeholder (or a killed window) freed a low index would
    otherwise pull a bare ``new-window`` to the front (see the ``place_last``
    branch of ``_add_session_window``). Best-effort like everything else here:
    a failed listing returns None and the caller falls back to plain
    ``new-window``.
    """
    res = run(f"tmux list-windows -t {q_con} -F '#{{window_index}}'", check=False)
    if not res.ok:
        return None
    indices: list[int] = []
    for line in res.stdout.strip().splitlines():
        try:
            indices.append(int(line.strip()))
        except ValueError:
            continue
    return max(indices) if indices else None


def _add_session_window(
    access: ConsoleAccess,
    db: Database,
    registry: Registry,
    *,
    values: Mapping[str, str],
    console_name: str,
    member: ConsoleSessionRow,
    vm: VMRow,
    layout: str,
    place_last: bool = False,
    tmux_name: str | None = None,
) -> SessionWindowBuild:
    """Create one session window in the console and attach its shell panes.

    Missing or off-VM sessions are skipped with a warning; live membership
    synchronization can therefore proceed around a session deleted out from
    under the console.

    ``place_last`` controls where the new window lands. By default ``tmux
    new-window`` fills the lowest free window index, which is what the build
    path wants (it appends behind a placeholder, in order) and what
    ``restore_session`` wants (it refills the killed window's own vacated slot).
    ``add_sessions`` appends a brand-new member, so it passes ``place_last=True``
    to target an explicit index one past the current last window; otherwise,
    under the stock ``renumber-windows off``, a reclaimed low slot (e.g. the
    retired placeholder's index 0) would pull the new window in front of every
    existing session window. ``add_sessions`` also runs ``_reorder_session_windows``
    afterward, so final order is settled deterministically either way; the
    explicit index keeps the window at the tail even if that best-effort
    reorder cannot run.

    Returns a `SessionWindowBuild`. `built` is True when the window itself was
    created, False when the whole window was skipped with a warning (no session
    row, no workspace, `tmux new-window` failed, or a missing agent).
    `failed_shells` lists the configured shell indices whose panes failed to
    split or tag (best-effort per pane; `_split_shell_pane` warns on each).

    The staged whole-console builder and `restore_session` inspect both fields
    so a skipped build or failed shell cannot be published or reported as
    success. Live `add_sessions` remains best effort after its durable update.
    """
    from agentworks.sessions.multi_console_layout import _apply_layout, _focus_session_pane
    from agentworks.sessions.tmux import access_run_command

    # Lazy import: attach.py imports _build_console_tmux from this module at
    # load time, so a module-level import back here would be circular.
    from .attach import _attach_loop_wrapper, _session_linux_user

    session = db.get_session(member.session_name)
    if session is None:
        output.warn(
            f"session '{member.session_name}' is in console '{console_name}' but no longer exists; skipping window"
        )
        return SessionWindowBuild(built=False)
    workspace_path = _resolve_workspace_path(db, session)
    if workspace_path is None:
        output.warn(f"workspace for session '{member.session_name}' is missing; skipping window")
        return SessionWindowBuild(built=False)

    from agentworks.sessions.manager import resolve_session_tmux_server

    try:
        server = resolve_session_tmux_server(db, session)
    except StateError as exc:
        output.warn(f"session '{session.name}' has no valid tmux socket ({exc}); skipping window")
        return SessionWindowBuild(built=False)

    run = access_run_command(access.admin, deadline=None, elevated=False)
    runtime_name = tmux_name or tmux_session_name(console_name)
    q_con = exact_tmux_target(runtime_name)
    q_session = shlex.quote(session.name)
    wrapper = _attach_loop_wrapper(session.name, server)

    # When appending a new member, target an explicit index one past the last
    # window so it lands at the tail rather than in a reclaimed low slot (see
    # the place_last docstring). A failed listing falls back to plain
    # new-window; the caller's reorder pass still settles order.
    new_window_target = q_con
    if place_last:
        last_index = _last_window_index(run, q_con)
        if last_index is not None:
            new_window_target = f"{q_con}:{last_index + 1}"

    # -P -F '#{pane_index}' makes new-window print the created session pane's
    # index. It is the window's lowest live pane index (0 normally, 1 under an
    # inherited `pane-base-index 1`) and is what _focus_session_pane targets;
    # capturing it here avoids a second round trip and keeps focus correct
    # under either base index.
    res = run(
        f"tmux new-window -t {new_window_target} -n {q_session} -P -F '#{{pane_index}}' {shlex.quote(wrapper)}",
        check=False,
    )
    if not res.ok:
        output.warn(f"failed to add window for '{session.name}': {res.stderr.strip()}")
        return SessionWindowBuild(built=False)
    try:
        base_pidx = int(res.stdout.strip())
    except ValueError:
        if tmux_name is not None:
            output.warn(f"failed to read the pane index for '{session.name}'")
            return SessionWindowBuild(built=False)
        base_pidx = 0

    failed_shells: list[int] = []
    if member.shells:
        # _session_linux_user raises NotFoundError if the session points at an
        # agent row that's gone (FK violation under PRAGMA foreign_keys = OFF,
        # or stale state from a migration). Match the missing-session /
        # missing-workspace handling above: warn and skip rather than abort
        # the whole console build.
        from agentworks.errors import NotFoundError

        try:
            session_user = _session_linux_user(db, session, vm)
        except NotFoundError as exc:
            output.warn(f"agent for session '{session.name}' is missing ({exc}); skipping shell panes for this window")
            return SessionWindowBuild(built=False)
        for config_index, shell in enumerate(member.shells):
            pane_id = _split_shell_pane(
                access,
                db,
                registry,
                values=values,
                console_name=console_name,
                window_name=session.name,
                workspace_path=workspace_path,
                shell=shell,
                session=session,
                vm=vm,
                session_user=session_user,
                admin_user=vm.admin_username,
                config_index=config_index,
                tmux_name=runtime_name,
            )
            if pane_id is None:
                failed_shells.append(config_index)
        if not _apply_layout(run, q_con, q_session, layout) and tmux_name is not None:
            return SessionWindowBuild(built=False)
    # Focus the session pane so the operator lands on the attach output
    # rather than the most-recently-created shell pane. Done unconditionally
    # (cheap, and consistent across windows with and without shells).
    if not _focus_session_pane(run, q_con, q_session, base_pidx) and tmux_name is not None:
        return SessionWindowBuild(built=False)
    return SessionWindowBuild(built=True, failed_shells=failed_shells)


def _build_console_tmux_unlogged(
    access: ConsoleAccess,
    db: Database,
    registry: Registry,
    console: ConsoleRow,
    vm: VMRow,
    *,
    values: Mapping[str, str],
    layout: str,
    members: Sequence[ConsoleSessionRow] | None = None,
) -> None:
    """Build under a reserved name and publish only a complete console.

    The admin-shell window parks its environment on the admin's access, and
    each shell pane on its own identity's (see ``_split_shell_pane``).
    """
    from agentworks.execution.requests import LoginShell
    from agentworks.sessions.tmux import ProbeStatus, access_run_command

    from .attach import _teardown_console_tmux, _tmux_session_presence

    admin = access.admin
    run = access_run_command(admin, deadline=None, elevated=False)
    members = tuple(db.list_console_sessions(console.name) if members is None else members)
    if not members and not console.admin_shell:
        raise ExternalError(
            f"console '{console.name}' has no configured windows",
            entity_kind="console",
            entity_name=console.name,
        )

    canonical_name = tmux_session_name(console.name)
    staging_name = tmux_staging_name(console.name)
    canonical_presence = _tmux_session_presence(admin, canonical_name)
    if canonical_presence is ProbeStatus.UNKNOWN:
        raise ExternalError(
            f"could not determine canonical tmux state for console '{console.name}'",
            entity_kind="console",
            entity_name=console.name,
        )
    if canonical_presence is ProbeStatus.PRESENT:
        raise ExternalError(
            f"console '{console.name}' already has a canonical tmux runtime",
            entity_kind="console",
            entity_name=console.name,
        )
    _teardown_console_tmux(admin, console.name)
    q_staging_create = shlex.quote(staging_name)

    def staging_failed() -> ExternalError:
        return ExternalError(
            f"failed to create staging tmux state for console '{console.name}'",
            entity_kind="console",
            entity_name=console.name,
        )

    def admin_shell_alive() -> bool:
        """Whether the admin shell window's pane still runs: gone, or dead under ``remain-on-exit``, is not."""
        q_window = exact_tmux_target(f"{staging_name}:{ADMIN_SHELL_WINDOW}")
        listed = run(f"tmux list-panes -t {q_window} -F '#{{pane_dead}}'", check=False)
        return listed.ok and listed.stdout.split() == ["0"]

    try:
        if console.admin_shell:
            # Window 0 is the admin shell. The literal tmux window name
            # '--admin--' cannot collide with a valid session name. Its
            # environment is parked on the admin's own connection and the
            # pane's command collects it (the terminal handover), so no value
            # reaches a tmux argument or the SSH command line.
            pane_env = _mc._resolve_admin_env(db, registry, values=values, vm=vm)
            with admin.park(LoginShell(env=pane_env)) as handover:
                created = run(
                    f"tmux new-session -d -s {q_staging_create} -n {shlex.quote(ADMIN_SHELL_WINDOW)} "
                    f"{shlex.quote(handover.command())}",
                    check=False,
                )
                if not created.ok:
                    raise staging_failed()  # inside the block, so the parked request goes at once
            # A pane that never collected its request never started its shell. One that did can
            # still lose it at once (the launcher refusing on the terminal, or a shell that ends
            # immediately); the listing after collection follows another round trip, so it
            # usually sees that. A shell that ends later is not the build's to see.
            if not handover.collected or not admin_shell_alive():
                raise ExternalError(
                    f"the admin shell window of console '{console.name}' did not start its shell",
                    entity_kind="console",
                    entity_name=console.name,
                    hint=f"Retry with `agw console start {console.name}`.",
                )
            placeholder_used = False
            placeholder = ""
        else:
            # tmux requires one window while the real windows are constructed.
            placeholder = "_PLACEHOLDER"
            created = run(f"tmux new-session -d -s {q_staging_create} -n {shlex.quote(placeholder)}", check=False)
            if not created.ok:
                raise staging_failed()
            placeholder_used = True

        if members:
            output.detail(f"Adding {len(members)} session window(s) to console '{console.name}'...")
        for member in members:
            built = _mc._add_session_window(
                access,
                db,
                registry,
                values=values,
                console_name=console.name,
                member=member,
                vm=vm,
                layout=layout,
                tmux_name=staging_name,
            )
            if not built.built or built.failed_shells:
                raise ExternalError(
                    f"failed to build complete tmux state for console '{console.name}'",
                    entity_kind="console",
                    entity_name=console.name,
                    hint=f"Retry with `agw console start {console.name}`.",
                )

        if placeholder_used:
            removed = run(f"tmux kill-window -t {exact_tmux_target(f'{staging_name}:{placeholder}')}", check=False)
            if not removed.ok:
                raise ExternalError(
                    f"failed to remove the staging placeholder for console '{console.name}'",
                    entity_kind="console",
                    entity_name=console.name,
                )
        published = run(
            f"tmux rename-session -t {exact_tmux_target(staging_name)} {shlex.quote(canonical_name)}", check=False
        )
        if not published.ok:
            raise ExternalError(
                f"failed to publish tmux state for console '{console.name}'",
                entity_kind="console",
                entity_name=console.name,
            )
        canonical_presence = _tmux_session_presence(admin, canonical_name)
        staging_presence = _tmux_session_presence(admin, staging_name)
        if canonical_presence is not ProbeStatus.PRESENT or staging_presence is not ProbeStatus.ABSENT:
            raise ExternalError(
                f"console '{console.name}' staging publication could not be verified",
                entity_kind="console",
                entity_name=console.name,
            )
        db.record_console_started(console.name)
    except (KeyboardInterrupt, Exception):
        # Ordinary cleanup failures preserve the primary error; new cancellation wins.
        try:
            _teardown_console_tmux(admin, console.name)
        except UserAbort:
            raise
        except Exception:
            pass
        raise


def _build_console_tmux(
    access: ConsoleAccess,
    db: Database,
    registry: Registry,
    console: ConsoleRow,
    vm: VMRow,
    *,
    values: Mapping[str, str],
    layout: str,
    members: Sequence[ConsoleSessionRow] | None = None,
) -> None:
    """Build with redacted diagnostics, logging the admin's requests to ``console-build``.

    A log that fails to close never masks the build's own failure, though a
    cancellation raised while closing it wins. See
    ``_build_console_tmux_unlogged``.
    """
    from agentworks.harness_setup.outcomes import sanitize_setup_error
    from agentworks.vms.manager import operation_log

    primary: BaseException | None = None
    try:
        with operation_log(vm.name, "console-build", redactions=tuple(values.values())) as log:
            try:
                _build_console_tmux_unlogged(
                    access.logged(log),
                    db,
                    registry,
                    console,
                    vm,
                    values=values,
                    layout=layout,
                    members=members,
                )
            except BaseException as error:
                primary = error
                if isinstance(error, AgentworksError):
                    sanitize_setup_error(error, redactions=values.values())
                raise
    except Exception as error:
        if primary is None or error is primary or isinstance(error, UserAbort):
            raise
    if primary is not None:
        raise primary
