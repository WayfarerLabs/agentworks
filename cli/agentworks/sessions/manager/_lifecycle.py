"""Stop and restart operations (single and batch)."""

from __future__ import annotations

import contextlib
import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    BrokenStateError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
)
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT

if TYPE_CHECKING:
    from agentworks.agents.nodes import (
        LiveAgentNode,
    )
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport
from ._constants import _STOP_GRACE_SECONDS


def _execute_stop(
    targets: list[tuple[SessionRow, Transport, bool]],
    *,
    db: Database,
    force: bool = False,
    announce_stopped: bool = True,
) -> list[tuple[str, str]]:
    """Core stop logic: C-c all, single grace period, kill survivors.

    ``targets`` is ``[(session, target, target_owns_session)]``. When
    ``target_owns_session`` is True, the SSH user is the same uid that owns
    the tmux server (admin sessions over admin SSH, or agent sessions over
    agent SSH) and no sudo is needed for kill / socket cleanup. When False
    (admin SSH for an agent session in batch ops), sudo is needed.

    Handles both single and batch stops. Returns list of (name, error) failures.

    ``announce_stopped`` gates the per-session "Session 'x' stopped" body
    line. Batch stops keep it (the per-item outcome of a loop that has no
    single terminal); the single-session caller sets it False because it
    owns a column-0 ``result()`` terminal of its own, and the per-session
    body line would just duplicate it.
    """
    import time

    from agentworks.sessions.tmux import force_kill_tmux_server, send_keys

    if not targets:
        return []

    # Phase 1: send C-c to all sessions (best effort).
    # This gives processes that handle SIGINT gracefully (save state, flush)
    # a chance to clean up before we kill the session. In practice, tmux
    # kill-session sends SIGHUP which cascades through the shell to children,
    # so the C-c is rarely necessary. Consider removing the C-c + grace
    # period if the 5-second wait becomes a pain point.
    output.detail("Sending C-c to stop any running commands...")
    for session, target, _ in targets:
        sock = session.socket_path
        with contextlib.suppress(Exception):
            send_keys(session.name, "C-c", run_command=target.run, socket_path=sock)

    # Phase 2: single grace period
    output.detail(f"Waiting {_STOP_GRACE_SECONDS}s for graceful exit...")
    time.sleep(_STOP_GRACE_SECONDS)

    # Phase 3: check survivors per VM (reuse existing targets). Status checks
    # only read /proc; sudo not relevant here. Group by target identity for
    # one batch-check SSH per (VM, transport).
    by_target: dict[int, tuple[Transport, list[SessionRow]]] = {}
    for session, target, _ in targets:
        tid = id(target)
        if tid not in by_target:
            by_target[tid] = (target, [])
        by_target[tid][1].append(session)

    survivor_map: dict[str, SessionStatus] = {}
    for target, group in by_target.values():
        survivor_map.update(_mgr.batch_check_status(group, target=target))

    failed: list[tuple[str, str]] = []

    for session, target, target_owns_session in targets:
        # Cross-uid kill/cleanup (admin SSH against an agent session) needs
        # sudo. Same-uid ops do not.
        kill_sudo = not target_owns_session
        status = survivor_map.get(session.name)
        if status is None:
            # Status check failed (SSH error or parse issue) -- don't assume stopped
            failed.append((session.name, "could not verify session status after stop"))
            output.warn(f"Could not verify status of '{session.name}', not marking as stopped")
            continue
        if status == SessionStatus.OK or status == SessionStatus.BROKEN:
            output.detail(f"Killing session '{session.name}'")
            sock = session.socket_path
            killed = _mgr._kill_session(session.name, run_command=target.run, socket_path=sock)
            if not killed:
                # Race condition: session may have exited between survivor check and kill.
                # Recheck before treating as failure.
                recheck = _mgr.check_session_status(session, target=target)
                if recheck == SessionStatus.STOPPED:
                    pass  # session exited on its own, that's success
                elif force and session.socket_path is not None and session.pid and session.pid > 0:
                    # Escalate to PID kill for agent sessions only (admin shares PID)
                    output.detail(f"tmux kill failed for '{session.name}', force-killing PID {session.pid}")
                    if not force_kill_tmux_server(
                        session.pid,
                        target=target,
                        socket_path=session.socket_path,
                        log=output.detail,
                        use_sudo=kill_sudo,
                    ):
                        failed.append((session.name, f"PID {session.pid} survived force-kill"))
                        continue
                else:
                    failed.append((session.name, f"tmux kill-session failed for '{session.name}'"))
                    if session.socket_path is not None and session.pid and session.pid > 0:
                        output.warn(f"Failed to stop '{session.name}' (tmux unreachable, use --force)")
                    else:
                        output.warn(f"Failed to stop '{session.name}' (tmux unreachable)")
                    continue

        # Clean up agent socket only after confirming the server process is dead
        if (
            session.socket_path
            and session.socket_path.startswith(AGENT_SOCKET_ROOT + "/")
            and session.pid
            and session.pid > 0
            and not _mgr._pid_alive(session.pid, target=target)
        ):
            target.run(f"rm -f {shlex.quote(session.socket_path)}", sudo=kill_sudo, check=False)

        db.update_session_pid(session.name, PID_STOPPED)
        if announce_stopped:
            output.info(f"Session '{session.name}' stopped")

    return failed


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
) -> None:
    """Stop a running session. Sends C-c first, then kills after a grace period."""
    from agentworks.sessions.tmux import force_kill_tmux_server

    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(db, config, session, operation="session-stop") as (
        _ws,
        vm,
        _run_command,
        _run_as_root,
        admin_target,
    ):
        session = _mgr._ensure_pid(session, target=admin_target, db=db)
        status = _mgr.check_session_status(session, target=admin_target)

        if status == SessionStatus.STOPPED:
            output.info(f"Session '{name}' is already stopped")
            return
        # UNKNOWN is impossible here -- _ensure_pid raises on unresolvable sessions

        # Pick the destructive-op transport BEFORE doing anything destructive.
        # For agent sessions this also probes the agent's direct SSH so a
        # pre-rollout agent surfaces as an actionable StateError up front
        # rather than mid-kill. _build_session_target
        # always returns a same-uid target, so no sudo is needed for the
        # destructive ops below.
        target = _mgr._build_session_target(session, vm=vm, config=config, db=db, admin_target=admin_target)
        kill_sudo = False

        if status == SessionStatus.BROKEN:
            if not force:
                raise BrokenStateError(
                    f"session '{name}' is broken (PID alive but tmux unreachable).",
                    entity_kind="session",
                    entity_name=name,
                    hint="Use --force to kill the process.",
                )
            output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
            assert session.pid is not None
            killed = force_kill_tmux_server(
                session.pid,
                target=target,
                socket_path=session.socket_path,
                log=output.detail,
                use_sudo=kill_sudo,
            )
            if not killed:
                raise ExternalError(
                    f"failed to kill PID {session.pid} for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )
            db.update_session_pid(name, PID_STOPPED)
            output.result(f"Session '{name}' force-stopped")
            return

        # OK: delegate to shared stop logic. target_owns_session=True
        # because _build_session_target returned a same-uid target. The
        # anchor gives _execute_stop's internal detail lines a parent (the
        # batch caller emits its own "Stopping N session(s)..." anchor).
        # announce_stopped=False: this single-stop path owns the terminal
        # (the column-0 result() below), so the shared helper must not also
        # emit its per-session "stopped" body line and double it up.
        output.info(f"Stopping session '{name}'...")
        failed = _execute_stop([(session, target, True)], db=db, force=force, announce_stopped=False)
        if failed:
            raise ExternalError(
                f"failed to stop session '{name}': {failed[0][1]}",
                entity_kind="session",
                entity_name=name,
            )
        output.result(f"Session '{name}' stopped")


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Restart a session. Prompts if running (--yes to skip). --force for BROKEN.

    Orchestrated: the live graph derives from the session's rows, the
    activation gate replaces the imperative ensure_active + hold, and
    the preflight sweep fires the required-commands probe BEFORE the
    kill (a missing binary aborts with the old session still running).
    Nothing here is created, so no realization log exists; the window
    after the kill is deliberately non-rollbackable (no unwind is
    consulted there), exactly the imperative shape.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    registry = build_registry(config)

    session = _mgr._require_session(db, name)
    ws = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, ws)
    template = _mgr._resolve_template(registry, session.template)

    # ===== Build: the live node graph from the rows =========================
    #
    # Everything exists, so every node is live and nothing is realized
    # or unwound: the session row names its agent, workspace, and VM,
    # and the domain factories construct one node per row (the VM row's
    # site field is its edge to the vm-site node, which holds the
    # platform instance). Construction registers the site's declared
    # secrets on the resolver; nothing resolves yet.
    from agentworks.agents.nodes import live_agent_node
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.nodes import live_session_node
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import live_workspace_node

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)
    workspace_node = live_workspace_node(ws, vm_node)
    agent_node: LiveAgentNode | None = None
    if session.agent_name is not None:
        agent_row = db.get_agent(session.agent_name)
        if agent_row is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' (referenced by session '{session.name}') not found",
                entity_kind="agent",
                entity_name=session.agent_name,
            )
        agent_node = live_agent_node(agent_row, vm_node)
    session_node = live_session_node(
        session,
        template,
        agent=agent_node,
        workspace=workspace_node,
        vm=vm_node,
    )
    nodes = walk(session_node)
    # The walk supplies the boundary union (the site's config secrets;
    # live nodes declare nothing else). The session's env chain is
    # deliberately NOT part of this boundary: it resolves after the
    # BROKEN/confirm gates below, the recorded bail-before-prompt
    # exception.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws.name,
        session=name,
        agent=session.agent_name,
        admin=session.agent_name is None,
    )

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active hold: opened once, before the preflight
    # sweep, held through the whole command, its just-in-time values
    # seeding the boundary resolver.
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        if vm.tailscale_host is None:
            raise StateError(
                f"VM '{vm.name}' has no Tailscale address",
                entity_kind="vm",
                entity_name=vm.name,
            )

        from agentworks.ssh import SSHLogger

        logger = SSHLogger(vm.name, "session-restart")
        admin_target = _mgr.transport(vm, config, logger=logger)
        run_command: RunCommand = admin_target.run

        with output.section("Preflight"):
            session = _mgr._ensure_pid(session, target=admin_target, db=db)

            # Legacy migration: sessions predating the per-session-socket model
            # have ``socket_path=None`` (they lived on the admin's default tmux
            # server, where session.pid identifies the server, not this
            # session). ``check_session_status`` would raise a typed StateError
            # for these; instead we recognize the shape, run a surgical
            # ``tmux kill-session -t <name>`` on the default server (no socket
            # path), and fall through to the create step. The downstream
            # ``create_tmux_session`` produces a per-session socket and the
            # subsequent ``db.update_session_socket_path`` lands the migration.
            is_legacy = session.socket_path is None and session.pid is not None and session.pid > 0
            if is_legacy:
                output.info(
                    f"Session '{name}' uses the legacy default-tmux-server model; migrating to per-session socket."
                )
                status = SessionStatus.STOPPED  # placeholder; legacy branch owns the kill below
            else:
                status = _mgr.check_session_status(session, target=admin_target)

            # Pick the destructive-op transport BEFORE any destructive action.
            # For agent sessions this builds an agent Transport and probes it
            # so a pre-rollout agent surfaces as an actionable StateError up
            # front rather than leaving us with a stopped session we can't
            # restart. Same transport is used for kill (above) and create
            # (below): every destructive step on an agent session goes via
            # direct agent SSH. _build_session_target always returns a
            # same-uid target, so no sudo is needed for kill.
            is_admin = session.mode == SessionMode.ADMIN.value
            session_target = _mgr._build_session_target(session, vm=vm, config=config, db=db, admin_target=admin_target)
            session_run_command: RunCommand = session_target.run
            kill_sudo = False

            # PREFLIGHT-ALL over the walk rooted at the live session node,
            # against the one command-start context: the required-commands
            # check's target (an existing agent, or the admin) is realized,
            # so it probes NOW, pre-resolve and PRE-KILL, and a missing
            # binary aborts the restart with the old session still running.
            # Preflight is read-only (no prompt), so it stays ahead of the
            # gates below; both secret-resolving passes run AFTER them.
            preflight_all(
                nodes,
                RunContext(
                    config=config,
                    operation_scope=scope,
                    admin_target=admin_target,
                    agent_target=None if is_admin else session_target,
                ),
            )

            # Bail-before-prompt: refuse the operation up front in the cases
            # where the operator either lacks the right flag (BROKEN + no
            # --force) or declines the confirm (OK + interactive 'no'). BOTH
            # secret-resolving passes (the graph-union boundary resolve and
            # the env-chain resolve below) run AFTER these checks so a
            # refused or declined restart never prompts for secrets it was
            # about to discard.
            # UNKNOWN is impossible here (_ensure_pid raises on unresolvable
            # sessions). Legacy sessions short-circuit at ``status =
            # SessionStatus.STOPPED`` above, so neither gate fires for them;
            # migration is implicit in the operator's restart opt-in.
            if status == SessionStatus.BROKEN and not force:
                raise BrokenStateError(
                    f"session '{name}' is broken (PID alive but tmux unreachable).",
                    entity_kind="session",
                    entity_name=name,
                    hint="Use --force to restart.",
                )
            if status == SessionStatus.OK and not yes and not output.confirm(f"Session '{name}' is running. Restart?"):
                raise UserAbort("restart cancelled")

        with output.section("Resolving Secrets"):
            # The graph-union boundary resolve (pass 1). Placed AFTER the
            # gates above, symmetric with the env-chain pass below, so a
            # refused or declined restart never prompts. Gate-resolved values
            # are already seeded, so nothing resolves twice.
            resolver.resolve()
            # Capture the graph boundary union for the harness's op-start
            # context (matching the create path, which captures
            # ``resolver.values`` at its boundary). Inert for the built-in
            # shell harness (empty ``secret_refs()``), but keeps the restart
            # op ctx shape-correct for a future secret-declaring harness; the
            # env-chain resolve (``resolve_for_command`` below) is a SEPARATE
            # pass, not this graph union.
            graph_secret_values = resolver.values

            # Eager-prompting orchestration (pass 2): resolve every secret
            # referenced by this session's env chain BEFORE any kill /
            # destructive step. Non-interactive failures surface as
            # SecretUnavailableError with no partial state to clean up. This
            # is the recorded bail-before-prompt exception to the
            # one-boundary-resolve contract: the graph's union (the site's
            # config secrets) and this env chain BOTH resolve here, after the
            # BROKEN/--force refusal and the "Restart?" confirm, so a declined
            # restart never prompts for secrets it was about to discard.
            # Folding the env chain into the boundary would trade that
            # operator protection for one fewer prompt session on proxmox
            # only.

            from agentworks.secrets import resolve_for_command

            secret_values = resolve_for_command(
                [
                    _mgr._session_secret_target(
                        registry,
                        db=db,
                        vm=vm,
                        ws=ws,
                        session_name=name,
                        session_template=template,
                        mode=SessionMode(session.mode),
                        agent_name=session.agent_name,
                    ),
                ],
                config,
                registry,
            )

        with output.section("Starting Session"):
            output.info(f"Restarting session '{name}'...")

            if is_legacy:
                # Surgical kill of the named session on the default tmux
                # server (no socket path). ``session.pid`` identifies the
                # SERVER for legacy admin rows, not this session, so the
                # BROKEN path's ``force_kill_tmux_server(pid)`` would nuke
                # every other tmux session sharing the server -- including
                # ad-hoc tmux work and other legacy Agentworks rows. The
                # ``kill-session -t <name>`` primitive is surgical. Failure
                # is best-effort: if the session is already gone (only the
                # DB row survived), kill returns False and we proceed to
                # create the new shape.
                _mgr._kill_session(name, run_command=session_run_command, socket_path=None)
            elif status == SessionStatus.BROKEN:
                from agentworks.sessions.tmux import force_kill_tmux_server

                output.warn(f"Session '{name}' is broken (tmux unreachable), force-killing via PID")
                assert session.pid is not None
                killed = force_kill_tmux_server(
                    session.pid,
                    target=session_target,
                    socket_path=session.socket_path,
                    log=output.detail,
                    use_sudo=kill_sudo,
                )
                if not killed:
                    raise ExternalError(
                        f"failed to kill PID {session.pid} for session '{name}'",
                        entity_kind="session",
                        entity_name=name,
                    )
            elif status == SessionStatus.OK:
                # Confirm already happened above (before eager-resolve), so we
                # know the operator opted in.
                sock = session.socket_path
                if not _mgr._kill_session(name, run_command=session_run_command, socket_path=sock):
                    raise ExternalError(
                        f"failed to stop session '{name}' for restart",
                        entity_kind="session",
                        entity_name=name,
                    )

            deploy_restricted_config(run_command, history_limit=config.session.history_limit)

            # Op-start RunContext for the harness's restart op, assembled
            # AFTER the kill (a state-aware harness decides resume-vs-launch
            # with the old process already dead). Mirrors the preflight
            # readiness ctx above (the restart path builds no runup ctx), plus
            # the scoped graph secrets (empty for the built-in shell harness).
            # Template-var substitution wraps the returned string; restart
            # sources ``workspace_name`` from the session row, as the interim
            # path did.
            restart_ctx = RunContext(
                config=config,
                operation_scope=scope,
                admin_target=admin_target,
                agent_target=None if is_admin else session_target,
                secrets=ScopedSecrets(graph_secret_values, session_node.secret_refs()),
            )
            command = _mgr._substitute_template_vars(
                session_node.harness.restart(restart_ctx),
                {"session_name": name, "workspace_name": session.workspace_name},
            )
            if (note := session_node.harness.launch_note()) is not None:
                output.detail(note)
            # Persist the harness's state blob after the op (mirrors the
            # create-path insert). Usually a no-op (the value was stored on
            # create), but a session predating the harness_state column
            # (backfilled to {}) mints its id on this first restart. Persisting
            # BEFORE create_tmux_session is intentional: a stable id that
            # survives a tmux-recreate retry beats re-minting a new one each
            # attempt (the id is the session's, whether or not the pane came up).
            db.update_session_harness_state(name, session_node.harness.state)
            linux_user = _mgr._resolve_session_linux_user(db, session, vm)
            session_env = _mgr._resolve_session_env(
                registry,
                values=secret_values,
                db=db,
                vm=vm,
                ws=ws,
                session_name=name,
                session_template=template,
                mode=SessionMode(session.mode),
                agent_name=session.agent_name,
                linux_user=linux_user,
            )

            try:
                new_sock, pid = create_tmux_session(
                    name,
                    ws.workspace_path,
                    command,
                    linux_user,
                    run_command=session_run_command,
                    target=admin_target,
                    admin_username=vm.admin_username,
                    is_admin=is_admin,
                    env=session_env,
                )
            except RuntimeError as exc:
                if "already has an active tmux server" in str(exc):
                    raise StateError(
                        f"session '{name}' has an active tmux server that was not detected by the status check.",
                        entity_kind="session",
                        entity_name=name,
                        hint="Use 'session stop --force' to kill it, then retry.",
                    ) from exc
                raise

            # Persist socket path if it differs from what's stored.
            if new_sock != session.socket_path:
                db.update_session_socket_path(name, new_sock)
            if pid is not None:
                # boot_id is /proc/sys/kernel/random/boot_id (world-readable);
                # admin's target is fine and convenient.
                boot_id = _mgr._get_boot_id(admin_target)
                if boot_id is not None:
                    db.update_session_pid(name, pid, boot_id=boot_id)
                else:
                    output.warn(f"Could not read boot ID for session '{name}', PID not stored")
            else:
                output.warn(f"Could not capture PID for session '{name}', will auto-repair on next access")

        output.result(f"Session '{name}' restarted")

        _mgr._regenerate_tmuxinator(db, config, vm, ws)
        # Don't re-add the session to the legacy vm-console here. The existing
        # window's wrapper polls the session's socket indefinitely and re-attaches
        # when the new tmux server comes back. Adding a new window here would
        # create a duplicate.


