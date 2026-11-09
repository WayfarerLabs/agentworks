"""Dependency-ordered session creation and recovery-aware rollback.

Before dispatching tmux, failure unwinds the newly realized owners. A dispatched
launch retains its row and dependencies unless runtime absence is proved.
Persist the stable fingerprint before finalizing startup and releasing protection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.capabilities.harness_integration import HarnessLaunchIntent, require_implemented_start
from agentworks.db import SessionMode
from agentworks.errors import AgentworksError, ExternalError, NotFoundError, ValidationError
from agentworks.harness_setup.outcomes import describe_setup_failures
from agentworks.harness_setup.readiness import RequiredSetupMissingError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.outcomes import SetupFailure
    from agentworks.orchestration.unwind import RealizationLog
    from agentworks.resources.registry import Registry
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport

    from ._create_types import SessionGraph, SessionPlan


def _realize_ephemerals(
    db: Database,
    config: Config,
    *,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    secret_values: dict[str, str],
    log: RealizationLog,
    setup_failures: list[SetupFailure],
) -> None:
    """Realize the pending workspace / agent in dependency order, recording
    each completed realization in ``log`` for the reverse-order unwind."""
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.secrets import ScopedSecrets

    def scoped_ctx(
        secret_names: tuple[str, ...],
        *,
        admin_access: VMAccess | None = None,
        agent_access: VMAccess | None = None,
    ) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=graph.scope,
            admin_access=admin_access,
            agent_access=agent_access,
            secrets=ScopedSecrets(secret_values, secret_names),
        )

    if graph.pending_workspace is not None:
        from agentworks.workspaces.realize import realize_workspace

        assert graph.workspace_tmpl is not None  # resolved at build above
        # The realizer emits its own "Creating workspace ..." line
        # (used by the standalone `workspace create` path too), so the
        # session flow must not echo it a second time here.
        with output.section("Creating Workspace"):
            try:
                realize_workspace(
                    db,
                    config,
                    registry,
                    name=plan.workspace_name,
                    vm=vm,
                    template=graph.workspace_tmpl,
                    setup_failures=setup_failures,
                    setup_inputs=graph.pending_workspace.setup_inputs,
                    setup_values=secret_values,
                    overlay=graph.workspace_overlay,
                    defer_overlay_report=True,
                )
            except BaseException:
                if db.get_workspace(plan.workspace_name) is not None:
                    log.mark_realized(graph.pending_workspace)
                raise
            else:
                log.mark_realized(graph.pending_workspace)
    if graph.pending_agent is not None:
        from agentworks.agents.realize import realize_agent
        from agentworks.git_credentials import (
            credential_redactions,
            credential_requests,
        )

        assert plan.agent_name is not None  # defaulted to ``name`` above
        assert graph.agent_tmpl is not None and graph.agent_tmpl_node is not None
        with output.section("Creating Agent"):
            output.info(f"Creating agent '{plan.agent_name}' on VM '{vm.name}' (template: {graph.agent_tmpl.name})...")
            credential_ops = credential_requests(graph.agent_tmpl_node.credentials, scoped_ctx)
            git_redactions = credential_redactions(graph.agent_tmpl_node.credentials, secret_values)
            try:
                realize_agent(
                    db,
                    config,
                    registry,
                    name=plan.agent_name,
                    vm=vm,
                    template=graph.agent_tmpl,
                    credential_requests=credential_ops,
                    credential_redactions=git_redactions,
                    setup_failures=setup_failures,
                    setup_inputs=graph.pending_agent.setup_inputs,
                    setup_values=secret_values,
                    overlay=graph.agent_overlay,
                    defer_overlay_report=True,
                )
            except BaseException:
                if db.get_agent(plan.agent_name) is not None:
                    log.mark_realized(graph.pending_agent)
                raise
            else:
                log.mark_realized(graph.pending_agent)


def _start_session_slice(
    db: Database,
    config: Config,
    *,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    target: Transport,
    logger: SSHLogger,
    run_command: RunCommand,
    agent_target: Transport | None,
    secret_values: dict[str, str],
    setup_incomplete: bool,
    on_session_retained: Callable[[], None],
) -> None:
    """The session's own realizing slice: the grant, the DB row, the tmux
    launch, and the completion bookkeeping.

    The inner ``except`` runs the session node's partial-state teardown
    (rollback level 1); a completed session is then flipped and never
    rolled back. The terminal result line and the post-start bookkeeping
    render at column 0, outside the ``Starting Session`` section.
    """
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    name = plan.name
    workspace_name = plan.workspace_name
    agent_name = plan.agent_name
    scope = graph.scope
    template = graph.template
    session_node = graph.session_node
    pending_workspace = graph.pending_workspace
    pending_agent = graph.pending_agent

    ws = _mgr._require_workspace(db, workspace_name)

    resolved_agent_name: str | None = None
    agent_row: AgentRow | None = None
    if agent_name is not None:
        mode = SessionMode.AGENT
        agent_row = db.get_agent(agent_name)
        if agent_row is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        # Unreachable in practice: existing-agent VM was already
        # cross-checked in the upfront anchor block, and a fresh
        # ephemeral agent was just created on this same VM. Kept as
        # a tripwire so a future refactor that reorders or drops the
        # upfront check fails loudly rather than silently corrupting
        # cross-VM state.
        if agent_row.vm_name != vm.name:
            raise ValidationError(
                f"agent '{agent_name}' is on VM '{agent_row.vm_name}', "
                f"but workspace '{workspace_name}' is on VM '{vm.name}'",
                entity_kind="session",
                entity_name=name,
            )
        linux_user = agent_row.linux_user
        resolved_agent_name = agent_name
        if agent_target is None:
            # The ephemeral agent just realized: probe its direct
            # SSH BEFORE any session mutation, same contract as
            # the existing-agent probe above.
            from agentworks.agents.manager import _assert_agent_ssh_works
            from agentworks.transports import agent_transport

            agent_target = agent_transport(vm, config, agent_row)
            _assert_agent_ssh_works(agent_target, agent_row)
    else:
        mode = SessionMode.ADMIN
        linux_user = vm.admin_username
    admin_access = _mgr._identity_access(vm, config, None, logger=logger)
    agent_access = None if agent_row is None else _mgr._identity_access(vm, config, agent_row, logger=logger)

    # Op-start runup: the required-commands check probes a
    # just-realized ephemeral agent here (it deferred at
    # preflight; the log's flip above is what it observed). For
    # targets that were realized at preflight the check already
    # fired and this is a no-op.
    session_node.runup(
        RunContext(
            config=config,
            operation_scope=scope,
            admin_access=admin_access,
            agent_access=agent_access,
        )
    )

    # Existing owners were checked before secret resolution. For pending
    # owners, read their newly applied setup now, before session mutation.
    if plan.new_workspace or plan.new_agent:
        from agentworks.harness_setup.readiness import require_setup_ready

        require_setup_ready(
            db,
            registry,
            session_node.harness_integration,
            vm=vm,
            workspace=ws,
            agent_name=resolved_agent_name,
            access=agent_access or admin_access,
        )

    # Retain the dedicated address for either mode, including failed launches.
    from agentworks.sessions.tmux import admin_socket_path, agent_socket_path

    expected_socket = (
        agent_socket_path(linux_user, name) if mode == SessionMode.AGENT else admin_socket_path(linux_user, name)
    )

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, vm.name), output.section("Starting Session"):
        output.info(
            f"Starting session '{name}' on workspace '{workspace_name}' ({mode_label}, template: {template.name})..."
        )

        retain_unknown_runtime = False
        fingerprint_recorded = False
        try:
            from agentworks.artifacts.session import (
                commit_session_artifacts,
                prepare_session_artifacts,
                stage_session_artifacts,
                validate_session_application,
            )

            session_env = _mgr._resolve_session_env(
                registry,
                values=secret_values,
                db=db,
                vm=vm,
                ws=ws,
                session_name=name,
                session_template=template,
                mode=mode,
                agent_name=resolved_agent_name,
                linux_user=linux_user,
            )
            prepared_artifacts = prepare_session_artifacts(
                db,
                registry,
                name=name,
                template=template,
                vm=vm,
                workspace=ws,
                agent_name=resolved_agent_name,
                access=agent_access or admin_access,
                environment=session_env,
                # The start stages sanitize probe diagnostics with everything the operation log redacts.
                redactions=logger.redactions,
                linux_user=linux_user,
                secret_target=_mgr._session_secret_target(
                    registry,
                    db=db,
                    vm=vm,
                    ws=ws,
                    session_name=name,
                    session_template=template,
                    mode=mode,
                    agent_name=resolved_agent_name,
                ),
            )
            session_node.harness_integration.prepare_artifacts(prepared_artifacts.context)
            # Everything that creates partial session state (on-VM group
            # membership, implicit-grant row, session row, restricted-config
            # write, tmux session) runs inside this block so a KI /
            # exception anywhere here triggers the session node's
            # partial-state teardown.
            if resolved_agent_name is not None:
                # Auto-grant implicit workspace access if the agent has no
                # existing grant on this workspace.
                if not db.has_any_grant(resolved_agent_name, workspace_name):
                    from agentworks.agents.grants import add_to_workspace_group

                    add_to_workspace_group(vm, config, db, linux_user, workspace_name)
                db.insert_agent_grant(resolved_agent_name, workspace_name, "implicit", session_name=name)

            # Op-start RunContext for the harness_integration's start op: mirrors
            # the runup readiness ctx above (targets), plus the scoped
            # secrets (the session node's declared union, empty for the
            # built-in shell harness_integration; ScopedSecrets never delivers).
            # Template-var substitution lifts OUT of the harness_integration and
            # wraps its returned string. The op runs BEFORE the insert
            # so a freshly minted harness_integration_state (claude-code's session
            # id) lands with the new row; it does only read-only work
            # (a login-shell string for shell, a find probe for
            # claude-code), so it stays ahead of any tmux mutation.
            start_ctx = RunContext(
                config=config,
                operation_scope=scope,
                admin_access=admin_access,
                agent_access=agent_access,
                secrets=ScopedSecrets(secret_values, session_node.secret_refs()),
            )
            harness_start = require_implemented_start(
                session_node.harness_integration.start(start_ctx, intent=HarnessLaunchIntent.CREATE),
                intent=HarnessLaunchIntent.CREATE,
                harness_integration_name=template.harness_integration,
                session_name=name,
            )
            artifact_application = validate_session_application(
                harness_start.artifacts, prepared_artifacts.context, integration=template.harness_integration
            )
            if artifact_application.artifacts_dir is not None:
                session_env["AGENTWORKS_ARTIFACTS_DIR"] = artifact_application.artifacts_dir
            command = _mgr._substitute_template_vars(
                harness_start.command,
                {"session_name": name, "workspace_name": workspace_name},
            )
            if harness_start.note is not None:
                output.detail(harness_start.note)

            # Insert DB record before any tmux work so a crash mid-create
            # leaves a recoverable row (and the teardown can find it to
            # delete). The harness_integration's start op ran just above, so its
            # state (mutated in place inside the node's full namespaced
            # blob) lands with the new row.
            from agentworks.instance_specs import persist_creation_overlay, refuse_orphan_creation_state

            with db.transaction():
                refuse_orphan_creation_state(db, "session", name)
                db.insert_session(
                    name,
                    workspace_name,
                    template.name,
                    mode,
                    agent_name=resolved_agent_name,
                    created_workspace=pending_workspace is not None,
                    created_agent=pending_agent is not None,
                    socket_path=expected_socket,
                    harness_integration_state=session_node.harness_integration_state,
                    session_uuid=prepared_artifacts.context.session_uuid,
                    run_id=prepared_artifacts.context.run_id,
                )
                persist_creation_overlay(db, "session", name, graph.session_overlay)

            stage_session_artifacts(
                db,
                name,
                template.harness_integration,
                agent_access or admin_access,
                prepared_artifacts,
                artifact_application,
            )
            commit_session_artifacts(
                db, name, template.harness_integration, agent_access or admin_access, prepared_artifacts
            )
            deploy_restricted_config(run_command, history_limit=config.session.history_limit)
            # Pick the SSH transport for tmux operations:
            # - admin sessions: admin's run_command (unchanged)
            # - agent sessions: agent's run_command (direct
            #   target-user SSH). agent_target was built and probed above
            #   so a pre-rollout agent never reaches this point. admin's
            #   ``target`` is still passed for socket-root setup which
            #   requires root.
            session_run_command: RunCommand
            if mode == SessionMode.AGENT:
                assert agent_target is not None  # built in the agent branches above
                session_run_command = agent_target.run
            else:
                session_run_command = run_command
            from agentworks.sessions.tmux import finalize_session_start

            fingerprint_target = agent_target if mode == SessionMode.AGENT else target
            assert fingerprint_target is not None
            launch_boot = _mgr._get_boot_id(fingerprint_target)
            if launch_boot is None:
                raise ExternalError(
                    f"could not read the VM boot identity before launching session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )

            def before_spawn() -> None:
                nonlocal retain_unknown_runtime
                db.update_session_runtime(
                    name, socket_path=expected_socket, pid=None, boot_id=launch_boot, tmux_server_start_ticks=None
                )
                retain_unknown_runtime = True

            sock, pid = create_tmux_session(
                name,
                ws.workspace_path,
                command,
                linux_user,
                run_command=session_run_command,
                target=target,
                admin_username=vm.admin_username,
                is_admin=(mode == SessionMode.ADMIN),
                env=session_env,
                before_spawn=before_spawn,
                session_uuid=prepared_artifacts.context.session_uuid,
                run_id=prepared_artifacts.context.run_id,
            )
            db.record_session_started(name)
            from agentworks.sessions.tmux import ProbeStatus, capture_tmux_server_fingerprint

            fingerprint_probe = capture_tmux_server_fingerprint(target=fingerprint_target, socket_path=sock)
            fingerprint = fingerprint_probe.fingerprint
            if (
                fingerprint_probe.status is not ProbeStatus.PRESENT
                or fingerprint is None
                or (pid is not None and fingerprint.pid != pid)
            ):
                raise ExternalError(
                    f"could not capture a stable tmux server fingerprint for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                    hint=(
                        f"The session row and socket {sock} were retained because runtime absence could not be proved."
                    ),
                )
            db.update_session_runtime(
                name,
                socket_path=sock,
                pid=fingerprint.pid,
                boot_id=fingerprint.boot_id,
                tmux_server_start_ticks=fingerprint.start_ticks,
            )
            fingerprint_recorded = True
            finalize_session_start(
                name,
                run_command=session_run_command,
                socket_path=sock,
                is_admin=mode == SessionMode.ADMIN,
                redactions=tuple(secret_values.values()),
            )
            retain_unknown_runtime = False
        except (KeyboardInterrupt, Exception) as error:
            # An acknowledged runtime may still be live. Only its stored
            # process fingerprint can authorize discarding the recovery row.
            if fingerprint_recorded and not isinstance(error, KeyboardInterrupt):
                current = db.get_session(name)
                if current is not None:
                    try:
                        retain_unknown_runtime = not _mgr._prove_stored_runtime_absent(
                            current, target=agent_target or target
                        )
                    except AgentworksError:
                        retain_unknown_runtime = True
            # Session-internal cleanup only (DB row, grant, group
            # membership: the node's partial-state teardown). The
            # realized ephemerals are unwound by the outer handlers,
            # whose warn prints one clean reason line before the
            # rollback's delete messages start landing.
            if not retain_unknown_runtime:
                from agentworks.artifacts.session import cleanup_session_artifacts

                current_session = db.get_session(name)
                if current_session is not None:
                    try:
                        cleanup_session_artifacts(db, current_session, agent_access or admin_access)
                    except Exception:
                        output.warn(
                            "Session artifact cleanup is incomplete; its row and ownership evidence were retained."
                        )
                        retain_unknown_runtime = True
                if not retain_unknown_runtime:
                    session_node.teardown()
            if retain_unknown_runtime:
                on_session_retained()
                output.warn(f"Session '{name}' and its dependencies were retained for runtime recovery.")
            raise

        # The session's realizing slice is complete: flip the node.
        # Deliberately NOT via the realization log: a completed
        # session (tmux up, row written) is never rolled back, so
        # failures past this point unwind only the ephemerals, and
        # the session survives them. That pins the completed-session
        # window as non-rollbackable.
        on_session_retained()
        session_node.mark_realized()

    # The section is closed: the terminal result line and the
    # post-start tmuxinator regeneration renders
    # at column 0, mirroring existing-session launch. They stay inside the
    # outer try so a failure here still triggers the ephemeral
    # rollback (a completed session itself is never rolled back).
    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    started = f"Session '{name}' started ({mode_label}, template: {template.name})"
    from agentworks.harness_setup.outcomes import setup_completion_suffix
    from agentworks.harness_setup.state import read_native_setup

    suffix = setup_completion_suffix(read_native_setup(db, "session", name))
    if setup_incomplete or suffix:
        output.info(f"{started}{suffix or '; harness setup incomplete'}")
    else:
        output.result(started)

    # Update the workspace's tmuxinator config.
    _mgr._regenerate_tmuxinator(db, config, vm, ws)


def _roll_forward(
    db: Database,
    config: Config,
    *,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    target: Transport,
    logger: SSHLogger,
    run_command: RunCommand,
    agent_target: Transport | None,
    secret_values: dict[str, str],
    setup_failures: list[SetupFailure],
) -> None:
    """Realize the ephemerals then the session slice, unwinding the
    realized ephemerals on core failure (rollback level 2). Required setup
    blocks only its session consumer and retains completed owners for repair."""
    from agentworks.orchestration.unwind import RealizationLog

    # Realize the pending nodes in dependency order, recording each
    # completed realization; on later core failure the log unwinds them in
    # reverse (agent before workspace, today's proven rollback order). The
    # session's own partial state is cleaned by its node's teardown in the
    # slice, and a COMPLETED session (tmux up) is deliberately never
    # rolled back.
    log = RealizationLog()
    retain_dependencies = False

    def retain_session() -> None:
        nonlocal retain_dependencies
        retain_dependencies = True

    try:
        _realize_ephemerals(
            db,
            config,
            registry=registry,
            plan=plan,
            graph=graph,
            vm=vm,
            secret_values=secret_values,
            log=log,
            setup_failures=setup_failures,
        )
        _start_session_slice(
            db,
            config,
            registry=registry,
            plan=plan,
            graph=graph,
            vm=vm,
            target=target,
            logger=logger,
            run_command=run_command,
            agent_target=agent_target,
            secret_values=secret_values,
            setup_incomplete=bool(setup_failures),
            on_session_retained=retain_session,
        )
    except RequiredSetupMissingError as error:
        # The owners completed their core work. Keep them and their native
        # progress so the operator can repair the session's required setup.
        _render_retained_overlay_outcomes(db, plan, graph)
        retained = []
        if graph.pending_workspace is not None:
            retained.append(
                f"Workspace '{plan.workspace_name}' was retained; "
                f"replace --new-workspace with --workspace {plan.workspace_name} and remove any "
                "--workspace-name, --workspace-template and --workspace-spec options."
            )
        if graph.pending_agent is not None:
            retained.append(
                f"Agent '{plan.agent_name}' was retained; replace --new-agent with --agent {plan.agent_name} "
                "and remove any --agent-name, --agent-template and --agent-spec options."
            )
        summary = describe_setup_failures(setup_failures)
        error.hint = "\n".join(part for part in (error.hint, *retained, summary) if part)
        raise
    except KeyboardInterrupt:
        if retain_dependencies:
            output.warn(f"Cancelling session create '{plan.name}'; its managed session and dependencies were retained.")
        else:
            output.warn(f"Cancelling session create '{plan.name}'... rolling back.")
            log.unwind()
        _render_retained_overlay_outcomes(db, plan, graph)
        raise
    except Exception as e:
        # Print the reason BEFORE the rollback's delete-* messages so the
        # operator sees the failure context first, not after a stream of
        # 'Agent deleted' / 'Workspace deleted' lines. The CLI's
        # exception handler still prints the canonical 'Error: ...' line
        # with the typed hint at the very end; this warn just bridges
        # the silence between "thing X created" and the rollback output.
        from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic, sanitize_setup_error

        if isinstance(e, AgentworksError):
            sanitize_setup_error(e, redactions=secret_values.values())
        diagnostic = sanitize_setup_diagnostic(str(e), redactions=secret_values.values())
        if retain_dependencies:
            output.warn(
                f"Session create '{plan.name}' failed; its managed session and dependencies were retained. "
                f"Reason: {diagnostic}"
            )
        else:
            output.warn(f"Session create '{plan.name}' failed; rolling back. Reason: {diagnostic}")
            log.unwind()
        _render_retained_overlay_outcomes(db, plan, graph)
        raise
    else:
        _render_retained_overlay_outcomes(db, plan, graph)


def _render_retained_overlay_outcomes(db: Database, plan: SessionPlan, graph: SessionGraph) -> None:
    """Report only desired layers whose owners remain after this roll-forward."""
    from agentworks.instance_specs import render_retained_creation_overlay

    if graph.pending_workspace is not None:
        render_retained_creation_overlay(db, "workspace", plan.workspace_name)
    if graph.pending_agent is not None:
        assert plan.agent_name is not None
        render_retained_creation_overlay(db, "agent", plan.agent_name)
    render_retained_creation_overlay(db, "session", plan.name)
