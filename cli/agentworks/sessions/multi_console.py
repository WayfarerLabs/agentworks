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
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.sessions.multi_console_layout import (
    SHELL_INDEX_OPTION,
    _apply_layout,
    _focus_session_pane,
    _list_shell_panes,
    _reorder_session_windows,
    _reorder_shell_panes,
)
from agentworks.sessions.tmux import tmux_cmd
from agentworks.vms.manager import keep_vm_active

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

# Literal tmux window name for the optional admin-shell window. Wrapped in
# double hyphens so it cannot collide with any session name: validate_name
# rejects leading hyphens, consecutive hyphens, and trailing hyphens.
ADMIN_SHELL_WINDOW = "--admin--"


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

    The shell count N must be a non-negative integer. The session name uses
    the loose reference form of validate_name (``allow_double_hyphen=True``)
    so legacy sessions with the pre-rename ``ws--agent`` convention can
    still be referenced; the DB is the ultimate arbiter of existence and is
    checked downstream by the caller.
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
            raise ValidationError(
                f"invalid session spec '{spec}': shell count must be a non-negative integer"
            ) from None
        if shells < 0:
            raise ValidationError(
                f"invalid session spec '{spec}': shell count must be >= 0"
            )
    else:
        raise ValidationError(
            f"invalid session spec '{spec}': use 'name' or 'name+N'"
        )
    try:
        validate_name(name, allow_double_hyphen=True)
    except ValidationError as exc:
        raise ValidationError(f"invalid session spec '{spec}': {exc}") from None
    return SessionSpec(name=name, shells=shells)


def default_shells(count: int) -> list[ShellEntry]:
    """Build N default shell entries (agent user, workspace root)."""
    return [{"cwd": None, "admin": False} for _ in range(count)]


# -- Helpers ---------------------------------------------------------------


def _require_console(db: Database, name: str) -> ConsoleRow:
    console = db.get_console(name)
    if console is None:
        raise NotFoundError(
            f"console '{name}' not found",
            entity_kind="console",
            entity_name=name,
        )
    return console


def _vm_sessions(db: Database, vm_name: str) -> list[SessionRow]:
    """All sessions belonging to workspaces on the given VM."""
    sessions: list[SessionRow] = []
    for ws in db.list_workspaces(vm_name=vm_name):
        sessions.extend(db.list_sessions(workspace_name=ws.name))
    return sessions


def running_session_names(
    db: Database, config: Config, vm_name: str
) -> list[str]:
    """SSH-probe the VM and return names of sessions whose live tmux state is OK.

    Uses the same one-round-trip-per-VM check that powers ``aw session list``.
    Returns alphabetically sorted names.

    Raises ConnectivityError when the VM has sessions eligible to be probed
    (valid PID + boot_id) but the probe came back empty -- almost always a
    transport failure that we don't want to silently report as "nothing
    running". A VM with zero eligible sessions simply returns an empty list.
    """
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.sessions.manager import batch_check_all_sessions, filter_sessions

    sessions = filter_sessions(db, vm_name=vm_name)
    status_map = batch_check_all_sessions(sessions, db=db, config=config)

    # If we have sessions that *should* have been probed but none came back
    # with a status, the probe almost certainly failed (e.g. SSH unreachable).
    # batch_check_all_sessions warns on exceptions but returns silently on
    # `check=False` non-zero exits, so we cannot rely on the warning alone.
    checkable = [
        s for s in sessions
        if s.pid is not None and s.pid != PID_STOPPED and s.pid > 0 and s.boot_id
    ]
    if checkable and not status_map:
        raise ConnectivityError(
            f"could not determine running sessions on VM '{vm_name}' "
            f"(status probe returned no results)",
            entity_kind="vm",
            entity_name=vm_name,
            hint="Check VM reachability.",
        )

    return sorted(
        s.name for s in sessions if status_map.get(s.name) == SessionStatus.OK
    )


