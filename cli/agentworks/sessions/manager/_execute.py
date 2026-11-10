"""Execute prepared session launches using invocation-owned secret values."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import require_implemented_start
from agentworks.db import SessionMode, SessionStatus
from agentworks.errors import ExternalError, StateError
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.sessions.manager._lifecycle import (
    _mark_runtime_unknown,
    _redacted_launch_errors,
    _require_selected_session,
    _teardown_session,
    _validated_socket_path,
)
from agentworks.ssh import SSHLogger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.sessions.manager._runtime import PreparedSessionLaunch
    from agentworks.sessions.tmux import RunCommand


def execute_session_start(
    prepared: PreparedSessionLaunch,
    *,
    secret_values: Mapping[str, str],
    graph_secret_values: Mapping[str, str],
) -> bool:
    """Launch or recover after the complete workload boundary has succeeded."""
    from agentworks.sessions.tmux import create_session as create_tmux_session
    from agentworks.sessions.tmux import deploy_restricted_config

    definition = prepared.definition
    db, config, registry = definition.db, definition.config, definition.registry
    session = _require_selected_session(db, definition.session.name, definition.session.session_uuid)
    ws = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, ws)
    if vm.vm_uuid != definition.vm.vm_uuid or ws.vm_name != definition.vm.name:
        raise StateError("session parent changed during preparation", entity_kind="session", entity_name=session.name)
    if session.desired_runtime_state != definition.session.desired_runtime_state:
        raise StateError("session target changed during preparation", entity_kind="session", entity_name=session.name)
    name, template, session_node, scope = session.name, definition.template, definition.session_node, definition.scope
    replace_running, force, intent = definition.replace_running, definition.force, definition.intent
    running_restart_authorized = definition.running_restart_authorized
    operation = "restart" if replace_running else "start"
    is_admin, is_legacy = session.mode == SessionMode.ADMIN.value, prepared.legacy
    admin_target, session_target = prepared.admin_target, prepared.session_target
    from agentworks.sessions.manager._runtime import observe_session_runtime

    observed_now = observe_session_runtime(
        session, admin_target=admin_target, session_target=session_target if is_legacy else None
    )
    if observed_now is SessionStatus.UNKNOWN:
        raise StateError("session runtime became unknown before execution", entity_kind="session", entity_name=name)
    if observed_now is SessionStatus.RUNNING and prepared.observed_status is not SessionStatus.RUNNING:
        raise StateError("session became running after preparation", entity_kind="session", entity_name=name)
    if prepared.noop and observed_now is not SessionStatus.RUNNING:
        raise StateError("session stopped after no-op preparation", entity_kind="session", entity_name=name)
    if prepared.noop:
        output.result(f"Session '{name}' is already running")
        return False
    # Evidence repair is deferred until every eligible workload has valid values.
    if not is_legacy:
        session = _mgr._ensure_pid(session, target=admin_target, db=db)
    status = _mgr.check_session_status(session, target=admin_target) if not is_legacy else observed_now
    if status is SessionStatus.UNKNOWN:
        raise StateError("session runtime became unknown before execution", entity_kind="session", entity_name=name)
    if status is SessionStatus.RUNNING and prepared.observed_status is not SessionStatus.RUNNING:
        raise StateError("session became running after preparation", entity_kind="session", entity_name=name)
    recovered_startup = prepared.recovered_startup
    session_run_command: RunCommand = session_target.run
    run_command: RunCommand = admin_target.run
    redactions = tuple(dict.fromkeys((*graph_secret_values.values(), *secret_values.values())))
    with _redacted_launch_errors(redactions):
        logger = SSHLogger(
            vm.name,
            f"session-{operation}",
            redactions=redactions,
        )
        admin_target = _mgr.transport(vm, config, logger=logger)
        run_command = admin_target.run
        if is_admin:
            session_target = admin_target
            session_run_command = admin_target.run

        if recovered_startup and status is SessionStatus.RUNNING and not replace_running:
            from agentworks.sessions.tmux import (
                ProbeStatus,
                capture_tmux_server_fingerprint,
                finalize_session_start,
                require_socket_path,
            )

            socket_path = _validated_socket_path(db, session)
            require_socket_path(socket_path, run_command=session_run_command)
            probe = capture_tmux_server_fingerprint(target=session_target, socket_path=socket_path)
            observed = probe.fingerprint
            if (
                probe.status is not ProbeStatus.PRESENT
                or observed is None
                or (observed.pid, observed.boot_id, observed.start_ticks)
                != (session.pid, session.boot_id, session.tmux_server_start_ticks)
            ):
                raise StateError(
                    f"session '{name}' runtime identity changed before startup recovery",
                    entity_kind="session",
                    entity_name=name,
                    hint="Inspect the current runtime; no startup settings were changed.",
                )

            finalize_session_start(
                name,
                run_command=session_run_command,
                socket_path=socket_path,
                is_admin=is_admin,
                redactions=redactions,
            )
            output.result(f"Session '{name}' startup recovered")
            return True

        from agentworks.artifacts.session import (
            commit_session_artifacts,
            prepare_session_artifacts,
            stage_session_artifacts,
            validate_session_application,
        )

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

        prepared_artifacts = prepare_session_artifacts(
            db,
            registry,
            name=name,
            template=template,
            vm=vm,
            workspace=ws,
            agent_name=session.agent_name,
            runner=session_target,
            environment=session_env,
            redactions=redactions,
            session_uuid=session.session_uuid,
            linux_user=linux_user,
            secret_target=_mgr._session_secret_target(
                registry,
                db=db,
                vm=vm,
                ws=ws,
                session_name=name,
                session_template=template,
                mode=SessionMode(session.mode),
                agent_name=session.agent_name,
            ),
        )
        session_node.harness_integration.prepare_artifacts(prepared_artifacts.context)
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
        template_values = _mgr._session_context_values(db, vm, ws, name, SessionMode(session.mode), session.agent_name)
        session_node.harness_integration.prepare_context(template_values, intent=intent)
        harness_start = require_implemented_start(
            session_node.harness_integration.start(start_ctx, intent=intent),
            intent=intent,
            harness_integration_name=template.harness_integration,
            session_name=name,
        )
        artifact_application = validate_session_application(
            harness_start.artifacts, prepared_artifacts.context, integration=template.harness_integration
        )
        if artifact_application.artifacts_dir is not None:
            session_env["AGENTWORKS_ARTIFACTS_DIR"] = artifact_application.artifacts_dir
        stage_session_artifacts(
            db, name, template.harness_integration, session_target, prepared_artifacts, artifact_application
        )
        command = harness_start.command

        with output.section("Starting Session"):
            output.info(f"{operation.title()}ing session '{name}'...")

            if is_legacy or status in {SessionStatus.RUNNING, SessionStatus.RESIDUAL, SessionStatus.BROKEN}:
                _teardown_session(
                    session,
                    target=session_target,
                    target_owns_session=True,
                    db=db,
                    force=force,
                    legacy_running_authorized=(not replace_running or running_restart_authorized),
                )

            commit_session_artifacts(db, name, template.harness_integration, session_target, prepared_artifacts)
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

            launch_boot = _mgr._get_boot_id(session_target)
            if launch_boot is None:
                raise ExternalError(
                    f"could not read the VM boot identity before launching session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                )
            from agentworks.sessions.tmux import admin_socket_path, agent_socket_path

            expected_socket = admin_socket_path(linux_user, name) if is_admin else agent_socket_path(linux_user, name)

            def before_spawn() -> None:
                _mark_runtime_unknown(db, session, socket_path=expected_socket, boot_id=launch_boot)

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
                    before_spawn=before_spawn,
                    session_uuid=prepared_artifacts.context.session_uuid,
                    run_id=prepared_artifacts.context.run_id,
                )
            except RuntimeError as exc:
                if "already has an active tmux server" in str(exc):
                    raise StateError(
                        f"session '{name}' has an active tmux server that was not detected by the status check.",
                        entity_kind="session",
                        entity_name=name,
                        hint="Inspect the conflicting server manually; its runtime was preserved.",
                    ) from exc
                raise
            from agentworks.sessions.tmux import (
                ProbeStatus,
                capture_tmux_server_fingerprint,
                finalize_session_start,
            )

            db.record_session_started(name)

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
                raise ExternalError(
                    f"could not capture a stable tmux server fingerprint for session '{name}'",
                    entity_kind="session",
                    entity_name=name,
                    hint=(
                        f"The session row and socket {new_sock} were retained because "
                        "runtime absence could not be proved."
                    ),
                )
            db.update_session_runtime(
                name,
                socket_path=new_sock,
                pid=fingerprint.pid,
                boot_id=fingerprint.boot_id,
                tmux_server_start_ticks=fingerprint.start_ticks,
            )
            finalize_session_start(
                name,
                run_command=session_run_command,
                socket_path=new_sock,
                is_admin=is_admin,
                redactions=redactions,
            )

        from agentworks.harness_setup.outcomes import setup_completion_suffix
        from agentworks.harness_setup.state import read_native_setup

        suffix = setup_completion_suffix(read_native_setup(db, "session", name))
        output.result(f"Session '{name}' {operation}ed{suffix}")

        _mgr._regenerate_tmuxinator(db, config, vm, ws)
    return True
