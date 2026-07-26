"""DB-level create/add/remove/reorder/delete operations for named consoles,
plus the live-tmux best-effort sync each one triggers when the console's
tmux session is already up.

``kill_session_windows``, ``_pane_secret_target``, ``_live_target``,
``_console_tmux_exists``, and ``_add_session_window`` are monkeypatched by
tests directly on the ``agentworks.sessions.multi_console`` package object
(so a test can, e.g., exercise ``remove_sessions``'s live-sync path without a
live VM). A patch on the package object only rebinds the package's own
attribute, not the attribute of the module that actually defines the
function, so every call site below goes through the package object at call
time (``_mc.<name>(...)``) rather than a direct or bare reference.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import AlreadyExistsError, NotFoundError, ValidationError
from agentworks.resources.access import named_console_template
from agentworks.sessions.multi_console_layout import _apply_layout, _reorder_session_windows

from ._helpers import (
    SessionSpec,
    _dedupe_specs,
    _require_console,
    _verify_session_on_vm,
    _vm_sessions,
    default_shells,
    parse_session_spec,
    tmux_session_name,
)
from .attach import _live_best_effort, _session_linux_user
from .tmux_build import PreserveEnvMemo, _resolve_workspace_path, _split_shell_pane

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, ShellEntry
    from agentworks.secrets import SecretTarget


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
        extras = sorted(s.name for s in _vm_sessions(db, vm_name) if s.name not in explicit_names)
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
    output.result(f"Console '{name}' created with {len(specs)} session(s){extras_note}.")


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
    from agentworks.bootstrap import build_registry

    console = _require_console(db, console_name)
    registry = build_registry(config)
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

    # Eager-prompting orchestration: when
    # any spec carries shells > 0 the live-attach path below will open
    # new shells via _add_session_window. Resolve every referenced
    # secret BEFORE the DB write so a failure leaves no partial state.
    # No platform instance participates in this command (the live sync
    # is pure Tailscale, no platform ops), so this is the operation's
    # ONE prompt session by construction: nothing to fold into a
    # bind boundary.
    # default_shells produces {cwd: None, admin: False} entries, so the
    # only admin-promotion path is session_user == admin_user (an
    # admin-mode session). The resolve is skipped when no specs carry
    # shells (the bare ``add-sessions s1 s2`` shape) -- the empty values
    # dict below still feeds the live-attach path, whose windows then
    # have no panes to compose env for.
    secret_values: dict[str, str] = {}
    if any(spec.shells > 0 for spec in specs):
        from agentworks.secrets import resolve_for_command

        vm_row = db.get_vm(console.vm_name)
        new_shell_targets: list[SecretTarget] = []
        if vm_row is not None:
            for spec in specs:
                if spec.shells <= 0:
                    continue
                session = db.get_session(spec.name)
                if session is None:
                    continue
                try:
                    session_user = _session_linux_user(db, session, vm_row)
                except NotFoundError:
                    continue
                # All new shells are admin=False (default_shells), so
                # use_admin promotion only fires for admin-mode sessions.
                use_admin = session_user == vm_row.admin_username
                pane = _mc._pane_secret_target(
                    db,
                    registry,
                    vm=vm_row,
                    session=session,
                    is_admin_pane=use_admin,
                )
                if pane is None:
                    continue
                # Every new shell on this session has the same scope
                # chain, so one target covers them all (eager-resolve
                # unions per secret name).
                new_shell_targets.append(pane)
        if new_shell_targets:
            secret_values = resolve_for_command(new_shell_targets, config, registry)

    with db.transaction():
        for spec in specs:
            db.add_console_session(console_name, spec.name, default_shells(spec.shells))

    output.result(f"Added {len(specs)} session(s) to console '{console_name}'.")

    with _live_best_effort(f"add-sessions to '{console_name}'", console_name=console_name):
        live = _mc._live_target(db, config, console.vm_name)
        if live is None:
            return
        vm, target = live
        if not _mc._console_tmux_exists(target, console_name):
            return
        preserve_memo: PreserveEnvMemo = {}
        for spec in specs:
            member = db.get_console_session(console_name, spec.name)
            assert member is not None
            _mc._add_session_window(
                target,
                db,
                registry,
                values=secret_values,
                console_name=console_name,
                member=member,
                vm=vm,
                layout=named_console_template(registry).tmux_layout,
                preserve_memo=preserve_memo,
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
    output.result(f"Removed {len(session_names)} session(s) from console '{console_name}'.")

    with _live_best_effort(f"remove-sessions from '{console_name}'", console_name=console_name):
        live = _mc._live_target(db, config, console.vm_name)
        if live is None:
            return
        _vm, target = live
        # kill_session_windows lives in .attach but is called through the
        # package object here (not `from .attach import kill_session_windows`)
        # so that tests monkeypatching `multi_console.kill_session_windows`
        # intercept this call path.
        _mc.kill_session_windows(target, pairs=[(console_name, n) for n in session_names])


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
        output.info(f"Console '{console_name}' is already in the requested order; nothing to do.")
        return

    db.reorder_console_sessions(console_name, desired_order)
    output.result(f"Reordered {len(front)} session(s) to the front of console '{console_name}'.")

    with _live_best_effort(f"reorder-sessions in '{console_name}'", console_name=console_name):
        live = _mc._live_target(db, config, console.vm_name)
        if live is None:
            return
        _vm, target = live
        if not _mc._console_tmux_exists(target, console_name):
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
        raise ValidationError(f"cwd '{cwd}' must be relative to the workspace root, not absolute")
    if ".." in cwd.split("/"):
        raise ValidationError(f"cwd '{cwd}' may not contain '..' segments")


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
    from agentworks.bootstrap import build_registry

    _validate_cwd(cwd)
    console = _require_console(db, console_name)
    registry = build_registry(config)
    cs = db.get_console_session(console_name, session_name)
    if cs is None:
        raise NotFoundError(
            f"session '{session_name}' is not a member of console '{console_name}'",
            entity_kind="console-member",
            entity_name=session_name,
        )

    # Eager-prompting orchestration: resolve any
    # secrets referenced by this pane's env chain BEFORE the DB write +
    # potential pane-split below. No platform instance participates in
    # this command (the live sync is pure Tailscale, no platform ops),
    # so this is the operation's ONE prompt session by construction.
    # Non-interactive failures surface as
    # SecretUnavailableError with no partial state to clean up. We
    # always resolve (even when the console isn't live), since the
    # operator typed add-shell expecting to use the new pane shortly.
    # use_admin promotion matches _split_shell_pane: an admin-mode
    # session has session_user == admin_user, so a pane on that session
    # always runs as admin even when --admin wasn't passed. Compute the
    # same promoted value here so the eager-resolve scope matches what
    # _resolve_pane_env will produce at pane-split time.
    session_row = db.get_session(session_name)
    vm_row = db.get_vm(console.vm_name)
    secret_values: dict[str, str] = {}
    if session_row is not None and vm_row is not None:
        session_user = _session_linux_user(db, session_row, vm_row)
        use_admin = admin or session_user == vm_row.admin_username
        pane_target = _mc._pane_secret_target(
            db,
            registry,
            vm=vm_row,
            session=session_row,
            is_admin_pane=use_admin,
        )
        if pane_target is not None:
            from agentworks.secrets import resolve_for_command

            secret_values = resolve_for_command([pane_target], config, registry)

    new_shell: ShellEntry = {"cwd": cwd, "admin": admin}
    new_shells = [*cs.shells, new_shell]
    db.update_console_shells(console_name, session_name, new_shells)
    user_tag = "admin" if admin else "agent"
    output.result(f"Added {user_tag} shell at {cwd or '<workspace>'} to '{session_name}' in console '{console_name}'.")

    with _live_best_effort(f"add-shell to '{console_name}:{session_name}'", console_name=console_name):
        live = _mc._live_target(db, config, console.vm_name)
        if live is None:
            return
        vm, target = live
        if not _mc._console_tmux_exists(target, console_name):
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
            db,
            registry,
            values=secret_values,
            console_name=console_name,
            window_name=session_name,
            workspace_path=workspace_path,
            shell=new_shell,
            session=session,
            vm=vm,
            session_user=session_user,
            admin_user=vm.admin_username,
            # new_shell is appended to cs.shells, so its index in the updated
            # configured list is the previous list's length.
            config_index=len(cs.shells),
            # One pane, so no probe verdict to share with a sibling split.
            preserve_memo={},
        )
        q_con = shlex.quote(tmux_session_name(console_name))
        q_win = shlex.quote(session_name)
        # No _focus_session_pane here: the operator is mid-attach when they
        # run `add-shell`; pulling focus off their current pane would be
        # jarring. The layout still re-applies so geometry reflects the new
        # pane count.
        _apply_layout(target, q_con, q_win, named_console_template(registry).tmux_layout)
