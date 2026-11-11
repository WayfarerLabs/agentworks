"""Caller interruption cannot discard a completed worker's stopped evidence."""

from __future__ import annotations

import signal
import sys
import threading
from collections import Counter
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor as PythonThreadPoolExecutor

import pytest

from agentworks.errors import UserAbort
from agentworks.sessions.manager import _teardown as teardown
from tests.sessions.test_session_batch_stop_parallelism import _plan, _RecordingDB


def test_real_sigint_during_successful_outcome_retrieval_still_reconciles(monkeypatch):
    plan = _plan("session-1")
    futures = []

    class Pool(PythonThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            futures.append(future)
            return future

    monkeypatch.setattr(teardown, "ThreadPoolExecutor", Pool)
    executed = []
    monkeypatch.setattr(teardown, "execute_dedicated_teardown", lambda plan: executed.append(plan.session_name))
    original_exception = Future.exception
    outcome_code = {Future.result.__code__, Future.exception.__code__}
    injected = False

    def trace(frame, event, arg):
        nonlocal injected
        future = frame.f_locals.get("self")
        if not injected and event == "call" and frame.f_code in outcome_code and future in futures:
            assert future.done() and original_exception(future) is None
            injected = True
            signal.raise_signal(signal.SIGINT)
        return trace

    db = _RecordingDB()
    completed = []
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(KeyboardInterrupt):
            teardown.execute_concurrent_dedicated_teardowns(
                [plan], db=db, on_outcome=lambda stopped, reason: completed.append(stopped.session_name)
            )
    finally:
        sys.settrace(previous_trace)

    assert injected
    assert executed == [plan.session_name]
    assert [name for _, name in db.calls] == [plan.session_name]
    assert completed == [plan.session_name]


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, UserAbort])
@pytest.mark.parametrize("interruptions", [1, 2])
def test_caller_outcome_interrupt_retries_without_repeating_native_work(monkeypatch, signal_type, interruptions):
    plans = [_plan("session-1"), _plan("session-2")]
    ready = threading.Barrier(2)
    executed = []

    def execute(plan):
        executed.append(plan.session_name)
        ready.wait(timeout=2)

    monkeypatch.setattr(teardown, "execute_dedicated_teardown", execute)
    original_result = Future.result
    original_exception = Future.exception
    errors = [signal_type(f"caller interruption {index}") for index in range(interruptions)]
    injected = 0

    def interrupt(future) -> None:
        nonlocal injected
        if future.done() and original_exception(future) is None and injected < len(errors):
            error = errors[injected]
            injected += 1
            raise error

    def result(future, *args, **kwargs):
        interrupt(future)
        return original_result(future, *args, **kwargs)

    def exception(future, *args, **kwargs):
        interrupt(future)
        return original_exception(future, *args, **kwargs)

    monkeypatch.setattr(Future, "result", result)
    monkeypatch.setattr(Future, "exception", exception)
    db = _RecordingDB()
    completed = []
    with pytest.raises(signal_type) as caught:
        teardown.execute_concurrent_dedicated_teardowns(
            plans, db=db, on_outcome=lambda plan, reason: completed.append(plan.session_name)
        )
    assert caught.value is errors[0]
    assert injected == interruptions
    assert Counter(executed) == Counter(plan.session_name for plan in plans)
    assert set(completed) == {plan.session_name for plan in plans}
    assert {name for _, name in db.calls} == set(completed)


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit, UserAbort])
def test_genuine_worker_abort_is_consumed_once_and_preserves_successful_sibling(monkeypatch, signal_type):
    plans = [_plan("bad-1"), _plan("good-2")]
    ready = threading.Barrier(2)
    error = signal_type("worker aborted")
    executed = []

    def execute(plan):
        executed.append(plan.session_name)
        ready.wait(timeout=2)
        if plan.session_name == "bad-1":
            raise error

    monkeypatch.setattr(teardown, "execute_dedicated_teardown", execute)
    db = _RecordingDB()
    completed = []
    with pytest.raises(signal_type) as caught:
        teardown.execute_concurrent_dedicated_teardowns(
            plans, db=db, on_outcome=lambda plan, reason: completed.append(plan.session_name)
        )
    assert caught.value is error
    assert Counter(executed) == Counter(plan.session_name for plan in plans)
    assert completed == ["good-2"]
    assert [name for _, name in db.calls] == ["good-2"]
