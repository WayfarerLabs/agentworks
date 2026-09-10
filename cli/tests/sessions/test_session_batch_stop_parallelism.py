"""Bounded, state-safe parallel teardown for ``session stop --all``."""

from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor as PythonThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks import output as output_mod
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager import _lifecycle as lifecycle_mod
from agentworks.sessions.manager import _teardown as teardown_mod
from agentworks.sessions.manager._teardown import (
    DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
    DedicatedTeardownEvidence,
    DedicatedTeardownPlan,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport
    from tests.conftest import CapturedOutput


BOOT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _Target:
    def __init__(self, label: str) -> None:
        self.label = label

    def run(self, command: str, **kwargs: object) -> object:
        raise AssertionError(f"unexpected remote command on {self.label}: {command} {kwargs}")


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def compare_and_set_session_stopped(self, name: str, **kwargs: object) -> None:
        self.calls.append((threading.get_ident(), name))


def _plan(
    name: str,
    *,
    vm_name: str = "box",
    socket_path: str | None = None,
    pid: int | None = None,
    start_ticks: int | None = None,
    target: _Target | None = None,
) -> DedicatedTeardownPlan:
    ordinal = int(name.rsplit("-", 1)[-1]) if name.rsplit("-", 1)[-1].isdigit() else 1
    return DedicatedTeardownPlan(
        session_name=name,
        vm_name=vm_name,
        socket_path=socket_path or f"/run/agentworks/admin-tmux-sockets/agentworks/{name}.sock",
        stored_pid=pid or 1000 + ordinal,
        stored_boot_id=BOOT_ID,
        stored_start_ticks=start_ticks or 2000 + ordinal,
        target=cast("Transport", target or _Target(name)),
        sudo=False,
        force=False,
    )


def _seed_session(
    db: Database,
    name: str,
    *,
    socket_path: str | None,
    pid: int,
    start_ticks: int | None,
) -> SessionRow:
    if db.get_vm("box") is None:
        db.insert_vm("box", site="test", hostname="box")
        db.update_vm_tailscale("box", "100.64.0.10")
        db.insert_workspace("work", "/srv/work", "box", "ws-work")
    db.insert_session(name, "work", "default", SessionMode.ADMIN, socket_path=socket_path)
    db._conn.execute(
        "UPDATE sessions SET pid = ?, boot_id = ?, tmux_server_start_ticks = ? WHERE name = ?",
        (pid, BOOT_ID, start_ticks, name),
    )
    db._conn.commit()
    session = db.get_session(name)
    assert session is not None
    return session


def test_concurrent_teardowns_overlap_under_fixed_bound_and_reconcile_on_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = [_plan(f"session-{index}", vm_name="same-vm") for index in range(12)]
    active = 0
    maximum_active = 0
    active_lock = threading.Lock()
    first_wave_ready = threading.Event()
    worker_threads: set[int] = set()
    output_threads: list[int] = []

    def execute(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
        nonlocal active, maximum_active
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            worker_threads.add(threading.get_ident())
            if active == teardown_mod.DEDICATED_TEARDOWN_MAX_WORKERS:
                first_wave_ready.set()
        assert first_wave_ready.wait(timeout=2)
        with active_lock:
            active -= 1
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", execute)
    monkeypatch.setattr(output_mod, "info", lambda message: output_threads.append(threading.get_ident()))
    db = _RecordingDB()
    main_thread = threading.get_ident()

    failures = teardown_mod.execute_concurrent_dedicated_teardowns(plans, db=db)  # type: ignore[arg-type]

    assert failures == []
    assert maximum_active == teardown_mod.DEDICATED_TEARDOWN_MAX_WORKERS
    assert worker_threads and main_thread not in worker_threads
    assert {thread_id for thread_id, _name in db.calls} == {main_thread}
    assert set(output_threads) == {main_thread}
    assert {name for _thread_id, name in db.calls} == {plan.session_name for plan in plans}
    assert len({id(plan.target) for plan in plans}) == len(plans)


@pytest.mark.parametrize("collision", ["socket", "process"])
def test_collision_refuses_before_executor_or_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    first = _plan("first", socket_path="/run/agentworks/admin-tmux-sockets/agentworks/first.sock")
    if collision == "socket":
        second = _plan("second", socket_path=first.socket_path, pid=9002)
    else:
        second = _plan(
            "second",
            socket_path="/run/agentworks/admin-tmux-sockets/agentworks/second.sock",
            pid=first.stored_pid,
            start_ticks=first.stored_start_ticks + 1,
        )

    monkeypatch.setattr(
        teardown_mod,
        "ThreadPoolExecutor",
        lambda **kwargs: pytest.fail("executor created before collision refusal"),
    )

    with pytest.raises(StateError):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [first, second],
            db=_RecordingDB(),  # type: ignore[arg-type]
        )


def test_database_compare_and_set_is_atomic_conflict_safe_and_retry_safe(db: Database) -> None:
    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=201)

    db.compare_and_set_session_stopped(
        "session",
        expected_socket_path=socket_path,
        expected_pid=101,
        expected_boot_id=BOOT_ID,
        expected_tmux_server_start_ticks=201,
    )
    stopped = db.get_session("session")
    assert stopped is not None and stopped.pid == PID_STOPPED

    db.compare_and_set_session_stopped(
        "session",
        expected_socket_path=socket_path,
        expected_pid=101,
        expected_boot_id=BOOT_ID,
        expected_tmux_server_start_ticks=201,
    )

    db.update_session_runtime(
        "session",
        socket_path=socket_path,
        pid=102,
        boot_id=BOOT_ID,
        tmux_server_start_ticks=202,
    )
    with pytest.raises(StateError):
        db.compare_and_set_session_stopped(
            "session",
            expected_socket_path=socket_path,
            expected_pid=101,
            expected_boot_id=BOOT_ID,
            expected_tmux_server_start_ticks=201,
        )
    current = db.get_session("session")
    assert current is not None and current.pid == 102


