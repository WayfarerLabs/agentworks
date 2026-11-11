"""Public stop keeps its prepared boundary and native execution policy."""

from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from agentworks.db import (
    PID_STOPPED,
    DatabaseDriverError,
    DesiredRuntimeState,
    SessionMode,
    SessionRuntimeSnapshot,
    SessionStatus,
    VMStatus,
)
from agentworks.errors import StateConflictError, StateError, UserAbort
from agentworks.sessions import manager
from agentworks.sessions.manager import _teardown
from tests.runtime_state.test_composition import BOOT, REFUSE, seed_session, seed_vm
from tests.runtime_state.test_composition import runtime as runtime


def _running(db, statuses, name, vm, *, agent=False) -> None:
    if agent:
        agent_name = f"agent-{vm}"
        if db.get_agent(agent_name) is None:
            db.insert_agent(agent_name, vm, f"agt-{vm}")
        socket = f"/run/agentworks/agent-tmux-sockets/agt-{vm}/{name}.sock"
        db.insert_session(name, f"ws-{vm}", "default", SessionMode.AGENT, agent_name=agent_name, socket_path=socket)
    else:
        seed_session(db, name, vm)
        socket = db.get_session(name).socket_path
    ordinal = len(statuses) + 1
    db.update_session_runtime(
        name, socket_path=socket, pid=100 + ordinal, boot_id=BOOT, tmux_server_start_ticks=200 + ordinal
    )
    statuses[name] = SessionStatus.RUNNING


def test_public_bulk_stops_overlap_with_private_admin_targets_and_main_thread_persistence(
    db, make_config, runtime, monkeypatch
):
    _, statuses, events = runtime
    seed_vm(db, "a")
    seed_vm(db, "b")
    for ordinal in range(12):
        _running(db, statuses, f"s-{ordinal}", "a" if ordinal % 2 else "b", agent=bool(ordinal % 2))
    main_thread = threading.get_ident()
    created = []

    def transport(vm, config, **kwargs):
        target = SimpleNamespace(vm=vm.name, policy=kwargs)
        created.append(target)
        return target

    monkeypatch.setattr(manager, "transport", transport)
    monkeypatch.setattr(manager, "_build_session_target", lambda *a, **k: pytest.fail("unused direct-agent SSH"))
    wave_ready = threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    worker_threads = set()
    worker_targets = []

    def execute(plan):
        nonlocal active, maximum
        assert plan.target.policy == {"default_timeout": 10, "retries": 1}
        assert plan.sudo == (plan.vm_name == "a")
        with lock:
            active += 1
            maximum = max(maximum, active)
            worker_threads.add(threading.get_ident())
            worker_targets.append(plan.target)
            if active == 8:
                wave_ready.set()
        assert wave_ready.wait(timeout=2)
        statuses[plan.session_name] = SessionStatus.STOPPED
        with lock:
            active -= 1

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    persisted = []
    cas = db.compare_and_set_session_stopped

    def persist(snapshot):
        persisted.append(threading.get_ident())
        cas(snapshot)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", persist)
    manager.stop_all_sessions(db, make_config(), interaction=REFUSE)

    assert maximum == 8
    assert worker_threads and main_thread not in worker_threads
    assert set(persisted) == {main_thread}
    assert len({id(target) for target in worker_targets}) == 12
    assert all(
        row.pid == PID_STOPPED and row.desired_runtime_state is DesiredRuntimeState.STOPPED
        for row in db.list_sessions()
    )
    assert not events


@pytest.mark.parametrize("bulk", [False, True])
def test_filtered_one_bulk_uses_worker_while_named_stop_is_synchronous(db, make_config, runtime, monkeypatch, bulk):
    _, statuses, events = runtime
    seed_vm(db, "box")
    _running(db, statuses, "work", "box")
    main_thread = threading.get_ident()
    workers = []
    synchronous = []

    def execute(plan):
        workers.append(threading.get_ident())
        statuses[plan.session_name] = SessionStatus.STOPPED

    def teardown(session, **kwargs):
        synchronous.append(threading.get_ident())
        db.compare_and_set_session_stopped(SessionRuntimeSnapshot.from_session(session))
        statuses[session.name] = SessionStatus.STOPPED

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    monkeypatch.setattr(manager, "_teardown_session", teardown)
    monkeypatch.setattr(manager, "_build_session_target", lambda *a, **k: manager.transport(db.get_vm("box"), object()))
    if bulk:
        manager.stop_all_sessions(db, make_config(), workspace_name="ws-box", interaction=REFUSE)
        assert len(workers) == 1 and workers[0] != main_thread
        assert not synchronous
    else:
        manager.stop_session(db, make_config(), name="work", interaction=REFUSE)
        assert synchronous == [main_thread]
        assert not workers
    assert db.get_session("work").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert not events


