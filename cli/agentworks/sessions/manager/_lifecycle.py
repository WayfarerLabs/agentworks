"""Session start, stop, and restart operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.capabilities.harness_integration import HarnessLaunchIntent, require_implemented_start
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    BrokenStateError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)

if TYPE_CHECKING:
    from agentworks.agents.nodes import (
        LiveAgentNode,
    )
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.template import SessionTemplate
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport


def _requested_launch_intent(*, force_new: bool, resume_only: bool) -> HarnessLaunchIntent:
    """Translate the service flags into one mutually exclusive launch policy."""
    if force_new and resume_only:
        raise ValidationError("--resume-only and --force-new are mutually exclusive")
    if force_new:
        return HarnessLaunchIntent.FORCE_NEW
    if resume_only:
        return HarnessLaunchIntent.RESUME_ONLY
    return HarnessLaunchIntent.RESUME_OR_NEW


def _mark_stopped(db: Database, session: SessionRow) -> None:
    db.update_session_runtime(
        session.name,
        socket_path=session.socket_path,
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


def _mark_runtime_unknown(db: Database, session: SessionRow, *, socket_path: str) -> None:
    """Persist an addressable runtime whose process identity is not yet known."""
    db.update_session_runtime(
        session.name,
        socket_path=socket_path,
        pid=None,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


def _teardown_legacy_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
) -> None:
    """Destroy one exact session on a reachable legacy shared tmux server."""
    from agentworks.sessions.tmux import (
        ProbeStatus,
        kill_session,
        probe_tmux_session,
        probe_tmux_session_after_teardown,
    )

    def run_runtime(command: str, *, check: bool = True, env: dict[str, str] | None = None) -> object:
        return target.run(command, sudo=not target_owns_session, check=check, env=env)

    presence = probe_tmux_session(session.name, run_command=run_runtime, socket_path=None)
    if presence is ProbeStatus.UNKNOWN:
        raise ExternalError(
            f"could not determine legacy runtime state for session '{session.name}'",
            entity_kind="session",
            entity_name=session.name,
        )
    if presence is ProbeStatus.PRESENT:
        kill_session(session.name, run_command=run_runtime, socket_path=None)
        presence = probe_tmux_session_after_teardown(session.name, run_command=run_runtime, socket_path=None)
    if presence is not ProbeStatus.ABSENT:
        raise ExternalError(
            f"failed to verify legacy session '{session.name}' stopped",
            entity_kind="session",
            entity_name=session.name,
        )
    _mark_stopped(db, session)


def _teardown_session(
    session: SessionRow,
    *,
    target: Transport,
    target_owns_session: bool,
    db: Database,
    force: bool,
) -> None:
    """Destroy one managed session runtime and verify its absence."""
    if session.pid == PID_STOPPED:
        return
    if session.socket_path is None:
        _teardown_legacy_session(
            session,
            target=target,
            target_owns_session=target_owns_session,
            db=db,
        )
        return
    from agentworks.sessions.manager._teardown import teardown_dedicated_session

    teardown_dedicated_session(
        session,
        target=target,
        target_owns_session=target_owns_session,
        db=db,
        force=force,
    )


def _execute_stop(
    targets: list[tuple[SessionRow, Transport, bool]],
    *,
    db: Database,
    force: bool = False,
    announce_stopped: bool = True,
) -> list[tuple[str, str]]:
    """Apply the one verified teardown authority to several sessions.

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
    if not targets:
        return []

    failed: list[tuple[str, str]] = []
    for session, target, target_owns_session in targets:
        try:
            _teardown_session(
                session,
                target=target,
                target_owns_session=target_owns_session,
                db=db,
                force=force,
            )
            if announce_stopped:
                output.info(f"Session '{session.name}' stopped")
        except Exception as exc:
            failed.append((session.name, str(exc)))

    return failed


