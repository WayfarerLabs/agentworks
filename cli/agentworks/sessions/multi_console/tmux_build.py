"""Tmux-side mechanics for building and repairing a console's live session:
splitting a tagged shell pane, adding a session window, and the from-scratch
console rebuild.

``attach.py`` imports ``_build_console_tmux`` from this module at load time
(the attach entrypoint needs it), so this module keeps its own references
back into ``attach`` (``_attach_loop_wrapper``, ``_session_linux_user``,
``_kill_console_tmux``) and into ``secrets_env`` (``_SUDO_PRESERVE_PROBE_VAR``)
as function-local imports rather than module-level ones, to avoid a circular
import between the two modules.

``_resolve_pane_env`` (defined in ``secrets_env``) is also monkeypatched by
tests directly on the ``agentworks.sessions.multi_console`` package object,
so the one call site below goes through the package object at call time
(``_mc._resolve_pane_env(...)``) rather than a direct reference, matching the
pattern used across the rest of the package for patched names.
"""

from __future__ import annotations

import posixpath
import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output

from ._helpers import ADMIN_SHELL_WINDOW, tmux_session_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import ConsoleRow, ConsoleSessionRow, Database, SessionRow, ShellEntry, VMRow
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


def _resolve_workspace_path(db: Database, session: SessionRow) -> str | None:
    ws = db.get_workspace(session.workspace_name)
    return ws.workspace_path if ws else None


# Agent linux user -> whether this VM's sudoers honors --preserve-env for it.
# Keyed on the user alone because a console is single-VM by construction, so
# one memo never spans VMs. Scoped to one console operation by the caller and
# never persisted: a VM's sudoers can change out of band, so the answer is only
# trustworthy for as long as the command that asked.
PreserveEnvMemo = dict[str, bool]


def _sudo_can_preserve_env(
    target: Transport,
    *,
    session_user: str,
    vm: VMRow,
    admin_user: str,
    memo: PreserveEnvMemo,
) -> bool:
    """Report whether this VM's sudoers lets the admin use ``--preserve-env``.

    VM init grants the admin ``Defaults:<admin> setenv`` (see
    ``_write_sudoers_console_setenv``). Without it sudo does not merely drop
    the vars it cannot preserve, it refuses the whole command: the keys reach
    the policy as command-line ``env_add`` vars (the list form of
    ``--preserve-env`` does not set ``MODE_PRESERVE_ENV``), so sudoers runs
    ``validate_env_vars``, rejects every name outside ``env_keep``, and aborts.
    A pane that asked for the flag anyway would exit on spawn.

    So ask before committing to the flag. The probe sets its own var rather
    than reusing a composed key: it needs a name no ``env_keep`` pattern
    covers, and it must not depend on the composed env having reached this
    process (it has not, on non-SSH transports). It goes through ``env`` rather
    than a ``VAR=val cmd`` prefix because this string runs under the admin's
    login shell, which is operator-configurable and need not be POSIX (fish
    only took ``VAR=val cmd`` in 3.1; csh never did). ``-n`` keeps the probe
    from blocking on a password prompt if the admin's NOPASSWD grant is gone.

    Scoped to the setenv gate specifically. The probe runs as ``MODE_RUN`` just
    as the real ``sudo --login`` invocation does (``--login`` only adds the
    ``MODE_LOGIN_SHELL`` flag), so the ``!def_setenv`` check fires identically
    for both. It does not attempt to predict a sudoers that restricts *which
    commands* the admin may run as the agent; the admin holds
    ``ALL=(ALL) NOPASSWD:ALL``, so no such restriction exists to model.

    Probes and warns once per agent user per ``memo``, on the first miss.
    Building a console splits a pane per shell per session, all asking this
    same question of the same VM, so without the memo the probe round-trips
    per pane and an identical multi-line warning repeats until it buries the
    rest of the attach output. ``memo`` is required rather than defaulted for
    that reason: every caller either shares one across its splits or passes an
    empty one to say it has a single pane, and neither is a default we can
    guess correctly on their behalf.
    """
    from agentworks.vms.initializer import AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH

    # Lazy import: secrets_env imports attach at module level (for
    # _session_linux_user), and attach imports _build_console_tmux from this
    # module at module level, so a module-level import back to secrets_env
    # here would complete a three-way circular import.
    from .secrets_env import _SUDO_PRESERVE_PROBE_VAR

    if session_user in memo:
        return memo[session_user]

    probe = target.run(
        f"env {_SUDO_PRESERVE_PROBE_VAR}=1 sudo -n "
        f"--preserve-env={_SUDO_PRESERVE_PROBE_VAR} "
        f"-u {shlex.quote(session_user)} true",
        check=False,
    )
    memo[session_user] = probe.ok
    if not probe.ok:
        # Report what sudo said rather than diagnosing it. A refused `setenv`
        # is the expected cause, but the probe fails for any reason (unknown
        # agent user, sudo missing, a transport blip), and sudo's own text
        # already tells those apart.
        detail = probe.stderr.strip() or f"exit {probe.returncode}"
        output.warn(
            f"{vm.name}: agent-scope env and secrets will not reach console "
            f"shell panes for '{session_user}'. Preserving them across the "
            f"pane's sudo needs `Defaults:{admin_user} setenv` in "
            f"{AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH}, which VM init deploys; "
            f"if this VM predates it, `agw vm reinit {vm.name}` will add it. "
            f"sudo said: {detail}"
        )
    return probe.ok