def test_unknown_runtime_refuses_every_bulk_teardown_after_accepting_intent(db, make_config, runtime, monkeypatch):
    _, statuses, events = runtime
    seed_vm(db, "box")
    _running(db, statuses, "unknown", "box")
    _running(db, statuses, "healthy", "box")
    statuses["unknown"] = SessionStatus.UNKNOWN
    before = {row.name: (row.pid, row.boot_id, row.tmux_server_start_ticks) for row in db.list_sessions()}
    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", lambda *a: pytest.fail("bulk mutated"))
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: pytest.fail("serial mutated"))
    with pytest.raises(StateError):
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
    assert {row.name: (row.pid, row.boot_id, row.tmux_server_start_ticks) for row in db.list_sessions()} == before
    assert all(row.desired_runtime_state is DesiredRuntimeState.STOPPED for row in db.list_sessions())
    assert not events


@pytest.mark.parametrize("signal", [UserAbort, KeyboardInterrupt])
@pytest.mark.parametrize("interruptions", [1, 2])
def test_bulk_completed_siblings_remain_completed_after_observer_cancellation(
    db, make_config, runtime, monkeypatch, signal, interruptions
):
    _, statuses, _ = runtime
    seed_vm(db, "box")
    for name in ["one", "two"]:
        _running(db, statuses, name, "box")
    both_started = threading.Barrier(2)

    def execute(plan):
        both_started.wait(timeout=2)
        statuses[plan.session_name] = SessionStatus.STOPPED

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    import agentworks.runtime_state._session_stop as stop

    original = stop._completed
    calls: Counter[str] = Counter()
    errors = [signal(f"cancelled after persistence {index}") for index in range(interruptions)]

    def completed(invocation, name, *, changed):
        calls[name] += 1
        index = sum(calls.values()) - 1
        if index < len(errors):
            raise errors[index]
        original(invocation, name, changed=changed)

    monkeypatch.setattr(stop, "_completed", completed)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(signal) as caught:
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
    assert caught.value is errors[0]
    assert all(row.pid == PID_STOPPED for row in db.list_sessions())
    assert {row.name for row in rendered if row.kind == "session" and row.outcome.value == "changed"} == {"one", "two"}
    assert sum(calls.values()) == 2 + interruptions


@pytest.mark.parametrize("bulk", [False, True])
def test_off_parent_explicit_stop_never_opens_transport_repairs_observes_guest_or_holds(
    db, make_config, runtime, monkeypatch, bulk
):
    powers, statuses, events = runtime
    seed_vm(db, "box", target=DesiredRuntimeState.STOPPED)
    _running(db, statuses, "work", "box")
    powers["box"] = VMStatus.STOPPED
    for method in ["transport", "ensure_pids_batch", "observe_session_statuses", "_build_session_target"]:
        monkeypatch.setattr(manager, method, lambda *a, **k: pytest.fail("off parent reached guest work"))
    if bulk:
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
    else:
        manager.stop_session(db, make_config(), name="work", interaction=REFUSE)
    assert db.get_session("work").pid == PID_STOPPED
    assert db.get_session("work").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert not events


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("error_type", [StateConflictError, DatabaseDriverError])
def test_real_public_acceptance_survives_failed_repair_before_status_or_teardown(
    db, make_config, runtime, monkeypatch, bulk, error_type
):
    from tests.sessions.test_pid_repair_reconciliation import _observations

    _, statuses, _ = runtime
    seed_vm(db, "box")
    _running(db, statuses, "work", "box")
    from uuid import uuid4

    db.set_session_run_id("work", str(uuid4()))
    selected = db.get_session("work")
    db.update_session_runtime(
        "work", socket_path=selected.socket_path, pid=None, boot_id=BOOT, tmux_server_start_ticks=None
    )
    _observations(monkeypatch, "recovered", lambda: None)
    error = error_type("repair write refused")

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(db, "compare_and_set_session_runtime_repair", refuse)
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: pytest.fail("status after failed repair"))
    monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("status after failed repair"))
    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", lambda *a: pytest.fail("failed repair dispatched"))
    with pytest.raises(error_type) as caught:
        if bulk:
            manager.stop_all_sessions(db, make_config(), interaction=REFUSE)
        else:
            manager.stop_session(db, make_config(), name="work", interaction=REFUSE)
    assert caught.value is error
    current = db.get_session("work")
    assert current.desired_runtime_state is DesiredRuntimeState.STOPPED
    assert current.session_uuid == selected.session_uuid
    assert current.pid is None and current.boot_id == BOOT