def stop_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    force: bool = False,
) -> None:
    """Stop all running sessions, optionally filtered by VM, workspace, agent, and/or mode.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    sessions = _mgr.filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered session set and open the
    # batch boundary + per-VM gates BEFORE the SSH probes. The probes
    # (ensure_pids_batch, batch_check_all_sessions) issue per-VM
    # round-trips; on WSL2 they would race the idle timer without the
    # held-active anchor (a no-op hold on other platforms).
    distinct_vms = _mgr._distinct_vms_for_sessions(db, sessions)
    with _mgr._batch_vm_boundary(db, config, distinct_vms):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = _mgr.ensure_pids_batch(sessions, db=db, config=config)
        status_map = _mgr.batch_check_all_sessions(sessions, db=db, config=config)

        # Error if any sessions are still unknown after auto-repair.
        # PID_STOPPED sessions are known-stopped (excluded from status_map by design).
        unknown = [
            s
            for s in sessions
            if s.pid != PID_STOPPED and (s.pid is None or s.boot_id is None or s.name not in status_map)
        ]
        if unknown:
            names = ", ".join(s.name for s in unknown)
            raise StateError(
                f"{len(unknown)} session(s) have unknown status after auto-repair ({names}).",
                hint="Resolve the listed sessions manually before retrying.",
            )

        broken = [s for s in sessions if status_map.get(s.name) == SessionStatus.BROKEN]
        if broken and not force:
            names = ", ".join(s.name for s in broken)
            output.warn(f"Skipping {len(broken)} broken session(s) ({names}). Use --force to kill.")

        ok_statuses = {SessionStatus.OK}
        if force:
            ok_statuses.add(SessionStatus.BROKEN)
        alive_sessions = [s for s in sessions if status_map.get(s.name) in ok_statuses]

        if not alive_sessions:
            output.info("No running sessions to stop.")
            return

        output.info(f"Stopping {len(alive_sessions)} session(s)...")

        # Resolve VM targets (reuse across sessions on the same VM)
        vm_targets: dict[str, Transport] = {}
        for s in alive_sessions:
            ws = db.get_workspace(s.workspace_name)
            if ws and ws.vm_name not in vm_targets:
                vm = db.get_vm(ws.vm_name)
                if vm and vm.tailscale_host:
                    vm_targets[ws.vm_name] = _mgr.transport(vm, config)

        # Build (session, target, target_owns_session) tuples for _execute_stop.
        # Batch ops keep admin's target across all sessions for efficiency
        # (carve-out): admin's path into agent tmux servers requires
        # sudo. target_owns_session is True only for admin's own sessions.
        stop_targets: list[tuple[SessionRow, Transport, bool]] = []
        for s in alive_sessions:
            ws = db.get_workspace(s.workspace_name)
            if ws and ws.vm_name in vm_targets:
                target_owns_session = s.mode == SessionMode.ADMIN.value
                stop_targets.append((s, vm_targets[ws.vm_name], target_owns_session))

        failed = _execute_stop(stop_targets, db=db, force=force)
        if failed:
            raise ExternalError(f"{len(failed)} session(s) failed to stop.")


def restart_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
    include_running: bool = False,
    force: bool = False,
) -> None:
    """Restart sessions, optionally filtered by VM, workspace, agent, and/or mode.

    With include_running=False (--all-stopped), only stopped sessions are
    restarted. With include_running=True (--all), all sessions are targeted;
    if any are running, the caller should have prompted or passed yes=True.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    sessions = _mgr.filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered set and anchor them BEFORE the
    # SSH probes. Each restart_session call also opens its own gate span;
    # the redundant inner gate is a no-op on already-active VMs and a cheap
    # extra subprocess on WSL2 (accepted, see PR description).
    distinct_vms = _mgr._distinct_vms_for_sessions(db, sessions)

    failed: list[tuple[str, str]] = []
    with _mgr._batch_vm_boundary(db, config, distinct_vms):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = _mgr.ensure_pids_batch(sessions, db=db, config=config)
        status_map = _mgr.batch_check_all_sessions(sessions, db=db, config=config)

        # Error if any sessions are still unknown after auto-repair.
        # PID_STOPPED sessions are known-stopped (excluded from status_map by design).
        # Legacy sessions (``socket_path is None``) are also excluded from
        # status_map by ``batch_check_status``; restart_session migrates them
        # to the new model, so don't treat them as "unknown" here.
        unknown = [
            s
            for s in sessions
            if s.pid != PID_STOPPED
            and s.socket_path is not None
            and (s.pid is None or s.boot_id is None or s.name not in status_map)
        ]
        if unknown:
            names = ", ".join(s.name for s in unknown)
            raise StateError(
                f"{len(unknown)} session(s) have unknown status after auto-repair ({names}).",
                hint="Resolve the listed sessions manually before retrying.",
            )

        if not include_running:
            # Only stopped sessions. Legacy sessions are alive-ish (PID set,
            # socket_path None) -- we can't tell whether they're stopped
            # from status_map alone (batch_check_status skips them), so we
            # filter them out under ``--all-stopped`` and tell the operator
            # how to migrate (``--all``). The batch_check_status warning
            # already named them; this second message ties that warning to
            # an actionable next step from the command they just ran.
            legacy_skipped = [s.name for s in sessions if s.socket_path is None and s.pid is not None and s.pid > 0]
            if legacy_skipped:
                names = ", ".join(legacy_skipped)
                output.warn(
                    f"Skipping {len(legacy_skipped)} legacy session(s) under "
                    f"--all-stopped (can't determine state without a per-session "
                    f"socket). Use `--all` to migrate them: {names}"
                )
            sessions = [s for s in sessions if s.pid == PID_STOPPED or status_map.get(s.name) == SessionStatus.STOPPED]

        if not sessions:
            output.info("No matching sessions to restart.")
            return

        output.info(f"Restarting {len(sessions)} session(s)...")

        for session in sessions:
            try:
                restart_session(db, config, name=session.name, force=force, yes=include_running)
            except UserAbort:
                # A confirm-cancellation aborts the whole batch operation, not
                # just this one session. Propagate so the outer wrapper renders
                # "Aborted." once and exits.
                raise
            except BrokenStateError as exc:
                if not force:
                    output.warn(f"Skipping '{session.name}': {exc}")
                else:
                    failed.append((session.name, str(exc)))
                    output.warn(f"Error restarting '{session.name}': {exc}")
            except StateError as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error restarting '{session.name}': {exc}")
            except Exception as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error restarting '{session.name}': {exc}")

    if failed:
        raise ExternalError(f"{len(failed)} session(s) failed to restart.")