def _split_shell_pane(
    target: Transport,
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
    preserve_memo: PreserveEnvMemo,
) -> str | None:
    """Split off one shell pane in an existing console window and tag the new
    pane with its position in the configured shell list. The tag lets
    restore-session detect which specific shell (out of an ordered list) is
    missing after an accidental kill.

    Env reaches the pane via:

    1. ``tmux split-window -e KEY=VAL`` flags (load-bearing): tmux sets
       these vars on the pane process before exec. In the agent-pane
       branch the pane then ``sudo --login``s to the agent user, which
       resets the environment. Two mechanisms carry vars across that
       crossing: agentworks-managed vars (``AGENTWORKS_*``, ``AW_*``)
       survive via the sudoers env_keep fragment, and arbitrarily-named
       operator env / secrets survive via ``sudo --preserve-env=<keys>``
       (permitted by the ``Defaults:<admin> setenv`` fragment). Both
       fragments are deployed by VM init. A VM without the setenv
       fragment refuses the ``--preserve-env`` command outright rather
       than dropping the vars, so ``_sudo_can_preserve_env`` asks first
       and we warn and fall back to a plain ``sudo --login`` (env_keep
       vars only) rather than hand back a pane that dies on spawn. See
       docs/adrs/0017-console-pane-preserve-env.md.
    2. SSH SetEnv on ``target.run`` (SSH transport only;
       non-SSH transports are a no-op because the tmux client is
       talking to an already-running server and the client's env
       doesn't flow into server-spawned panes). For SSH this is
       belt-and-suspenders in the rare case where the console tmux
       server has just been (re)started; in steady state channel (1)
       is what reaches the pane.

    Returns the new pane id on full success (split + tag both completed), or
    None if either step failed (tmux refused the split, or the pane was created
    but its id couldn't be captured so the tag couldn't be set). Callers in
    best-effort paths (`add_shell`, `_add_session_window`) may ignore the
    return value; `restore_session` checks each return so a partial restore
    is loud rather than a silent exit-0."""
    from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
    from agentworks.sessions.tmux import _tmux_env_flags

    cwd = shell["cwd"]
    full_path = posixpath.join(workspace_path, cwd) if cwd else workspace_path
    q_full = shlex.quote(full_path)
    q_con = shlex.quote(tmux_session_name(console_name))
    q_win = shlex.quote(window_name)
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
    env_flags = _tmux_env_flags(pane_env)

    # Login shell in both branches keeps profile/aliases consistent with the
    # session pane behavior (sessions use $SHELL -l via create_session).
    # Diagnostic on cd failure so a missing cwd shows the actual path.
    # The echo argument is shlex.quoted so paths containing shell metacharacters
    # (quotes, $(...), backticks) print literally rather than triggering
    # expansion. -P -F '#{pane_id}' makes split-window print the new pane's ID
    # to stdout so we can target set-option at that exact pane immediately after.
    q_diag = shlex.quote(f"cwd missing: {full_path}")
    if use_admin:
        bootstrap = f'cd {q_full} || echo {q_diag}; exec "$SHELL" -l'
        cmd = (
            f"tmux split-window -t {q_con}:{q_win} -P -F '#{{pane_id}}'{env_flags} -c {q_full} {shlex.quote(bootstrap)}"
        )
    else:
        q_user = shlex.quote(session_user)
        bootstrap = f'cd {q_full} || echo {q_diag}; exec "$SHELL" -l'
        # Carry the composed operator/secret env across the sudo boundary.
        # tmux set these vars on the pane process (env_flags, above), but
        # `sudo --login` resets the environment and would drop every var
        # except the AGENTWORKS_*/AW_* env_keep allowlist. --preserve-env
        # names the composed keys explicitly so arbitrarily-named agent-scope
        # vars and secrets survive. sudo reads their VALUES from its inherited
        # environment (tmux -e set them), not from its command line, so only
        # the names land on the SUDO argv; passing VAR=value to sudo instead
        # would respell the values there. (The values are already on the tmux
        # -e argv from env_flags above, a pre-existing exposure this does not
        # add to.) Honored because VM init grants the admin `Defaults:<admin>
        # setenv` (see _write_sudoers_console_setenv).
        #
        # Ask sudo first, because a VM without that fragment does not drop the
        # un-preservable vars: it refuses the whole command, which would exit
        # the pane process on spawn. Skip the question when there is no env to
        # preserve (nothing to ask about, and no empty `--preserve-env=`).
        # _sudo_can_preserve_env warns on a miss; a caller that splits panes in
        # a loop passes a memo so that happens once rather than per pane.
        preserve = ""
        if pane_env and _sudo_can_preserve_env(
            target,
            session_user=session_user,
            vm=vm,
            admin_user=admin_user,
            memo=preserve_memo,
        ):
            preserve = f" --preserve-env={shlex.quote(','.join(pane_env))}"
        pane_cmd = f"exec sudo --login{preserve} -u {q_user} bash -c {shlex.quote(bootstrap)}"
        cmd = (
            f"tmux split-window -t {q_con}:{q_win} -P -F '#{{pane_id}}'{env_flags} -c {q_full} {shlex.quote(pane_cmd)}"
        )

    res = target.run(cmd, check=False, env=pane_env)
    if not res.ok:
        output.warn(f"failed to add shell pane in '{window_name}': {res.stderr.strip()}")
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
    target: Transport,
    db: Database,
    registry: Registry,
    *,
    values: Mapping[str, str],
    console_name: str,
    member: ConsoleSessionRow,
    vm: VMRow,
    layout: str,
    preserve_memo: PreserveEnvMemo,
) -> None:
    """Create one session window in the console and attach its shell panes.

    Missing or off-VM sessions are skipped with a warning; this keeps the
    console attach functional even if a session has been deleted out from
    under it.
    """
    from agentworks.sessions.multi_console_layout import _apply_layout, _focus_session_pane

    # Lazy import: attach.py imports _build_console_tmux from this module at
    # load time, so a module-level import back here would be circular.
    from .attach import _attach_loop_wrapper, _session_linux_user

    session = db.get_session(member.session_name)
    if session is None:
        output.warn(
            f"session '{member.session_name}' is in console '{console_name}' but no longer exists; skipping window"
        )
        return
    workspace_path = _resolve_workspace_path(db, session)
    if workspace_path is None:
        output.warn(f"workspace for session '{member.session_name}' is missing; skipping window")
        return

    q_con = shlex.quote(tmux_session_name(console_name))
    q_session = shlex.quote(session.name)
    wrapper = _attach_loop_wrapper(session.name, session.socket_path)

    res = target.run(
        f"tmux new-window -t {q_con} -n {q_session} {shlex.quote(wrapper)}",
        check=False,
    )
    if not res.ok:
        output.warn(f"failed to add window for '{session.name}': {res.stderr.strip()}")
        return

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
            return
        for config_index, shell in enumerate(member.shells):
            _split_shell_pane(
                target,
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
                preserve_memo=preserve_memo,
            )
        _apply_layout(target, q_con, q_session, layout)
    # Focus the session pane so the operator lands on the attach output
    # rather than the most-recently-created shell pane. Done unconditionally
    # (cheap, and consistent across windows with and without shells).
    _focus_session_pane(target, q_con, q_session)


