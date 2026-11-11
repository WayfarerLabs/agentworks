"""A broken failure receiver cannot turn failed teardown into command success."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor as PythonThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures

import pytest

from agentworks.db import PID_STOPPED, DatabaseDriverError, DesiredRuntimeState, VMStatus
from agentworks.errors import ExternalError, UserAbort
from agentworks.runtime_state._models import Outcome, RuntimeInvocation
from agentworks.sessions import manager
from agentworks.sessions.manager import _teardown
from tests.runtime_state.test_composition import REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_public_stop import _running


@pytest.mark.parametrize("failure_stage", ["native", "cas"])
@pytest.mark.parametrize("prior_cancellation", [None, UserAbort, KeyboardInterrupt])
def test_failure_receiver_error_drains_and_propagates_without_replaying_known_failure(
    db, make_config, runtime, monkeypatch, failure_stage, prior_cancellation
):
    powers, statuses, _ = runtime
    seed_vm(db, "box")
    for name in ["bad", "good", "queued"]:
        _running(db, statuses, name, "box")
    before = {row.name: row for row in db.list_sessions()}
    caller = threading.get_ident()
    ready = threading.Barrier(2)
    native: Counter[str] = Counter()
    futures = {}
    draining = threading.Event()
    drain_errors = []
    original_drain = _teardown._enter_reconciliation

    def drain(pending, error):
        drain_errors.append(error)
        draining.set()
        original_drain(pending, error)

    monkeypatch.setattr(_teardown, "_enter_reconciliation", drain)

    class QueuedFuture(Future[None]):
        def cancel(self) -> bool:
            was_cancelled = self.cancelled()
            cancelled = super().cancel()
            if cancelled and not was_cancelled:
                assert not self.set_running_or_notify_cancel()
            return cancelled

    queued = QueuedFuture()

    class Pool(PythonThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            plan = args[2]
            future = queued if plan.session_name == "queued" else super().submit(*args, **kwargs)
            futures[plan.session_name] = future
            return future

    monkeypatch.setattr(_teardown, "ThreadPoolExecutor", Pool)

    def wait(pending, **kwargs):
        # When testing an earlier cancellation, establish it on the good receipt
        # before the bad receipt's receiver fails. Otherwise fail first and drain good.
        first = futures["good" if prior_cancellation else "bad"]
        selected = {first} if first in pending else pending
        return wait_for_futures(selected, **kwargs)

    monkeypatch.setattr(_teardown, "_wait_for_futures", wait)

    def execute(plan):
        native[plan.session_name] += 1
        ready.wait(timeout=2)
        if plan.session_name == "good" and not prior_cancellation:
            assert draining.wait(timeout=5)
        powers["box"] = VMStatus.STOPPED
        if plan.session_name == "bad" and failure_stage == "native":
            raise ExternalError("native teardown failed")

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", execute)
    original_cas = db.compare_and_set_session_stopped
    writes: Counter[str] = Counter()

    def cas(expected):
        assert threading.get_ident() == caller
        writes[expected.name] += 1
        if expected.name == "bad" and failure_stage == "cas":
            raise DatabaseDriverError("stopped persistence failed")
        original_cas(expected)

    monkeypatch.setattr(db, "compare_and_set_session_stopped", cas)
    import agentworks.runtime_state._session_stop as stop

    original_completed = stop._completed
    original_fail = RuntimeInvocation.fail
    original_close = RuntimeInvocation.close
    receiver_error = RuntimeError("failure receiver unavailable")
    primary = prior_cancellation("earlier cancellation") if prior_cancellation else receiver_error
    cancellation_raised = False
    failure_receipts = []
    final_results = {}
    completed_names = set()

    def completed(invocation, name, *, changed):
        nonlocal cancellation_raised
        assert threading.get_ident() == caller
        if name == "good" and prior_cancellation and not cancellation_raised:
            cancellation_raised = True
            raise primary
        original_completed(invocation, name, changed=changed)

    def fail(invocation, kind, name, error, *, blocked=False):
        assert threading.get_ident() == caller
        if kind == "session" and name == "bad" and not blocked:
            failure_receipts.append(str(error))
            raise receiver_error
        original_fail(invocation, kind, name, error, blocked=blocked)

    def close(invocation):
        final_results.update(invocation.results)
        completed_names.update(invocation.completed)
        original_close(invocation)

    monkeypatch.setattr(stop, "_completed", completed)
    monkeypatch.setattr(RuntimeInvocation, "fail", fail)
    monkeypatch.setattr(RuntimeInvocation, "close", close)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    with pytest.raises(type(primary)) as caught:
        manager.stop_all_sessions(db, make_config(), interaction=REFUSE)

    assert caught.value is primary
    assert drain_errors[0] is primary
    assert receiver_error in drain_errors
    assert failure_receipts == ["native teardown failed" if failure_stage == "native" else "stopped persistence failed"]
    assert final_results["session", "good"].outcome is Outcome.CHANGED
    assert completed_names == {("session", "good")}
    assert final_results["session", "bad"].outcome is not Outcome.CHANGED
    assert final_results["session", "queued"].outcome is not Outcome.CHANGED
    if prior_cancellation:
        assert final_results["session", "queued"].outcome is Outcome.BLOCKED
        assert rendered
    else:
        assert not rendered
    assert queued.cancelled() and native == Counter(bad=1, good=1)
    assert writes == Counter(good=1, **({} if failure_stage == "native" else {"bad": 1}))
    assert db.get_session("bad").pid == before["bad"].pid
    assert db.get_session("queued").pid == before["queued"].pid
    assert db.get_session("good").pid == PID_STOPPED
    assert all(row.desired_runtime_state is DesiredRuntimeState.STOPPED for row in db.list_sessions())
