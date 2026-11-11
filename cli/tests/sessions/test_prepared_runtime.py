"""Prepared session boundaries preserve intent, identity, and read-only selection."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import DesiredRuntimeState, SessionStatus
from agentworks.errors import StateError, UserAbort, ValidationError
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from agentworks.sessions.manager._intent import accept_session_runtime_state
from agentworks.sessions.manager._runtime import (
    execute_session_stop,
    prepare_session_launch,
    prepare_session_stop,
)

from .test_create_start_restart_orchestrated import _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.sessions.manager import PreparedSessionLaunch, SessionLaunchDefinition


def _session(db: Database) -> SessionRow:
    session = db.get_session("s1")
    assert session is not None
    return session


def _vm(db: Database) -> VMRow:
    vm = db.get_vm("vm1")
    assert vm is not None
    return vm


def test_pure_setter_preserves_runtime_and_start_identity(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    before = _session(db)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("registry loaded"))
    manager.set_session_desired_runtime_state(db, name="s1", state="stopped")
    after = _session(db)
    assert after.desired_runtime_state is DesiredRuntimeState.STOPPED
    assert (after.session_uuid, after.pid, after.boot_id, after.tmux_server_start_ticks, after.last_started_at) == (
        before.session_uuid,
        before.pid,
        before.boot_id,
        before.tmux_server_start_ticks,
        before.last_started_at,
    )
    with pytest.raises(ValidationError):
        manager.set_session_desired_runtime_state(db, name="s1", state="on-demand")
    assert _session(db).desired_runtime_state is DesiredRuntimeState.STOPPED
    db.close()


def test_acceptance_reuses_guard_and_refuses_replaced_uuid(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    selected = _session(db)
    with native_mutation_guard(db.path, "vm1") as guard:
        accepted = accept_session_runtime_state(
            db, selected, state=DesiredRuntimeState.STOPPED, held_native_guard=guard
        )
        assert accepted.desired_runtime_state is DesiredRuntimeState.STOPPED
        with pytest.raises(StateError):
            accept_session_runtime_state(
                db,
                replace(selected, session_uuid="00000000-0000-0000-0000-000000000000"),
                state=DesiredRuntimeState.RUNNING,
                held_native_guard=guard,
            )
    assert _session(db).desired_runtime_state is DesiredRuntimeState.STOPPED
    db.close()


def _definition(db: Database, monkeypatch: pytest.MonkeyPatch, *, status: SessionStatus) -> SessionLaunchDefinition:
    definition = _launch_definition(db)
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: status)
    monkeypatch.setattr(manager, "_ensure_pid", lambda *a, **k: pytest.fail("PID evidence mutated"))
    return definition


def test_running_noop_preparation_never_repairs_or_uses_env_targets(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    definition = _definition(db, monkeypatch, status=SessionStatus.RUNNING)
    monkeypatch.setattr(manager, "_build_session_target", lambda *a, **k: pytest.fail("owning transport probed"))
    prepared = prepare_session_launch(definition)
    assert prepared.noop and prepared.secret_targets == ()
    db.close()


@pytest.mark.parametrize("status", [SessionStatus.UNKNOWN, SessionStatus.BROKEN])
def test_ineligible_preparation_never_repairs_or_collects_values(tmp_path, monkeypatch, status):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    definition = _definition(db, monkeypatch, status=status)
    with pytest.raises(StateError):
        prepare_session_launch(definition)
    db.close()


def test_off_parent_stop_records_proven_absence_without_transport(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("transport created"))
    prepared = prepare_session_stop(db, SimpleNamespace(), _session(db), parent_off=True)
    assert prepared.parent_off and prepared.observed_status is SessionStatus.STOPPED
    assert execute_session_stop(prepared) is True
    assert _session(db).pid == -1 and _session(db).boot_id is None
    assert _session(db).desired_runtime_state is DesiredRuntimeState.RUNNING
    db.close()


@pytest.mark.parametrize("already_stopped", [False, True])
def test_off_parent_stop_cannot_overwrite_an_intervening_run(tmp_path, monkeypatch, already_stopped):
    from uuid import uuid4

    from agentworks.db import PID_STOPPED, SessionRuntimeSnapshot

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    prepared = prepare_session_stop(db, SimpleNamespace(), _session(db), parent_off=True)
    db._conn.execute("UPDATE sessions SET run_id = ? WHERE name = ?", (str(uuid4()), "s1"))
    db._conn.commit()
    if already_stopped:
        db.update_session_runtime(
            "s1", socket_path=_session(db).socket_path, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
        )
    before = SessionRuntimeSnapshot.from_session(_session(db))
    with pytest.raises(StateError):
        execute_session_stop(prepared)
    assert SessionRuntimeSnapshot.from_session(_session(db)) == before
    db.close()


@pytest.mark.parametrize("control_signal", [UserAbort, KeyboardInterrupt])
def test_prepared_stop_propagates_cancellation_without_marking_runtime_stopped(tmp_path, monkeypatch, control_signal):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    manager.set_session_desired_runtime_state(db, name="s1", state=DesiredRuntimeState.STOPPED)
    before = _session(db)
    prepared = prepare_session_stop(db, SimpleNamespace(), before)
    failure = control_signal("cancelled")
    attempted: list[str] = []

    def cancel(session, *, target, **kwargs):
        assert session == before
        assert target is prepared.session_target
        attempted.append(session.session_uuid)
        raise failure

    monkeypatch.setattr(manager, "_teardown_session", cancel)
    try:
        with pytest.raises((UserAbort, KeyboardInterrupt)) as caught:
            execute_session_stop(prepared)
        assert caught.value is failure
        assert attempted == [before.session_uuid]
        assert _session(db) == before
    finally:
        db.close()


def _launch_definition(db: Database, *, replace_running: bool = False, force: bool = False) -> SessionLaunchDefinition:
    from agentworks.bootstrap import load_request_registry
    from agentworks.sessions.manager._runtime import prepare_session_definition
    from agentworks.vms.nodes import live_vm_node

    config = cast("Config", SimpleNamespace(session=SimpleNamespace(history_limit=1)))
    registry = load_request_registry(config, live_database=db)
    return prepare_session_definition(
        db,
        config,
        registry,
        _session(db),
        vm_node=live_vm_node(db, config, registry, _vm(db)),
        replace_running=replace_running,
        running_restart_authorized=replace_running,
        force=force,
    )


def _prepare_launch(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: SessionStatus = SessionStatus.STOPPED,
    replace_running: bool = False,
    force: bool = False,
) -> PreparedSessionLaunch:
    return prepare_session_launch(_launch_definition(db, replace_running=replace_running, force=force))


@pytest.mark.parametrize("status", [SessionStatus.STOPPED, SessionStatus.RESIDUAL])
def test_eligible_preparation_defers_pid_writes_and_keeps_session_scope(tmp_path, monkeypatch, status):
    db, events = _restart_fixture(tmp_path, monkeypatch, status=status)
    before = _session(db)
    monkeypatch.setattr(manager, "_ensure_pid", lambda *a, **k: pytest.fail("PID repair during preparation"))
    prepared = _prepare_launch(db, monkeypatch, status=status)
    assert prepared.observed_status is status and not prepared.noop
    assert len(prepared.secret_targets) == 1
    assert prepared.definition.scope.session == "s1"
    assert prepared.definition.scope.agent == "a1" and not prepared.definition.scope.admin
    assert _session(db) == before
    assert "kill" not in events and "tmux_create" not in events
    db.close()


def test_executor_uses_supplied_values_and_preserves_target(tmp_path, monkeypatch):
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    manager.set_session_desired_runtime_state(db, name="s1", state="stopped")
    selected = _session(db)
    prepared = _prepare_launch(db, monkeypatch)
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", lambda *a, **k: pytest.fail("source reached executor")
    )
    assert execute_session_start(prepared, secret_values={}, graph_secret_values={})
    after = _session(db)
    assert after.session_uuid == selected.session_uuid
    assert after.desired_runtime_state is DesiredRuntimeState.STOPPED
    assert after.pid == 4243 and after.tmux_server_start_ticks == 1
    assert events.count("tmux_create") == 1
    db.close()


def test_executor_refuses_newly_running_session_before_artifact_staging(tmp_path, monkeypatch):
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    prepared = _prepare_launch(db, monkeypatch)
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.RUNNING)
    monkeypatch.setattr("agentworks.artifacts.session.stage_session_artifacts", lambda *a, **k: pytest.fail("staged"))
    with pytest.raises(StateError):
        execute_session_start(prepared, secret_values={}, graph_secret_values={})
    assert "tmux_create" not in events
    db.close()


def test_failed_launch_preparation_preserves_old_runtime_and_target(tmp_path, monkeypatch):
    from agentworks.errors import ExternalError
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.RUNNING)
    prepared = _prepare_launch(db, monkeypatch, status=SessionStatus.RUNNING, replace_running=True)
    before = _session(db)

    def fail_compile(*args, **kwargs):
        raise ExternalError("artifact preparation failed")

    monkeypatch.setattr("agentworks.artifacts.session.prepare_session_artifacts", fail_compile)
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: pytest.fail("old runtime killed"))
    with pytest.raises(ExternalError):
        execute_session_start(prepared, secret_values={}, graph_secret_values={})
    assert _session(db) == before
    assert "tmux_create" not in events
    db.close()


def test_stage_time_identity_replacement_prevents_teardown_or_spawn(tmp_path, monkeypatch):
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.RUNNING)
    prepared = _prepare_launch(db, monkeypatch, status=SessionStatus.RUNNING, replace_running=True)
    old_uuid = prepared.definition.session.session_uuid

    def replace_definition(*args, **kwargs):
        from agentworks.db import SessionMode

        db.delete_session("s1")
        db.insert_session(
            "s1",
            "ws1",
            "claude",
            SessionMode.AGENT,
            agent_name="a1",
            socket_path="/run/agentworks/agent-tmux-sockets/agt-a1/s1.sock",
        )

    monkeypatch.setattr("agentworks.artifacts.session.stage_session_artifacts", replace_definition)
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: pytest.fail("replacement killed"))
    with pytest.raises(StateError):
        execute_session_start(prepared, secret_values={}, graph_secret_values={})
    assert _session(db).session_uuid != old_uuid
    assert "tmux_create" not in events
    db.close()


def test_stopped_parent_refuses_launch_before_transport_creation(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    definition = _definition(db, monkeypatch, status=SessionStatus.STOPPED)
    vm = _vm(db)
    db.set_vm_desired_runtime_state("vm1", DesiredRuntimeState.STOPPED, expected_vm_uuid=vm.vm_uuid)
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("guest transport created"))
    with pytest.raises(StateError):
        prepare_session_launch(definition)
    assert _session(db).desired_runtime_state is DesiredRuntimeState.RUNNING
    db.close()


def test_interrupted_startup_recovery_collects_no_launch_requirements(tmp_path, monkeypatch):
    from agentworks.sessions import tmux
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.RUNNING)
    selected = _session(db)
    monkeypatch.setattr(tmux, "startup_protection_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        "agentworks.orchestration.readiness.preflight_all", lambda *a, **k: pytest.fail("launch preflight")
    )
    monkeypatch.setattr(
        "agentworks.harness_setup.readiness.require_setup_ready", lambda *a, **k: pytest.fail("setup check")
    )
    prepared = _prepare_launch(db, monkeypatch, status=SessionStatus.RUNNING)
    assert prepared.recovered_startup and not prepared.requires_launch
    assert prepared.secret_targets == () and prepared.secret_nodes == ()
    monkeypatch.setattr(
        tmux,
        "capture_tmux_server_fingerprint",
        lambda **k: tmux.FingerprintProbe(
            tmux.ProbeStatus.PRESENT,
            tmux.TmuxServerFingerprint(selected.pid, selected.boot_id, selected.tmux_server_start_ticks),
        ),
    )
    monkeypatch.setattr(tmux, "finalize_session_start", lambda *a, **k: events.append("recovered"))
    monkeypatch.setattr(
        "agentworks.artifacts.session.prepare_session_artifacts", lambda *a, **k: pytest.fail("launch compiled")
    )
    assert execute_session_start(prepared, secret_values={}, graph_secret_values={})
    assert "recovered" in events and "tmux_create" not in events and "kill" not in events
    assert _session(db) == selected
    db.close()


def test_guest_preparation_does_not_predict_missing_graph_credentials(tmp_path, monkeypatch):
    from agentworks.orchestration.secrets import secret_union
    from agentworks.sessions.nodes import LiveSessionNode

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda *a, **k: pytest.fail("source preview"))
    monkeypatch.setattr(
        "agentworks.orchestration.secrets.require_predicted_refs", lambda *a, **k: pytest.fail("prediction")
    )
    monkeypatch.setattr(LiveSessionNode, "secret_refs", lambda self: ("unavailable-workload-credential",))
    prepared = _prepare_launch(db, monkeypatch)
    assert secret_union(prepared.secret_nodes) == ("unavailable-workload-credential",)
    assert len(prepared.secret_targets) == 1
    db.close()


def test_empty_resource_cleanup_propagates_cancellation(monkeypatch):
    from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource

    failure = UserAbort("cancelled")

    def cancel():
        raise failure

    with pytest.raises(UserAbort) as caught:
        cleanup_now_empty_resource(
            kind="workspace",
            name="empty",
            created=True,
            delete=cancel,
            manual_command="agw workspace delete empty",
            yes=True,
            empty_clause="empty",
            report_clause="empty",
        )
    assert caught.value is failure


def test_batch_pid_repair_propagates_cancellation(tmp_path, monkeypatch):
    from agentworks.sessions.manager import _pids

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    db.update_session_runtime(
        "s1",
        socket_path="/run/agentworks/agent-tmux-sockets/agt-a1/s1.sock",
        pid=None,
        boot_id=None,
        tmux_server_start_ticks=None,
    )
    failure = UserAbort("cancelled")

    def cancel(*args, **kwargs):
        raise failure

    monkeypatch.setattr(_pids, "_repair_session_pid", cancel)
    with pytest.raises(UserAbort) as caught:
        manager.ensure_pids_batch([_session(db)], db=db, config=SimpleNamespace())
    assert caught.value is failure
    db.close()


def test_pending_runtime_ownership_probe_propagates_cancellation(tmp_path, monkeypatch):
    from uuid import uuid4

    from agentworks.sessions import tmux
    from agentworks.sessions.manager import _status

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    db.set_session_run_id("s1", str(uuid4()))
    session = _session(db)
    pending = replace(session, pid=None)
    failure = UserAbort("cancelled")

    def cancel(**kwargs):
        raise failure

    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", cancel)
    target = manager.transport(_vm(db), SimpleNamespace())
    with pytest.raises(UserAbort) as caught:
        _status._pending_launch_is_owned(pending, target=target)
    assert caught.value is failure
    db.close()


def test_create_below_stopped_parent_refuses_before_activation(tmp_path, monkeypatch):
    from agentworks.sessions.manager import _create

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    vm = _vm(db)
    db.set_vm_desired_runtime_state("vm1", DesiredRuntimeState.STOPPED, expected_vm_uuid=vm.vm_uuid)
    monkeypatch.setattr(_create, "_build_session_graph", lambda *a, **k: pytest.fail("graph activation preparation"))
    with pytest.raises(StateError):
        manager.create_session(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="new-session",
            workspace="ws1",
            admin=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert db.get_session("new-session") is None
    assert _session(db).desired_runtime_state is DesiredRuntimeState.RUNNING
    db.close()


def test_local_definition_rejects_canonical_identity_before_guest_or_secret_work(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    selected = _session(db)
    failure = StateError("configured SSH identity is unavailable")

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("guest transport created"))
    with pytest.raises(StateError) as caught:
        _prepare_launch(db, monkeypatch)
    assert caught.value is failure
    assert _session(db) == selected
    db.close()


def test_delete_consent_cannot_adopt_same_name_replacement(tmp_path, monkeypatch):
    from agentworks.db import SessionMode

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    selected = _session(db)

    def replace_while_confirming(*args, **kwargs):
        db.delete_session("s1")
        db.insert_session(
            "s1",
            "ws1",
            "claude",
            SessionMode.AGENT,
            agent_name="a1",
            socket_path="/run/agentworks/agent-tmux-sockets/agt-a1/s1.sock",
        )
        return True

    monkeypatch.setattr("agentworks.sessions.manager._queries.output.confirm", replace_while_confirming)
    monkeypatch.setattr(
        "agentworks.artifacts.session.cleanup_session_artifacts",
        lambda *a, **k: pytest.fail("replacement artifacts removed"),
    )
    with pytest.raises(StateError):
        manager.delete_session(db, SimpleNamespace(), name="s1", interaction=TtyInteractionPolicy.ALLOW)
    assert _session(db).session_uuid != selected.session_uuid
    db.close()


@pytest.mark.parametrize("status", [SessionStatus.STOPPED, SessionStatus.RESIDUAL, SessionStatus.BROKEN])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("after_repair", [False, True])
def test_recovery_only_action_never_widens_after_preparation(tmp_path, monkeypatch, status, force, after_repair):
    from agentworks.sessions import tmux
    from agentworks.sessions.manager._execute import execute_session_start

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.RUNNING)
    monkeypatch.setattr(tmux, "startup_protection_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        "agentworks.harness_setup.readiness.require_setup_ready",
        lambda *a, **k: pytest.fail("recovery performed launch setup"),
    )
    prepared = _prepare_launch(db, monkeypatch, status=SessionStatus.RUNNING, force=force)
    assert prepared.recovered_startup and not prepared.requires_launch
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda *a, **k: pytest.fail("source reopened"))
    before = _session(db)
    if after_repair:
        monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.RUNNING)

        def repair(session, **kwargs):
            monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: status)
            return session

        monkeypatch.setattr(manager, "_ensure_pid", repair)
    else:
        monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: status)
        monkeypatch.setattr(manager, "_ensure_pid", lambda *a, **k: pytest.fail("invalid recovery wrote evidence"))
    monkeypatch.setattr(
        "agentworks.artifacts.session.prepare_session_artifacts", lambda *a, **k: pytest.fail("compiled launch")
    )
    monkeypatch.setattr(
        "agentworks.artifacts.session.stage_session_artifacts", lambda *a, **k: pytest.fail("staged launch")
    )
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: pytest.fail("killed runtime"))
    monkeypatch.setattr(tmux, "create_session", lambda *a, **k: pytest.fail("spawned without launch preparation"))
    with pytest.raises(StateError):
        execute_session_start(prepared, secret_values={}, graph_secret_values={})
    assert _session(db) == before
    db.close()
