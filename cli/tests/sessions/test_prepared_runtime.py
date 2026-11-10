"""Prepared session boundaries preserve intent, identity, and read-only selection."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
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


def test_pure_setter_preserves_runtime_and_start_identity(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    before = db.get_session("s1")
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("registry loaded"))
    manager.set_session_desired_runtime_state(db, name="s1", state="stopped")
    after = db.get_session("s1")
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
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.STOPPED
    db.close()


def test_acceptance_reuses_guard_and_refuses_replaced_uuid(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    selected = db.get_session("s1")
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
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.STOPPED
    db.close()


def _definition(db, monkeypatch, *, status):
    session = db.get_session("s1")
    workspace = db.get_workspace(session.workspace_name)
    vm = db.get_vm(workspace.vm_name)
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: status)
    monkeypatch.setattr(manager, "_ensure_pid", lambda *a, **k: pytest.fail("PID evidence mutated"))
    return SimpleNamespace(
        db=db,
        config=SimpleNamespace(),
        registry=object(),
        session=session,
        workspace=workspace,
        vm=vm,
        replace_running=False,
        force=False,
        intent=HarnessLaunchIntent.RESUME_OR_NEW,
        running_restart_authorized=False,
        env_target=object(),
    )


def test_running_noop_preparation_never_repairs_or_uses_env_targets(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    definition = _definition(db, monkeypatch, status=SessionStatus.RUNNING)
    monkeypatch.setattr(manager, "_build_session_target", lambda *a, **k: pytest.fail("owning transport probed"))
    prepared = prepare_session_launch(definition, interaction=TtyInteractionPolicy.REFUSE)
    assert prepared.noop and prepared.secret_targets == ()
    db.close()


@pytest.mark.parametrize("status", [SessionStatus.UNKNOWN, SessionStatus.BROKEN])
def test_ineligible_preparation_never_repairs_or_collects_values(tmp_path, monkeypatch, status):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    definition = _definition(db, monkeypatch, status=status)
    with pytest.raises(StateError):
        prepare_session_launch(definition, interaction=TtyInteractionPolicy.REFUSE)
    db.close()


def test_off_parent_stop_has_no_transport_or_runtime_mutation(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("transport created"))
    prepared = prepare_session_stop(db, SimpleNamespace(), db.get_session("s1"), parent_off=True)
    assert prepared.parent_off and prepared.observed_status is SessionStatus.STOPPED
    assert execute_session_stop(prepared) is False
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.RUNNING
    db.close()


def test_stop_executor_propagates_cancellation(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    failure = UserAbort("cancelled")

    def cancel(*args, **kwargs):
        raise failure

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", cancel)
    with pytest.raises(UserAbort) as caught:
        manager._execute_stop([(db.get_session("s1"), object(), True)], db=db)
    assert caught.value is failure
    db.close()


def _prepare_launch(db, monkeypatch, *, status=SessionStatus.STOPPED, replace_running=False, force=False):
    from agentworks.bootstrap import load_request_registry
    from agentworks.sessions.manager._runtime import prepare_session_definition
    from agentworks.vms.nodes import live_vm_node

    config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
    registry = load_request_registry(config, live_database=db)
    session = db.get_session("s1")
    vm = db.get_vm("vm1")
    definition = prepare_session_definition(
        db,
        config,
        registry,
        session,
        vm_node=live_vm_node(db, config, registry, vm),
        replace_running=replace_running,
        running_restart_authorized=replace_running,
        force=force,
    )
    return prepare_session_launch(definition, interaction=TtyInteractionPolicy.REFUSE)


@pytest.mark.parametrize("status", [SessionStatus.STOPPED, SessionStatus.RESIDUAL])
def test_eligible_preparation_defers_pid_writes_and_keeps_session_scope(tmp_path, monkeypatch, status):
    db, events = _restart_fixture(tmp_path, monkeypatch, status=status)
    before = db.get_session("s1")
    monkeypatch.setattr(manager, "_ensure_pid", lambda *a, **k: pytest.fail("PID repair during preparation"))
    prepared = _prepare_launch(db, monkeypatch, status=status)
    assert prepared.observed_status is status and not prepared.noop
    assert len(prepared.secret_targets) == 1
    assert prepared.definition.scope.session == "s1"
    assert prepared.definition.scope.agent == "a1" and not prepared.definition.scope.admin
    assert db.get_session("s1") == before
    assert "kill" not in events and "tmux_create" not in events
    db.close()


def test_executor_uses_supplied_values_and_preserves_target(tmp_path, monkeypatch):
    from agentworks.sessions.manager._execute import execute_session_start

    db, events = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    manager.set_session_desired_runtime_state(db, name="s1", state="stopped")
    selected = db.get_session("s1")
    prepared = _prepare_launch(db, monkeypatch)
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", lambda *a, **k: pytest.fail("source reached executor")
    )
    assert execute_session_start(prepared, secret_values={}, graph_secret_values={})
    after = db.get_session("s1")
    assert after.session_uuid == selected.session_uuid
    assert after.desired_runtime_state is DesiredRuntimeState.STOPPED
    assert after.pid == 4243 and after.tmux_server_start_ticks == 1
    assert events.count("tmux_create") == 1
    db.close()