def test_incomplete_fingerprint_selects_serial_compatibility(db: Database) -> None:
    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=None)

    plan = teardown_mod.prepare_concurrent_dedicated_teardown(
        db,
        session,
        vm_name="box",
        target=cast("Transport", _Target("bounded")),
        target_owns_session=True,
        force=False,
    )

    assert plan is None


def test_invalid_complete_fingerprint_fails_preparation_closed(db: Database) -> None:
    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=-2, start_ticks=201)

    with pytest.raises(StateError):
        teardown_mod.prepare_concurrent_dedicated_teardown(
            db,
            session,
            vm_name="box",
            target=cast("Transport", _Target("bounded")),
            target_owns_session=True,
            force=False,
        )


def test_incomplete_synchronous_teardown_persists_fingerprint_before_kill(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions import tmux as tmux_mod
    from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus, TmuxServerFingerprint

    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=None)
    events: list[str] = []
    real_update = db.update_session_runtime

    def update(name: str, **kwargs: object) -> None:
        events.append("fingerprint" if kwargs["pid"] == 101 else "stopped")
        real_update(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db, "update_session_runtime", update)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(
            ProbeStatus.PRESENT,
            TmuxServerFingerprint(pid=101, boot_id=BOOT_ID, start_ticks=201),
        ),
    )

    def execute(*args: object, **kwargs: object) -> DedicatedTeardownEvidence:
        events.append("kill")
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "_execute_reachable_dedicated_teardown", execute)

    lifecycle_mod._teardown_session(
        session,
        target=cast("Transport", _Target("serial")),
        target_owns_session=True,
        db=db,
        force=False,
    )

    assert events == ["fingerprint", "kill", "stopped"]