def infer_vm_from_session_specs(
    db: Database, session_specs: list[str]
) -> str | None:
    """Return the single VM hosting all listed sessions.

    - Returns None if *session_specs* is empty or none of the names resolve to
      a known session (callers prompt for --vm or surface the not-found error
      from create_console).
    - Raises ValidationError if listed sessions span more than one VM (the
      user must disambiguate with --vm explicitly).
    """
    if not session_specs:
        return None

    vms: set[str] = set()
    for spec in session_specs:
        try:
            session_name = parse_session_spec(spec).name
        except ValidationError:
            # Bad spec -- defer the error to create_console's own validation.
            continue
        session = db.get_session(session_name)
        if session is None:
            continue
        ws = db.get_workspace(session.workspace_name)
        if ws and ws.vm_name:
            vms.add(ws.vm_name)

    if len(vms) > 1:
        raise ValidationError(
            f"sessions span multiple VMs ({', '.join(sorted(vms))})",
            entity_kind="console",
            hint="Pass --vm to pick one.",
        )
    return next(iter(vms)) if vms else None


def _verify_session_on_vm(db: Database, session_name: str, vm_name: str) -> None:
    """Raise if the session does not exist or is not on the given VM."""
    session = db.get_session(session_name)
    if session is None:
        raise NotFoundError(
            f"session '{session_name}' not found",
            entity_kind="session",
            entity_name=session_name,
        )
    ws = db.get_workspace(session.workspace_name)
    if ws is None or ws.vm_name != vm_name:
        raise ValidationError(
            f"session '{session_name}' is not on VM '{vm_name}'",
            entity_kind="session",
            entity_name=session_name,
        )


