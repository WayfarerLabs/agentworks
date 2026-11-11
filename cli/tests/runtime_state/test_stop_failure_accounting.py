"""Parent shutdown cannot erase authoritative teardown or persistence failures."""

from __future__ import annotations

from contextlib import closing
from uuid import uuid4

import pytest

from agentworks.db import (
    PID_STOPPED,
    Database,
    DatabaseDriverError,
    DesiredRuntimeState,
    SessionRuntimeSnapshot,
    SessionStatus,
    VMStatus,
)
from agentworks.errors import ExternalError, StateError
from agentworks.runtime_state import reconcile_session
from agentworks.runtime_state._models import Outcome
from agentworks.sessions import manager
from agentworks.sessions.manager import _teardown
from tests.runtime_state.test_composition import REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_public_stop import _running


@pytest.mark.parametrize("failure_stage", ["native", "cas", "observer"])
def test_parent_shutdown_preserves_bulk_failure_and_successful_sibling(
    db, make_config, runtime, monkeypatch, failure_stage
):
    powers, statuses, _ = runtime
    seed_vm(db, "box")
    for name in ["bad", "good"]:
        _running(db, statuses, name, "box")
    before = db.get_session("bad")

    def execute(plan):
        powers["box"] = VMStatus.STOPPED
        if plan.session_name == "bad" and failure_stage == "native":
            raise ExternalError("uncertain native teardown")

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    original_cas = db.compare_and_set_session_stopped

    def cas(expected):
        if expected.name == "bad" and failure_stage == "cas":
            raise DatabaseDriverError("stopped persistence refused")
        original_cas(expected)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", cas)
    import agentworks.runtime_state._session_stop as stop

    original_completed = stop._completed

    def completed(invocation, name, *, changed):
        if name == "bad" and failure_stage == "observer":
            raise RuntimeError("completion recording refused")
        original_completed(invocation, name, changed=changed)

    monkeypatch.setattr(stop, "_completed", completed)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(StateError):
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)

    outcomes = {row.name: row.outcome for row in rendered if row.kind == "session"}
    assert outcomes == {"bad": Outcome.FAILED, "good": Outcome.CHANGED}
    current = db.get_session("bad")
    assert current.pid == (PID_STOPPED if failure_stage == "observer" else before.pid)
    assert current.session_uuid == before.session_uuid
    assert db.get_session("good").pid == PID_STOPPED
    assert all(row.desired_runtime_state is DesiredRuntimeState.STOPPED for row in db.list_sessions())


@pytest.mark.parametrize("operation", ["named", "serial-bulk"])
@pytest.mark.parametrize("failure_stage", ["native", "cas", "observer"])
def test_parent_shutdown_does_not_hide_named_or_serial_execution_failure(
    db, make_config, runtime, monkeypatch, operation, failure_stage
):
    powers, statuses, _ = runtime
    seed_vm(db, "box")
    _running(db, statuses, "bad", "box")
    if operation == "serial-bulk":
        seed_vm(db, "other")
        _running(db, statuses, "good", "other")
        # Historical dedicated rows without ticks use the serial compatibility lane.
        db._conn.execute("UPDATE sessions SET tmux_server_start_ticks = NULL")
        db._conn.commit()
    before = db.get_session("bad")
    original_cas = db.compare_and_set_session_stopped

    def cas(expected):
        if expected.name == "bad" and failure_stage == "cas":
            raise DatabaseDriverError("stopped persistence refused")
        original_cas(expected)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", cas)

    def execute(session, **kwargs):
        if session.name == "bad":
            powers["box"] = VMStatus.STOPPED
            if failure_stage == "native":
                raise ExternalError("uncertain native teardown")
        db.compare_and_set_session_stopped(SessionRuntimeSnapshot.from_session(session))
        statuses[session.name] = SessionStatus.STOPPED

    monkeypatch.setattr(manager, "_teardown_session", execute)
    monkeypatch.setattr(
        manager, "_build_session_target", lambda *args, **kwargs: manager.transport(db.get_vm("box"), object())
    )
    import agentworks.runtime_state._session_stop as stop

    original_completed = stop._completed

    def completed(invocation, name, *, changed):
        if name == "bad" and failure_stage == "observer":
            raise RuntimeError("completion recording refused")
        original_completed(invocation, name, changed=changed)

    monkeypatch.setattr(stop, "_completed", completed)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(StateError):
        if operation == "named":
            manager.stop_session(db, make_config(), name="bad", interaction=REFUSE)
        else:
            manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
    outcomes = {row.name: row.outcome for row in rendered if row.kind == "session"}
    assert outcomes["bad"] is Outcome.FAILED
    assert db.get_session("bad").pid == (PID_STOPPED if failure_stage == "observer" else before.pid)
    if operation == "serial-bulk":
        assert outcomes["good"] is Outcome.CHANGED
        assert db.get_session("good").pid == PID_STOPPED


@pytest.mark.parametrize("operation", ["named", "bulk", "reconcile"])
def test_off_parent_stopped_cas_conflict_remains_failed_and_preserves_new_run(
    db, make_config, runtime, monkeypatch, operation
):
    powers, statuses, _ = runtime
    seed_vm(db, "box", target=DesiredRuntimeState.STOPPED)
    _running(db, statuses, "bad", "box")
    if operation == "bulk":
        _running(db, statuses, "good", "box")
    if operation == "reconcile":
        manager.set_session_desired_runtime_state(db, name="bad", state=DesiredRuntimeState.STOPPED)
    powers["box"] = VMStatus.STOPPED
    before = db.get_session("bad")
    replacement_run = str(uuid4())
    original_cas = db.compare_and_set_session_stopped

    def cas(expected):
        if expected.name == "bad":
            with closing(Database(db.path)) as other:
                other.set_session_run_id("bad", replacement_run)
        original_cas(expected)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", cas)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(StateError):
        if operation == "bulk":
            manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
        elif operation == "named":
            manager.stop_session(db, make_config(), name="bad", interaction=REFUSE)
        else:
            reconcile_session(db, make_config(), name="bad", interaction=REFUSE)

    outcomes = {row.name: row.outcome for row in rendered if row.kind == "session"}
    assert outcomes["bad"] is Outcome.FAILED
    current = db.get_session("bad")
    assert current.run_id == replacement_run
    assert current.pid == before.pid and current.session_uuid == before.session_uuid
    assert current.desired_runtime_state is DesiredRuntimeState.STOPPED
    if operation == "bulk":
        assert outcomes["good"] is Outcome.CHANGED
        assert db.get_session("good").pid == PID_STOPPED