def stop_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Stop a session through its exact tmux ownership boundary."""

    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(
        db,
        config,
        session,
        operation="session-stop",
        interaction=interaction,
    ) as (
        _ws,
        vm,
        _run_command,
        _run_as_root,
        admin_target,
    ):
        legacy = session.socket_path is None and session.pid is not None and session.pid > 0
        if legacy:
            status = SessionStatus.RUNNING
        else:
            session = _mgr._ensure_pid(session, target=admin_target, db=db)
            status = _mgr.check_session_status(session, target=admin_target)

        if status == SessionStatus.STOPPED:
            output.info(f"Session '{name}' is already stopped")
            return
        if status == SessionStatus.UNKNOWN:
            raise StateError(
                f"session '{name}' runtime state is unknown",
                entity_kind="session",
                entity_name=name,
                hint="Retry after transport access is reliable; no runtime was changed.",
            )

        # Pick the destructive-op transport BEFORE doing anything destructive.
        # For agent sessions this also probes the agent's direct SSH so a
        # pre-rollout agent surfaces as an actionable StateError up front
        # rather than mid-kill. _build_session_target
        # always returns a same-uid target, so no sudo is needed for the
        # destructive ops below.
        target = _mgr._build_session_target(session, vm=vm, config=config, db=db, admin_target=admin_target)
        if status == SessionStatus.BROKEN and not force:
            raise BrokenStateError(
                f"session '{name}' is broken (tmux unreachable).",
                entity_kind="session",
                entity_name=name,
                hint="Use --force only after the prior server has exited.",
            )

        # Running: delegate to shared stop logic. target_owns_session=True
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


def _launch_existing_session(
    db: Database,
    config: Config,
    *,
    name: str,
    replace_running: bool,
    force: bool = False,
    intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start a stopped session, optionally replacing a running runtime.

    Orchestrated: the live graph derives from the session's rows, the
    activation gate replaces the imperative ensure_active + hold, and
    the preflight sweep fires the required-commands probe BEFORE the
    kill (a missing binary aborts with the old session still running).
    Nothing here is created, so no realization log exists; the window
    after the kill is deliberately non-rollbackable (no unwind is
    consulted there), exactly the imperative shape.
    """
    from agentworks.bootstrap import load_request_registry
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    registry = load_request_registry(config, live_database=db)

    session = _mgr._require_session(db, name)
    ws = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, ws)
    template = _mgr._resolve_template(
        registry,
        session.template,
        db=db,
        instance_name=session.name,
    )
    from typing import cast

    from agentworks.instance_specs import ensure_effective_references_enabled, get_instance_overlay
    from agentworks.sessions.template import effective_references, validate_effective_harness
    from agentworks.sessions.templates import resolve_template_with_provenance

    stored_overlay = get_instance_overlay(db, "session", session.name)
    if stored_overlay is not None:
        layered_template = resolve_template_with_provenance(
            registry,
            session.template,
            overlay=cast("SessionTemplate", stored_overlay.declaration),
            instance_name=session.name,
        )
        template = layered_template.value
        ensure_effective_references_enabled(
            registry,
            effective_references(template, ("session", session.name), layered_template.provenance),
        )
        validate_effective_harness(
            template,
            ("session", session.name),
            layered_template.provenance,
        )

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

    resolver = Resolver(config, registry, interaction=interaction)

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
    # Gate a disabled plugin harness_integration at USE (R14, the secret model): a live
    # session on a disabled harness_integration refuses to start or restart with the
    # enable-plugin error. The gate lives at this call site (not inside
    # ``live_session_node``, which threads no registry); a drift guard pins that
    # every caller of the node factory gates.
    from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
    from agentworks.resources.access import ensure_recipe_enabled

    ensure_harness_integration_enabled(registry, template.harness_integration)
    # Refuse a session-template recipe drawing on a disabled plugin's declarable
    # resource before start or restart (Phase 7, LLD b). Drift guard:
    # tests/agents/test_recipe_gate_drift.py.
    ensure_recipe_enabled(registry, "session-template", template.name)
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
    # status and operation-policy gates below, the recorded bail-before-prompt
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
    from agentworks.vms.manager import require_vm_ssh_boundary

    require_vm_ssh_boundary(db, config, vm)
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        if vm.tailscale_host is None:
            raise StateError(
                f"VM '{vm.name}' has no Tailscale address",
                entity_kind="vm",
                entity_name=vm.name,
            )

        from agentworks.ssh import SSHLogger

        operation = "restart" if replace_running else "start"
        logger = SSHLogger(vm.name, f"session-{operation}")
        admin_target = _mgr.transport(vm, config, logger=logger)
        run_command: RunCommand = admin_target.run

        with output.section("Preflight"):
            is_legacy = session.socket_path is None and session.pid is not None and session.pid > 0
            if not is_legacy:
                session = _mgr._ensure_pid(session, target=admin_target, db=db)

            # Legacy migration: sessions predating the per-session-socket model
            # have ``socket_path=None`` (they lived on the admin's default tmux
            # server, where session.pid identifies the server, not this
            # session). ``check_session_status`` would raise a typed StateError
            # for these; instead we recognize the shape, run a surgical
            # ``tmux kill-session -t <name>`` on the default server (no socket
            # path), and fall through to the create step. The downstream
            # ``create_tmux_session`` produces a per-session socket and the
            # subsequent atomic runtime update lands the migration.
            if is_legacy:
                output.info(
                    f"Session '{name}' uses the legacy default-tmux-server model; migrating to per-session socket."
                )
                status = SessionStatus.STOPPED  # placeholder; legacy branch owns the kill below
            else:
                status = _mgr.check_session_status(session, target=admin_target)

            is_admin = session.mode == SessionMode.ADMIN.value
            if status == SessionStatus.UNKNOWN:
                raise StateError(
                    f"session '{name}' runtime state is unknown",
                    entity_kind="session",
                    entity_name=name,
                    hint="Retry after transport access is reliable; no runtime was changed.",
                )
            if status == SessionStatus.RUNNING and not replace_running:
                if intent is HarnessLaunchIntent.FORCE_NEW:
                    raise StateError(
                        f"session '{name}' is already running",
                        entity_kind="session",
                        entity_name=name,
                        hint=f"Run `agw session restart {name} --force-new` to replace it.",
                    )
                output.result(f"Session '{name}' is already running")
                return

            # Only a launch path needs the owning transport. In particular, an
            # resume-or-new or resume-only start no-op and the running
            # --force-new refusal return above
            # without probing direct agent SSH.
            session_target = _mgr._build_session_target(
                session,
                vm=vm,
                config=config,
                db=db,
                admin_target=admin_target,
            )
            session_run_command: RunCommand = session_target.run

            if status == SessionStatus.BROKEN and not force:
                raise BrokenStateError(
                    f"session '{name}' is broken (PID alive but tmux unreachable).",
                    entity_kind="session",
                    entity_name=name,
                    hint=f"Use `agw session {operation} {name} --force` to recover it.",
                )

            # PREFLIGHT-ALL over the walk rooted at the live session node,
            # against the one command-start context: the required-commands
            # check's target (an existing agent, or the admin) is realized,
            # so it probes NOW, pre-resolve and PRE-KILL, and a missing
            # binary aborts the launch with the old session still running.
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
                registry=registry,
                interaction=interaction,
            )

        with output.section("Resolving Secrets"):
            # The graph-union boundary resolve (pass 1). Placed AFTER the
            # gates above, symmetric with the env-chain pass below, so a
            # refused launch never prompts. Gate-resolved values
            # are already seeded, so nothing resolves twice.
            resolver.resolve()
            # Capture the graph boundary union for the harness_integration's op-start
            # context (matching the create path, which captures
            # ``resolver.values`` at its boundary). Inert for the built-in
            # shell harness_integration (empty ``config_secret_refs()``), but keeps the start
            # context shape-correct for a future secret-declaring harness integration; the
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
            # BROKEN/--force refusal, so a refused launch never prompts for
            # secrets it was about to discard.
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
                allow_transient_auto_declare=True,
                interaction=interaction,
            )

        # Ask the harness for its launch decision before any teardown. A
        # strict resume failure or an unsupported intent therefore leaves an
        # existing runtime intact. Stateful integrations decide from their
        # durable target state, not from whether the old tmux process is live.
        start_ctx = RunContext(
            config=config,
            operation_scope=scope,
            admin_target=admin_target,
            agent_target=None if is_admin else session_target,
            secrets=ScopedSecrets(graph_secret_values, session_node.secret_refs()),
        )
        harness_start = require_implemented_start(
            session_node.harness_integration.start(start_ctx, intent=intent),
            intent=intent,
            harness_integration_name=template.harness_integration,
            session_name=name,
        )
        command = _mgr._substitute_template_vars(
            harness_start.command,
            {"session_name": name, "workspace_name": session.workspace_name},
        )

        with output.section("Starting Session"):
            output.info(f"{operation.title()}ing session '{name}'...")

            if is_legacy or status in {SessionStatus.RUNNING, SessionStatus.RESIDUAL, SessionStatus.BROKEN}:
                _teardown_session(
                    session,
                    target=session_target,
                    target_owns_session=True,
                    db=db,
                    force=force,
                )

            deploy_restricted_config(run_command, history_limit=config.session.history_limit)

            if harness_start.note is not None:
                output.detail(harness_start.note)
            # Persist the node's FULL namespaced harness_integration_state blob after the
            # op (mirrors the create-path insert): the harness_integration mutated its own
            # namespace in place, and persisting the full blob keeps foreign
            # harness integrations' namespaces intact across a template's
            # harness_integration switch.
            # Usually a no-op (the value was stored on create), but a session
            # predating the harness_integration_state column (backfilled to {}) mints its
            # id on this first launch. Persisting BEFORE create_tmux_session
            # is intentional: a stable id that survives a tmux-recreate retry
            # beats re-minting a new one each attempt (the id is the
            # session's, whether or not the pane came up).
            db.update_session_harness_integration_state(name, session_node.harness_integration_state)
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
            from agentworks.sessions.tmux import (
                ProbeStatus,
                capture_tmux_server_fingerprint,
                kill_server_and_probe,
            )

            try:
                db.record_session_started(name)
            except (KeyboardInterrupt, Exception):
                cleanup = kill_server_and_probe(run_command=session_run_command, socket_path=new_sock)
                if cleanup is not ProbeStatus.ABSENT:
                    _mark_runtime_unknown(db, session, socket_path=new_sock)
                    output.warn(f"session '{name}' may still have a runtime at socket {new_sock}")
                raise

            fingerprint_probe = capture_tmux_server_fingerprint(
                target=session_target,
                socket_path=new_sock,
            )
            fingerprint = fingerprint_probe.fingerprint
            if (
                fingerprint_probe.status is not ProbeStatus.PRESENT
                or fingerprint is None
                or (pid is not None and fingerprint.pid != pid)
            ):
                cleanup = kill_server_and_probe(run_command=session_run_command, socket_path=new_sock)
                if cleanup is ProbeStatus.ABSENT:
                    _mark_stopped(db, session)
                else:
                    _mark_runtime_unknown(db, session, socket_path=new_sock)
                raise ExternalError(
                    f"could not capture a stable tmux server fingerprint for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                    hint=(
                        f"The session row and socket {new_sock} were retained because "
                        "runtime absence could not be proved."
                        if cleanup is not ProbeStatus.ABSENT
                        else None
                    ),
                )
            db.update_session_runtime(
                name,
                socket_path=new_sock,
                pid=fingerprint.pid,
                boot_id=fingerprint.boot_id,
                tmux_server_start_ticks=fingerprint.start_ticks,
            )

        output.result(f"Session '{name}' {operation}ed")

        _mgr._regenerate_tmuxinator(db, config, vm, ws)


