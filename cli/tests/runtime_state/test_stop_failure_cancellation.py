"""Cancellation preserves established failures and verified successful siblings."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor as PythonThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from time import monotonic

import pytest

from agentworks.db import PID_STOPPED, DatabaseDriverError, DesiredRuntimeState
from agentworks.errors import ExternalError, UserAbort
from agentworks.runtime_state._models import Outcome, RuntimeInvocation
from agentworks.sessions import manager
from agentworks.sessions.manager import _teardown
from tests.runtime_state.test_composition import REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_public_stop import _running


@pytest.mark.parametrize("failure_stage", ["native", "cas", "observer"])
@pytest.mark.parametrize("signal_type", [UserAbort, KeyboardInterrupt])
@pytest.mark.parametrize("interruptions", [1, 2])
@pytest.mark.parametrize("cancel_on", ["success", "failure"])
def test_bulk_failure_receipt_survives_notification_cancellation_and_queued_work(
    db, make_config, runtime, monkeypatch, failure_stage, signal_type, interruptions, cancel_on
):
    _, statuses, _ = runtime
    seed_vm(db, "box")
    for name in ["bad", "good", "queued"]:
        _running(db, statuses, name, "box")
    before = {row.name: row for row in db.list_sessions()}
    caller = threading.get_ident()
    ready = threading.Barrier(2)
    native: Counter[str] = Counter()

    class QueuedFuture(Future[None]):
        def cancel(self) -> bool:
            was_cancelled = self.cancelled()
            cancelled = super().cancel()
            if cancelled and not was_cancelled:
                # Match executor notification of a cancelled queued work item.
                assert not self.set_running_or_notify_cancel()
            return cancelled

    pending = QueuedFuture()

    class Pool(PythonThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            plan = args[2]
            # Hold the third plan queued deterministically while two real workers run.
            return pending if plan.session_name == "queued" else super().submit(*args, **kwargs)

    monkeypatch.setattr(_teardown, "ThreadPoolExecutor", Pool)
    # A broken coordinator must fail instead of waiting forever on the
    # synthetic queued item. The watchdog runs only on the caller thread.
    deadline = monotonic() + 5
    watchdog_fired = False

    def wait(futures, **kwargs):
        nonlocal watchdog_fired
        if monotonic() >= deadline:
            watchdog_fired = True
            pending.cancel()
        kwargs["timeout"] = min(kwargs["timeout"], 0.1)
        return wait_for_futures(futures, **kwargs)

    monkeypatch.setattr(_teardown, "_wait_for_futures", wait)

    def execute(plan):
        native[plan.session_name] += 1
        ready.wait(timeout=2)
        if plan.session_name == "bad" and failure_stage == "native":
            raise ExternalError("native stop failed")

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    original_cas = db.compare_and_set_session_stopped
    writes: Counter[str] = Counter()

    def cas(expected):
        assert threading.get_ident() == caller
        writes[expected.name] += 1
        if expected.name == "bad" and failure_stage == "cas":
            raise DatabaseDriverError("stopped CAS failed")
        original_cas(expected)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", cas)
    import agentworks.runtime_state._session_stop as stop

    original_completed = stop._completed
    original_fail = RuntimeInvocation.fail
    errors = [signal_type(f"notification cancelled {index}") for index in range(interruptions)]
    injected = 0
    failed_receipts = []

    def cancel() -> None:
        nonlocal injected
        if injected < len(errors):
            error = errors[injected]
            injected += 1
            raise error

    def completed(invocation, name, *, changed):
        assert threading.get_ident() == caller
        if name == "bad" and failure_stage == "observer":
            raise RuntimeError("completed receipt failed")
        if name == "good" and cancel_on == "success":
            cancel()
        original_completed(invocation, name, changed=changed)

    def fail(invocation, kind, name, error, *, blocked=False):
        assert threading.get_ident() == caller
        if kind == "session" and name == "bad":
            failed_receipts.append(str(error))
            # Alternate before/after recording to exercise idempotent retry.
            if cancel_on == "failure" and injected == 0:
                cancel()
            original_fail(invocation, kind, name, error, blocked=blocked)
            if cancel_on == "failure":
                cancel()
        else:
            original_fail(invocation, kind, name, error, blocked=blocked)

    monkeypatch.setattr(stop, "_completed", completed)
    monkeypatch.setattr(RuntimeInvocation, "fail", fail)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(signal_type) as caught:
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)

    assert not watchdog_fired
    assert caught.value is errors[0] and injected == interruptions
    outcomes = {row.name: row for row in rendered if row.kind == "session"}
    assert outcomes["bad"].outcome is Outcome.FAILED
    assert outcomes["good"].outcome is Outcome.CHANGED
    assert outcomes["queued"].outcome is Outcome.BLOCKED
    reason = {"native": "native stop failed", "cas": "stopped CAS failed", "observer": "completed receipt failed"}[
        failure_stage
    ]
    assert outcomes["bad"].reason == reason
    assert set(failed_receipts) == {reason}
    assert native == Counter(bad=1, good=1)
    assert writes == Counter(good=1, **({} if failure_stage == "native" else {"bad": 1}))
    assert pending.cancelled()
    assert db.get_session("good").pid == PID_STOPPED
    assert db.get_session("bad").pid == (PID_STOPPED if failure_stage == "observer" else before["bad"].pid)
    assert db.get_session("queued").pid == before["queued"].pid
    assert all(row.desired_runtime_state is DesiredRuntimeState.STOPPED for row in db.list_sessions())