def _build_console_tmux(
    target: Transport,
    db: Database,
    registry: Registry,
    console: ConsoleRow,
    vm: VMRow,
    *,
    values: Mapping[str, str],
    layout: str,
) -> None:
    """Kill any existing tmux session, then rebuild it from current DB state."""
    # Lazy import: attach.py imports _build_console_tmux from this module at
    # load time, so a module-level import back here would be circular.
    from .attach import _kill_console_tmux

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
        # No sudo wrapper: the SSH user IS the admin user (direct
        # target-user SSH), so a login shell at the pane is the goal directly.
        target.run(
            f"tmux new-session -d -s {q_con} -n {shlex.quote(ADMIN_SHELL_WINDOW)} {shlex.quote('exec $SHELL -l')}"
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
        # A sub-step of attach_console's "Building/Rebuilding console..." line.
        output.detail(f"Adding {len(members)} session window(s) to console '{console.name}'...")
    # One memo for the whole build: every window's agent panes ask the same VM
    # the same question, so probe (and warn) once per agent user, not per pane.
    preserve_memo: PreserveEnvMemo = {}
    for member in members:
        _mc._add_session_window(
            target,
            db,
            registry,
            values=values,
            console_name=console.name,
            member=member,
            vm=vm,
            layout=layout,
            preserve_memo=preserve_memo,
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
            f"console '{console.name}' has no usable session windows; placeholder kept so the tmux session survives"
        )