def test_batch_partitions_complete_work_before_serial_compatibility_and_uses_distinct_targets(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    complete_a = _seed_session(
        db,
        "complete-a",
        socket_path="/run/agentworks/admin-tmux-sockets/agentworks/complete-a.sock",
        pid=101,
        start_ticks=201,
    )
    complete_b = _seed_session(
        db,
        "complete-b",
        socket_path="/run/agentworks/admin-tmux-sockets/agentworks/complete-b.sock",
        pid=102,
        start_ticks=202,
    )
    incomplete = _seed_session(
        db,
        "incomplete",
        socket_path="/run/agentworks/admin-tmux-sockets/agentworks/incomplete.sock",
        pid=103,
        start_ticks=None,
    )
    legacy = _seed_session(db, "legacy", socket_path=None, pid=104, start_ticks=None)
    selected = [complete_a, complete_b, incomplete, legacy]
    statuses = {session.name: SessionStatus.RUNNING for session in selected}

    monkeypatch.setattr(session_manager, "_batch_vm_boundary", lambda *args, **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(session_manager, "ensure_pids_batch", lambda sessions, **kwargs: sessions)
    monkeypatch.setattr(session_manager, "observe_session_statuses", lambda sessions, **kwargs: statuses)

    transport_calls: list[tuple[int | None, _Target]] = []

    def transport(vm: object, config: object, *, default_timeout: int | None = None) -> _Target:
        target = _Target(f"target-{len(transport_calls)}")
        transport_calls.append((default_timeout, target))
        return target

    monkeypatch.setattr(session_manager, "transport", transport)
    events: list[str] = []
    concurrent_targets: list[object] = []

    def concurrent(plans: list[DedicatedTeardownPlan], *, db: Database) -> list[tuple[str, str]]:
        events.append("concurrent")
        concurrent_targets.extend(plan.target for plan in plans)
        assert [plan.session_name for plan in plans] == ["complete-a", "complete-b"]
        return []

    def serial(
        targets: list[tuple[SessionRow, Transport, bool]],
        *,
        db: Database,
        force: bool,
    ) -> list[tuple[str, str]]:
        events.append("serial")
        assert [session.name for session, _target, _owns in targets] == ["incomplete", "legacy"]
        assert targets[0][1] is targets[1][1]
        return []

    monkeypatch.setattr(teardown_mod, "execute_concurrent_dedicated_teardowns", concurrent)
    monkeypatch.setattr(lifecycle_mod, "_execute_stop", serial)

    lifecycle_mod.stop_all_sessions(
        db,
        cast("Config", SimpleNamespace()),
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert events == ["concurrent", "serial"]
    assert [timeout for timeout, _target in transport_calls] == [
        DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
        DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
        None,
    ]
    assert len({id(target) for target in concurrent_targets}) == 2
    assert not captured_output.warnings


def test_worker_failure_does_not_suppress_sibling_persistence_or_labeled_warning(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    plans = [_plan("good-1"), _plan("bad-2"), _plan("good-3")]

    def execute(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
        if plan.session_name == "bad-2":
            raise ExternalError("remote failure")
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", execute)
    db = _RecordingDB()

    failures = teardown_mod.execute_concurrent_dedicated_teardowns(plans, db=db)  # type: ignore[arg-type]

    assert [name for name, _error in failures] == ["bad-2"]
    assert {name for _thread_id, name in db.calls} == {"good-1", "good-3"}
    assert len(captured_output.warnings) == 1
    assert "bad-2" in captured_output.warnings[0]


def test_quiet_wait_invokes_heartbeat_before_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
        started.set()
        assert release.wait(timeout=2)
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", execute)
    real_wait = teardown_mod._wait_for_futures  # type: ignore[attr-defined]
    wait_calls = 0

    def controlled_wait(*args: object, **kwargs: object) -> Any:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert started.wait(timeout=1)
            release.set()
            return set(), args[0]
        return real_wait(*args, **kwargs)

    heartbeats: list[int] = []
    monkeypatch.setattr(teardown_mod, "_wait_for_futures", controlled_wait)
    monkeypatch.setattr(teardown_mod, "_heartbeat", lambda futures, *, total: heartbeats.append(total))

    failures = teardown_mod.execute_concurrent_dedicated_teardowns(
        [_plan("session-1")],
        db=_RecordingDB(),  # type: ignore[arg-type]
    )

    assert failures == []
    assert heartbeats == [1]


def test_repeated_interrupts_cancel_queued_work_drain_running_work_and_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    started = threading.Event()
    release = threading.Event()
    executed: list[str] = []

    def execute(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
        executed.append(plan.session_name)
        started.set()
        assert release.wait(timeout=2)
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "DEDICATED_TEARDOWN_MAX_WORKERS", 1)
    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", execute)
    real_wait = teardown_mod._wait_for_futures  # type: ignore[attr-defined]
    wait_calls = 0

    def interrupting_wait(*args: object, **kwargs: object) -> Any:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert started.wait(timeout=1)
            raise KeyboardInterrupt
        if wait_calls == 2:
            release.set()
            raise KeyboardInterrupt
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(teardown_mod, "_wait_for_futures", interrupting_wait)
    db = _RecordingDB()

    with pytest.raises(KeyboardInterrupt):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("running-1"), _plan("queued-2"), _plan("queued-3")],
            db=db,  # type: ignore[arg-type]
        )

    assert executed == ["running-1"]
    assert [name for _thread_id, name in db.calls] == ["running-1"]
    assert len(captured_output.notices) >= 2


def test_interrupt_after_persistence_attempt_retries_reconciliation_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        teardown_mod,
        "execute_dedicated_teardown",
        lambda plan: DedicatedTeardownEvidence.VERIFIED_STOPPED,
    )

    class _InterruptedDB(_RecordingDB):
        def compare_and_set_session_stopped(self, name: str, **kwargs: object) -> None:
            super().compare_and_set_session_stopped(name, **kwargs)
            if len(self.calls) == 1:
                raise KeyboardInterrupt

    db = _InterruptedDB()
    with pytest.raises(KeyboardInterrupt):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("session-1")],
            db=db,  # type: ignore[arg-type]
        )

    assert [name for _thread_id, name in db.calls] == ["session-1", "session-1"]