def _dedupe_specs(specs: list[SessionSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValidationError(
                f"session '{spec.name}' listed more than once",
                entity_kind="session",
                entity_name=spec.name,
            )
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

    Explicit *session_specs* keep their argument order. *fill_all* appends
    every other session on the VM in alphabetical order with zero shells.
    *add_admin_shell* adds a window 0 login shell as the VM admin (legacy
    vm-console behavior) -- useful when you want a top-level shell alongside
    the curated session windows. All inserts run in one transaction; the
    console is not created if any step fails.

    Note: this function is DB-only. Live filtering (e.g. --all-running) is
    resolved by the CLI layer into an explicit list of session names before
    calling create_console.
    """
    validate_name(name)

    if db.get_console(name) is not None:
        raise AlreadyExistsError(
            f"console '{name}' already exists",
            entity_kind="console",
            entity_name=name,
        )
    if db.get_vm(vm_name) is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )

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
        # fill_all (--all) and --all-running both go through specs expansion before
        # reaching here, so an empty specs list means the expansion turned up
        # nothing or no flags were passed at all.
        if fill_all:
            detail = f"VM '{vm_name}' has no sessions"
        else:
            detail = (
                "specify at least one session, pass --all (or --all-running for "
                "live sessions only), or pass --add-admin-shell"
            )
        raise ValidationError(
            f"refusing to create empty console '{name}' ({detail})",
            entity_kind="console",
            entity_name=name,
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
            raise AlreadyExistsError(
                f"session '{spec.name}' is already a member of console '{console_name}'",
                entity_kind="console-member",
                entity_name=spec.name,
            )

    with db.transaction():
        for spec in specs:
            db.add_console_session(console_name, spec.name, default_shells(spec.shells))

    output.info(f"Added {len(specs)} session(s) to console '{console_name}'.")

    with _live_best_effort(f"add-sessions to '{console_name}'", console_name=console_name):
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
                target,
                db,
                console_name=console_name,
                member=member,
                vm=vm,
                layout=config.named_console.tmux_layout,
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
            raise NotFoundError(
                f"session '{n}' is not a member of console '{console_name}'",
                entity_kind="console-member",
                entity_name=n,
            )
    with db.transaction():
        for n in session_names:
            db.remove_console_session(console_name, n)
    output.info(
        f"Removed {len(session_names)} session(s) from console '{console_name}'."
    )

    with _live_best_effort(
        f"remove-sessions from '{console_name}'", console_name=console_name
    ):
        live = _live_target(db, config, console.vm_name)
        if live is None:
            return
        _vm, target = live
        kill_session_windows(
            target, pairs=[(console_name, n) for n in session_names]
        )


def reorder_sessions(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_names: list[str],
) -> None:
    """Bump *session_names* to the front of a console's session order.

    The listed sessions become the first windows (in the order given, after
    the ``--admin--`` window if the console has one). Unlisted members keep
    their current relative order and are pushed back.

    Every name in *session_names* must already be a member; duplicates and
    an empty list are rejected (matches create_console's stance that
    no-op-shaped input is almost certainly a typo). Atomic at the DB layer;
    if the console's tmux session is live, also reorders the windows via
    ``tmux swap-window`` (best-effort).

    Short-circuits with an info message and no DB / tmux work when the
    listed sessions are already in the requested order at the front.
    """
    console = _require_console(db, console_name)

    if not session_names:
        raise ValidationError(
            f"refusing to reorder console '{console_name}' with no sessions "
            f"specified (pass the member names to bump to the front)",
            entity_kind="console",
            entity_name=console_name,
        )

    seen: set[str] = set()
    for name in session_names:
        if name in seen:
            raise ValidationError(
                f"session '{name}' listed more than once",
                entity_kind="session",
                entity_name=name,
            )
        seen.add(name)

    # One read for both membership validation and the current-order baseline,
    # rather than N get_console_session calls.
    current = db.list_console_sessions(console_name)
    current_order = [m.session_name for m in current]
    current_set = set(current_order)
    for name in session_names:
        if name not in current_set:
            raise NotFoundError(
                f"session '{name}' is not a member of console '{console_name}'",
                entity_kind="console-member",
                entity_name=name,
            )

    # `remaining` preserves DB-order for unlisted members regardless of
    # where in the input list those names appeared.
    front = list(session_names)
    remaining = [n for n in current_order if n not in seen]
    desired_order = front + remaining

    if desired_order == current_order:
        output.info(
            f"Console '{console_name}' is already in the requested order; "
            f"nothing to do."
        )
        return

    db.reorder_console_sessions(console_name, desired_order)
    output.info(
        f"Reordered {len(front)} session(s) to the front of console "
        f"'{console_name}'."
    )

    with _live_best_effort(
        f"reorder-sessions in '{console_name}'", console_name=console_name
    ):
        live = _live_target(db, config, console.vm_name)
        if live is None:
            return
        _vm, target = live
        if not _console_tmux_exists(target, console_name):
            return
        _reorder_session_windows(
            target,
            console_name=console_name,
            ordered_session_windows=desired_order,
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
        raise ValidationError("cwd may not be empty (omit it for workspace root)")
    if cwd.startswith("/"):
        raise ValidationError(
            f"cwd '{cwd}' must be relative to the workspace root, not absolute"
        )
    if ".." in cwd.split("/"):
        raise ValidationError(
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
        raise NotFoundError(
            f"session '{session_name}' is not a member of console '{console_name}'",
            entity_kind="console-member",
            entity_name=session_name,
        )
    new_shell: ShellEntry = {"cwd": cwd, "admin": admin}
    new_shells = [*cs.shells, new_shell]
    db.update_console_shells(console_name, session_name, new_shells)
    user_tag = "admin" if admin else "agent"
    output.info(
        f"Added {user_tag} shell at {cwd or '<workspace>'} to '{session_name}' "
        f"in console '{console_name}'."
    )

    with _live_best_effort(
        f"add-shell to '{console_name}:{session_name}'", console_name=console_name
    ):
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
            # new_shell is appended to cs.shells, so its index in the updated
            # configured list is the previous list's length.
            config_index=len(cs.shells),
        )
        q_con = shlex.quote(tmux_session_name(console_name))
        q_win = shlex.quote(session_name)
        # No _focus_session_pane here: the operator is mid-attach when they
        # run `add-shell`; pulling focus off their current pane would be
        # jarring. The layout still re-applies so geometry reflects the new
        # pane count.
        _apply_layout(target, q_con, q_win, config.named_console.tmux_layout)


def restore_session(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_name: str,
) -> None:
    """Reconcile a single session window's live tmux state against its configured
    shell list. Additive only: rebuilds the window if missing, fills in any
    panes the user accidentally killed (in their correct config positions), but
    refuses to remove panes if the user has more live than configured.

    Strict on untagged panes: a window built before pane-tagging was added has
    no way to determine which configured shell is missing, so we refuse and
    direct the operator to `attach --recreate`.
    """
    console = _require_console(db, console_name)
    member = db.get_console_session(console_name, session_name)
    if member is None:
        raise NotFoundError(
            f"session '{session_name}' is not a member of console '{console_name}'",
            entity_kind="console-member",
            entity_name=session_name,
        )

    vm, target = _prepare_vm_target_for_attach(db, config, console.vm_name)
    # restore_session raises StateError/ExternalError on failure, so it's
    # not a best-effort op (those are exempted from the keepalive sweep by
    # base.VMProvisioner.vm_active's docstring). Wrap the SSH-heavy body
    # so a freshly booted WSL2 distro doesn't idle out between the window
    # probe and the pane reconciliation.
    with keep_vm_active(db, config, vm):
        if not _console_tmux_exists(target, console_name):
            raise StateError(
                f"console '{console_name}' has no live tmux session on VM "
                f"'{console.vm_name}'.",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name}` to build it; "
                    f"restore-session only repairs an already-running console."
                ),
            )

        q_con = shlex.quote(tmux_session_name(console_name))
        q_win = shlex.quote(session_name)
        layout = config.named_console.tmux_layout
        configured_count = len(member.shells)

        # Window present?
        res = target.run(
            f"tmux list-windows -t {q_con} -F '#{{window_name}}'",
            check=False,
        )
        if not res.ok:
            raise ExternalError(
                f"failed to list windows for console '{console_name}': "
                f"{res.stderr.strip()}",
                entity_kind="console",
                entity_name=console_name,
            )
        windows = res.stdout.strip().splitlines()
        if session_name not in windows:
            output.info(
                f"window '{session_name}' is missing; rebuilding from config..."
            )
            _add_session_window(
                target,
                db,
                console_name=console_name,
                member=member,
                vm=vm,
                layout=layout,
            )
            return

        # Window exists. Enumerate shell panes (skipping pane_index 0, the session
        # pane). The session pane is created via tmux new-window and intentionally
        # left untagged; every shell pane is created via _split_shell_pane and
        # gets an @agentworks-shell-index tag.
        shell_panes = _list_shell_panes(target, q_con, q_win)
        if shell_panes is None:
            raise ExternalError(
                f"failed to list panes for window '{session_name}'",
                entity_kind="console",
                entity_name=console_name,
            )

        untagged = [pid for pid, _pidx, cidx in shell_panes if cidx is None]
        if untagged:
            # Untagged shell panes happen for two reasons: (a) the window predates
            # the pane-tagging feature, or (b) the operator manually split a pane
            # via `tmux split-window` instead of `console add-shell`. Either way,
            # restore-session can't map the live pane back to a configured shell
            # index, so we refuse and direct the operator to rebuild.
            raise StateError(
                f"window '{session_name}' has {len(untagged)} shell pane(s) with "
                f"no agentworks tag.",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name} --recreate` "
                    f"to rebuild and retag from scratch."
                ),
            )

        # Validate that the tag values form a subset of 0..configured_count-1 with
        # no duplicates. Three corruptions are caught here, all of which restore-
        # session can't safely repair:
        #   - duplicates: two panes claim the same config index
        #   - out-of-range: a pane references a config index that no longer exists
        #     (e.g. config shrank or DB was edited)
        #   - implied "too many panes": pigeonhole says any live_count >
        #     configured_count must trigger one of the two above (since untagged
        #     panes are already rejected by the strict check earlier)
        tag_values = [cidx for _pid, _pidx, cidx in shell_panes if cidx is not None]
        # Single-pass O(n) duplicate + out-of-range detection. The naive
        # tag_values.count(v) in a comprehension would be O(n^2); not a concern at
        # typical shell counts (1-5) but free to do correctly.
        counts = Counter(tag_values)
        duplicates = sorted(v for v, n in counts.items() if n > 1)
        out_of_range = sorted(v for v in counts if v < 0 or v >= configured_count)
        if duplicates or out_of_range:
            parts: list[str] = []
            if duplicates:
                parts.append(f"duplicate tags {duplicates}")
            if out_of_range:
                if configured_count == 0:
                    parts.append(
                        f"{len(out_of_range)} tagged shell pane(s) but session has "
                        f"no configured shells"
                    )
                else:
                    parts.append(
                        f"tags {out_of_range} point past the configured range "
                        f"(0..{configured_count - 1})"
                    )
            raise StateError(
                f"window '{session_name}' has shell panes with inconsistent tags "
                f"({'; '.join(parts)}).",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name} --recreate` "
                    f"to rebuild and retag from scratch."
                ),
            )

        # tag_values is now a subset of 0..configured_count-1 with no duplicates,
        # so len(tag_values) <= configured_count.
        if len(tag_values) == configured_count:
            output.info(
                f"session '{session_name}' already matches config "
                f"({len(tag_values)} shell pane(s)); nothing to do."
            )
            # Still focus the session pane on this no-op path so post-restore
            # landing focus is consistent whether or not repairs were needed.
            _focus_session_pane(target, q_con, q_win)
            return

        # Strict subset: figure out which config indices are missing.
        missing = sorted(set(range(configured_count)) - set(tag_values))

        session = db.get_session(session_name)
        if session is None:
            raise StateError(
                f"session '{session_name}' no longer exists in the database",
                entity_kind="session",
                entity_name=session_name,
                hint="Remove the session from the console first.",
            )
        workspace_path = _resolve_workspace_path(db, session)
        if workspace_path is None:
            raise StateError(
                f"workspace for session '{session_name}' is missing; cannot restore.",
                entity_kind="session",
                entity_name=session_name,
            )
        session_user = _session_linux_user(db, session, vm)

        output.info(
            f"Restoring {len(missing)} shell pane(s) in '{session_name}': "
            f"config indices {missing}."
        )
        # Collect each split's outcome so a partial failure becomes a loud error
        # rather than a silent exit-0 leaving panes missing or untagged.
        failed: list[int] = []
        for cidx in missing:
            pane_id = _split_shell_pane(
                target,
                console_name=console_name,
                window_name=session_name,
                workspace_path=workspace_path,
                shell=member.shells[cidx],
                session_user=session_user,
                admin_user=vm.admin_username,
                config_index=cidx,
            )
            if pane_id is None:
                failed.append(cidx)
        if failed:
            raise ExternalError(
                f"restore-session left '{session_name}' incomplete: failed to "
                f"create/tag config indices {failed} (see warnings above).",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name} --recreate` "
                    f"to rebuild from scratch."
                ),
            )

        # New panes land at the tail; reorder so visual pane_index matches
        # config_index for every shell pane.
        _reorder_shell_panes(target, q_con, q_win, configured_count)

        # Re-apply the layout to redistribute geometry after the splits and
        # swaps, then land the operator on the session pane (matches attach /
        # recreate behavior; restore-session is a repair, not an attach, but
        # we still want consistent landing focus).
        _apply_layout(target, q_con, q_win, layout)
        _focus_session_pane(target, q_con, q_win)


# -- Read-side helpers ----------------------------------------------------


def list_consoles(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
) -> None:
    """Print a table of consoles, optionally filtered by VM, workspace, or agent.

    Workspace/agent filters match a console if any of its member sessions
    match; see `Database.list_consoles_with_counts` for full semantics.
    Filters compose with AND.
    """
    consoles = db.list_consoles_with_counts(
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
    )
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
        output.info(f"  [{i}] {m.session_name}  ({_shell_summary(m.shells)})")


# -- Tmux orchestration ----------------------------------------------------


def _session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user that owns a session's tmux server."""
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' not found "
                f"(referenced by session '{session.name}')",
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


def _console_tmux_exists(target: ExecTarget, console_name: str) -> bool:
    q = shlex.quote(tmux_session_name(console_name))
    return target.run(f"tmux has-session -t {q} 2>/dev/null", check=False).ok


def _kill_console_tmux(target: ExecTarget, console_name: str) -> None:
    q = shlex.quote(tmux_session_name(console_name))
    target.run(f"tmux kill-session -t {q}", check=False)


def kill_session_windows(
    target: ExecTarget,
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
            if not _console_tmux_exists(target, console_name):
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
        recovery = "; ".join(
            f"agw console attach {shlex.quote(c)} --recreate" for c in affected
        )
        output.warn(
            f"live console window cleanup failed: {exc}. "
            f"Stale windows may persist; rebuild with: {recovery}"
        )


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
    config_index: int,
) -> str | None:
    """Split off one shell pane in an existing console window and tag the new
    pane with its position in the configured shell list. The tag lets
    restore-session detect which specific shell (out of an ordered list) is
    missing after an accidental kill.

    Returns the new pane id on full success (split + tag both completed), or
    None if either step failed (tmux refused the split, or the pane was created
    but its id couldn't be captured so the tag couldn't be set). Callers in
    best-effort paths (`add_shell`, `_add_session_window`) may ignore the
    return value; `restore_session` checks each return so a partial restore
    is loud rather than a silent exit-0."""
    cwd = shell["cwd"]
    full_path = posixpath.join(workspace_path, cwd) if cwd else workspace_path
    q_full = shlex.quote(full_path)
    q_con = shlex.quote(tmux_session_name(console_name))
    q_win = shlex.quote(window_name)
    use_admin = shell["admin"] or session_user == admin_user

    # Login shell in both branches keeps profile/aliases consistent with the
    # session pane behavior (sessions use $SHELL -l via create_session).
    # Diagnostic on cd failure so a missing cwd shows the actual path.
    # The echo argument is shlex.quoted so paths containing shell metacharacters
    # (quotes, $(...), backticks) print literally rather than triggering
    # expansion. -P -F '#{pane_id}' makes split-window print the new pane's ID
    # to stdout so we can target set-option at that exact pane immediately after.
    q_diag = shlex.quote(f"cwd missing: {full_path}")
    if use_admin:
        bootstrap = (
            f'cd {q_full} || echo {q_diag}; '
            f'exec "$SHELL" -l'
        )
        cmd = (
            f"tmux split-window -t {q_con}:{q_win} -P -F '#{{pane_id}}' "
            f"-c {q_full} {shlex.quote(bootstrap)}"
        )
    else:
        q_user = shlex.quote(session_user)
        bootstrap = (
            f'cd {q_full} || echo {q_diag}; '
            f'exec "$SHELL" -l'
        )
        pane_cmd = (
            f"exec sudo --login -u {q_user} bash -c {shlex.quote(bootstrap)}"
        )
        cmd = (
            f"tmux split-window -t {q_con}:{q_win} -P -F '#{{pane_id}}' "
            f"-c {q_full} {shlex.quote(pane_cmd)}"
        )

    res = target.run(cmd, check=False)
    if not res.ok:
        output.warn(
            f"failed to add shell pane in '{window_name}': {res.stderr.strip()}"
        )
        return None

    q_console = shlex.quote(console_name)
    pane_id = res.stdout.strip()
    if not pane_id:
        # tmux is supposed to print the new pane id on stdout under `-P -F`;
        # an empty stdout means we lost the handle and can't tag the pane.
        # The pane is live but invisible to restore-session, which will
        # later refuse to repair this window (untagged-pane strict check).
        output.warn(
            f"added shell pane in '{window_name}' but couldn't capture its id; "
            f"the pane is untagged. restore-session won't be able to repair "
            f"this window; use `agw console attach {q_console} "
            f"--recreate` if you need clean tag state."
        )
        return None
    q_pane = shlex.quote(pane_id)
    tag_res = target.run(
        f"tmux set-option -p -t {q_pane} {SHELL_INDEX_OPTION} {config_index}",
        check=False,
    )
    if not tag_res.ok:
        # The split happened (the pane is live) but tagging failed. Treat this
        # as a failure: callers like restore_session must know the pane is
        # untagged so the operator gets a loud signal rather than a future
        # untagged-pane error on the next restore-session.
        output.warn(
            f"added shell pane in '{window_name}' but tagging failed "
            f"({tag_res.stderr.strip() or 'tmux refused set-option'}); "
            f"the pane is untagged. Use `agw console attach "
            f"{q_console} --recreate` to rebuild and retag from scratch."
        )
        return None
    return pane_id


def _add_session_window(
    target: ExecTarget,
    db: Database,
    *,
    console_name: str,
    member: ConsoleSessionRow,
    vm: VMRow,
    layout: str,
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

    if member.shells:
        # _session_linux_user raises NotFoundError if the session points at an
        # agent row that's gone (FK violation under PRAGMA foreign_keys = OFF,
        # or stale state from a migration). Match the missing-session /
        # missing-workspace handling above: warn and skip rather than abort
        # the whole console build.
        try:
            session_user = _session_linux_user(db, session, vm)
        except NotFoundError as exc:
            output.warn(
                f"agent for session '{session.name}' is missing ({exc}); "
                f"skipping shell panes for this window"
            )
            return
        for config_index, shell in enumerate(member.shells):
            _split_shell_pane(
                target,
                console_name=console_name,
                window_name=session.name,
                workspace_path=workspace_path,
                shell=shell,
                session_user=session_user,
                admin_user=vm.admin_username,
                config_index=config_index,
            )
        _apply_layout(target, q_con, q_session, layout)
    # Focus the session pane so the operator lands on the attach output
    # rather than the most-recently-created shell pane. Done unconditionally
    # (cheap, and consistent across windows with and without shells).
    _focus_session_pane(target, q_con, q_session)


def _build_console_tmux(
    target: ExecTarget,
    db: Database,
    console: ConsoleRow,
    vm: VMRow,
    *,
    layout: str,
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
        # Window 0 is the admin shell. The literal tmux window name '--admin--'
        # is impossible for any session (validate_name rejects leading hyphen,
        # consecutive hyphens, and trailing hyphen), so we don't need extra
        # logic to distinguish this internal window from real session windows.
        target.run(
            f"tmux new-session -d -s {q_con} -n {shlex.quote(ADMIN_SHELL_WINDOW)} "
            f"{shlex.quote('exec sudo su --login ' + shlex.quote(vm.admin_username))}"
        )
        placeholder_used = False
        placeholder = ""
    else:
        # tmux requires at least one window at all times. Create a transient
        # placeholder whose name (leading underscore, all uppercase) is doubly
        # impossible for any real session: validate_name requires names to
        # start with an alphanumeric AND be lowercase, so this string can
        # never collide with a session name, including legacy '--' names that
        # the loose validator now allows by reference. Stands out visibly in
        # tmux list-windows output.
        placeholder = "_PLACEHOLDER"
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
            layout=layout,
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
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    _ensure_vm_running(db, config, vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )
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
        raise StateError(
            "already inside a tmux session. Nesting is not recommended "
            "(prefix key conflicts, confusing detach behavior).",
            hint="Pass --allow-nesting to override.",
        )

    console = _require_console(db, name)
    vm, target = _prepare_vm_target_for_attach(db, config, console.vm_name)

    with keep_vm_active(db, config, vm):
        exists = _console_tmux_exists(target, name)
        layout = config.named_console.tmux_layout
        if recreate and exists:
            output.info(f"Rebuilding console '{name}' (--recreate)...")
            _build_console_tmux(target, db, console, vm, layout=layout)
        elif not exists:
            output.info(f"Building console '{name}' on first attach...")
            _build_console_tmux(target, db, console, vm, layout=layout)
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
        raise UserAbort("delete cancelled")

    # Best-effort tmux teardown. Don't block the DB delete on VM reachability.
    teardown_failed = False
    try:
        live = _live_target(db, config, console.vm_name)
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
        output.info(f"Console '{name}' deleted.")