def stop_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
    force: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Stop all running sessions, optionally filtered by VM, workspace, agent, console, or mode.

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
        console_name=console_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered session set and open the
    # batch boundary + per-VM gates BEFORE the SSH probes. The probes
    # (ensure_pids_batch, observe_session_statuses) issue per-VM
    # round-trips; on WSL2 they would race the idle timer without the
    # held-active anchor (a no-op hold on other platforms).
    distinct_vms = _mgr._distinct_vms_for_sessions(db, sessions)
    with _mgr._batch_vm_boundary(db, config, distinct_vms, interaction=interaction):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = _mgr.ensure_pids_batch(sessions, db=db, config=config)
        status_map = _mgr.observe_session_statuses(sessions, db=db, config=config)

        # Error if any actionable sessions are still unknown after auto-repair.
        # The observer reports PID_STOPPED rows too; lifecycle omits them from
        # this refusal set because it does not need to act on them.
        legacy_names = {
            session.name
            for session in sessions
            if session.socket_path is None and session.pid is not None and session.pid > 0
        }
        unknown = [
            s
            for s in sessions
            if s.pid != PID_STOPPED
            and s.name not in legacy_names
            and (
                s.pid is None
                or s.boot_id is None
                or status_map.get(s.name, SessionStatus.UNKNOWN) is SessionStatus.UNKNOWN
            )
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

        active_statuses = {SessionStatus.RUNNING, SessionStatus.RESIDUAL}
        if force:
            active_statuses.add(SessionStatus.BROKEN)
        alive_sessions = [s for s in sessions if s.name in legacy_names or status_map.get(s.name) in active_statuses]

        if not alive_sessions:
            output.info("No running sessions to stop.")
            return

        output.info(f"Stopping {len(alive_sessions)} session(s)...")

        from agentworks.sessions.manager._teardown import (
            DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
            DedicatedTeardownPlan,
            execute_concurrent_dedicated_teardowns,
            prepare_concurrent_dedicated_teardown,
        )

        concurrent_plans: list[DedicatedTeardownPlan] = []
        serial_targets: list[tuple[SessionRow, Transport, bool]] = []
        serial_vm_targets: dict[str, Transport] = {}
        failed: list[tuple[str, str]] = []
        for session in alive_sessions:
            try:
                ws = _mgr._require_workspace(db, session.workspace_name)
                vm = _mgr._require_vm_for_workspace(db, ws)
                target_owns_session = session.mode == SessionMode.ADMIN.value
                complete_dedicated = (
                    session.socket_path is not None
                    and session.pid is not None
                    and session.boot_id is not None
                    and session.tmux_server_start_ticks is not None
                )
                if complete_dedicated:
                    plan_target = _mgr.transport(
                        vm,
                        config,
                        default_timeout=DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
                    )
                    plan = prepare_concurrent_dedicated_teardown(
                        db,
                        session,
                        vm_name=vm.name,
                        target=plan_target,
                        target_owns_session=target_owns_session,
                        force=force,
                    )
                    assert plan is not None
                    concurrent_plans.append(plan)
                    continue

                if vm.name not in serial_vm_targets:
                    serial_vm_targets[vm.name] = _mgr.transport(vm, config)
                serial_targets.append((session, serial_vm_targets[vm.name], target_owns_session))
            except Exception as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Session '{session.name}' failed teardown preparation: {exc}")

        failed.extend(execute_concurrent_dedicated_teardowns(concurrent_plans, db=db))
        serial_failures = _execute_stop(serial_targets, db=db, force=force)
        for session_name, error in serial_failures:
            output.warn(f"Session '{session_name}' failed to stop: {error}")
        failed.extend(serial_failures)
        if failed:
            raise ExternalError(f"{len(failed)} session(s) failed to stop.")


def _launch_all_sessions(
    db: Database,
    config: Config,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
    replace_running: bool,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start sessions, optionally replacing running runtimes.

    Batch start selects stopped sessions plus states required by explicit
    modifiers; restart considers every match and replaces running runtimes.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``agent_name``
    and ``admin_only`` are mutually exclusive; the caller enforces
    the mutex.
    """
    intent = _requested_launch_intent(force_new=force_new, resume_only=resume_only)
    sessions = _mgr.filter_sessions(
        db,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        console_name=console_name,
        admin_only=admin_only,
    )

    # Resolve distinct VMs from the filtered set and anchor them BEFORE the
    # SSH probes. Each singular launch also opens its own gate span;
    # the redundant inner gate is a no-op on already-active VMs and a cheap
    # extra subprocess on WSL2 (accepted, see PR description).
    distinct_vms = _mgr._distinct_vms_for_sessions(db, sessions)

    failed: list[tuple[str, str]] = []
    with _mgr._batch_vm_boundary(db, config, distinct_vms, interaction=interaction):
        # Auto-repair NULL-PID sessions, then batch check
        sessions = _mgr.ensure_pids_batch(sessions, db=db, config=config)
        status_map = _mgr.observe_session_statuses(sessions, db=db, config=config)

        # Error if any actionable sessions are still unknown after auto-repair.
        # The observer reports PID_STOPPED rows too; lifecycle omits them from
        # this refusal set because it does not need to act on them.
        # Legacy sessions remain UNKNOWN in batch observation. Exclude them
        # from this refusal so unfiltered batch restart can migrate them in the
        # singular launcher. Named start also migrates directly; batch start's
        # observed-status selection below omits them.
        unknown = [
            s
            for s in sessions
            if s.pid != PID_STOPPED
            and s.socket_path is not None
            and (
                s.pid is None
                or s.boot_id is None
                or status_map.get(s.name, SessionStatus.UNKNOWN) is SessionStatus.UNKNOWN
            )
        ]
        if unknown:
            names = ", ".join(s.name for s in unknown)
            raise StateError(
                f"{len(unknown)} session(s) have unknown status after auto-repair ({names}).",
                hint="Resolve the listed sessions manually before retrying.",
            )

        if not replace_running:
            start_statuses = {SessionStatus.STOPPED}
            if force:
                start_statuses.add(SessionStatus.BROKEN)
            if force_new:
                start_statuses.add(SessionStatus.RUNNING)
            sessions = [s for s in sessions if status_map.get(s.name) in start_statuses]

        if not sessions:
            output.info("No matching sessions to start.")
            return

        operation = "restarting" if replace_running else "starting"
        output.info(f"{operation.title()} {len(sessions)} session(s)...")

        for session in sessions:
            try:
                _launch_existing_session(
                    db,
                    config,
                    name=session.name,
                    replace_running=replace_running,
                    force=force,
                    intent=intent,
                    interaction=interaction,
                )
            except UserAbort:
                # An interaction cancellation aborts the whole batch operation, not
                # just this one session. Propagate so the outer wrapper renders
                # "Aborted." once and exits.
                raise
            except BrokenStateError as exc:
                if not force:
                    output.warn(f"Skipping '{session.name}': {exc}")
                else:
                    failed.append((session.name, str(exc)))
                    output.warn(f"Error {operation} '{session.name}': {exc}")
            except StateError as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error {operation} '{session.name}': {exc}")
            except Exception as exc:
                failed.append((session.name, str(exc)))
                output.warn(f"Error {operation} '{session.name}': {exc}")

    if failed:
        raise ExternalError(f"{len(failed)} session(s) failed while {operation}.")


def start_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    _launch_existing_session(
        db,
        config,
        name=name,
        replace_running=False,
        force=force,
        intent=_requested_launch_intent(force_new=force_new, resume_only=resume_only),
        interaction=interaction,
    )


def restart_session(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    force_new: bool = False,
    resume_only: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    _launch_existing_session(
        db,
        config,
        name=name,
        replace_running=True,
        force=force,
        intent=_requested_launch_intent(force_new=force_new, resume_only=resume_only),
        interaction=interaction,
    )


def start_all_sessions(
    db: Database,
    config: Config,
    **kwargs: object,
) -> None:
    _launch_all_sessions(db, config, replace_running=False, **kwargs)  # type: ignore[arg-type]


def restart_all_sessions(
    db: Database,
    config: Config,
    **kwargs: object,
) -> None:
    _launch_all_sessions(db, config, replace_running=True, **kwargs)  # type: ignore[arg-type]