def test_interrupt_at_complete_map_release_drains_and_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        teardown_mod,
        "execute_dedicated_teardown",
        lambda plan: DedicatedTeardownEvidence.VERIFIED_STOPPED,
    )
    real_release = teardown_mod._SubmissionGate.release
    releases = 0

    def interrupting_release(gate: object) -> None:
        nonlocal releases
        releases += 1
        real_release(gate)  # type: ignore[arg-type]
        if releases == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(teardown_mod._SubmissionGate, "release", interrupting_release)
    db = _RecordingDB()

    with pytest.raises(KeyboardInterrupt):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("session-1")],
            db=db,  # type: ignore[arg-type]
        )

    assert [name for _thread_id, name in db.calls] == ["session-1"]


def test_worker_base_exception_drains_and_persists_successful_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = threading.Barrier(2)

    def execute(plan: DedicatedTeardownPlan) -> DedicatedTeardownEvidence:
        ready.wait(timeout=2)
        if plan.session_name == "escape-1":
            raise SystemExit(7)
        return DedicatedTeardownEvidence.VERIFIED_STOPPED

    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", execute)
    db = _RecordingDB()

    with pytest.raises(SystemExit):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("escape-1"), _plan("good-2")],
            db=db,  # type: ignore[arg-type]
        )

    assert [name for _thread_id, name in db.calls] == ["good-2"]


def test_submission_interrupt_after_enqueue_never_reaches_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _InterruptAfterEnqueue(PythonThreadPoolExecutor):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            super().submit(fn, *args, **kwargs)
            raise KeyboardInterrupt

    monkeypatch.setattr(teardown_mod, "ThreadPoolExecutor", _InterruptAfterEnqueue)
    monkeypatch.setattr(
        teardown_mod,
        "execute_dedicated_teardown",
        lambda plan: calls.append(plan.session_name) or DedicatedTeardownEvidence.VERIFIED_STOPPED,
    )

    with pytest.raises(KeyboardInterrupt):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("session-1")],
            db=_RecordingDB(),  # type: ignore[arg-type]
        )

    assert calls == []


def test_submission_interrupt_after_unregistered_thread_start_never_reaches_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    threads: list[threading.Thread] = []

    class _InterruptAfterUnregisteredStart:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 1

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            thread = threading.Thread(target=fn, args=args, kwargs=kwargs)
            threads.append(thread)
            thread.start()
            raise KeyboardInterrupt

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            return None

    monkeypatch.setattr(teardown_mod, "ThreadPoolExecutor", _InterruptAfterUnregisteredStart)
    monkeypatch.setattr(
        teardown_mod,
        "execute_dedicated_teardown",
        lambda plan: calls.append(plan.session_name) or DedicatedTeardownEvidence.VERIFIED_STOPPED,
    )

    with pytest.raises(KeyboardInterrupt):
        teardown_mod.execute_concurrent_dedicated_teardowns(
            [_plan("session-1")],
            db=_RecordingDB(),  # type: ignore[arg-type]
        )

    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert calls == []
