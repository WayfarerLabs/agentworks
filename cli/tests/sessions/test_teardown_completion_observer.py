"""Completed stop notifications survive cancellation without guessing success."""

from __future__ import annotations

import threading
from collections import Counter

import pytest

from agentworks.errors import UserAbort
from agentworks.sessions.manager import _teardown as teardown
from tests.sessions.test_session_batch_stop_parallelism import _plan, _RecordingDB


@pytest.mark.parametrize("signal", [KeyboardInterrupt, UserAbort])
@pytest.mark.parametrize("interruptions", [1, 2])
def test_post_commit_observer_retries_before_primary_cancellation_escapes(monkeypatch, signal, interruptions):
    plans = [_plan("session-1"), _plan("session-2")]
    ready = threading.Barrier(2)
    main_thread = threading.get_ident()

    def execute(plan):
        ready.wait(timeout=2)

    monkeypatch.setattr(teardown, "execute_dedicated_teardown", execute)
    db = _RecordingDB()
    errors = [signal(f"cancelled after commit {index}") for index in range(interruptions)]
    attempts: Counter[str] = Counter()
    notified = set()

    def completed(plan):
        assert threading.get_ident() == main_thread
        assert any(name == plan.session_name for _, name in db.calls)
        attempts[plan.session_name] += 1
        index = sum(attempts.values()) - 1
        if index < len(errors):
            raise errors[index]
        notified.add(plan.session_name)

    with pytest.raises(signal) as caught:
        teardown.execute_concurrent_dedicated_teardowns(plans, db=db, on_stopped=completed)

    assert caught.value is errors[0]
    assert notified == {plan.session_name for plan in plans}
    assert sum(attempts.values()) == len(plans) + interruptions
    assert {thread for thread, _ in db.calls} == {main_thread}


def test_observer_failure_does_not_repeat_native_mutation_or_erase_sibling_success(monkeypatch):
    plans = [_plan("session-1"), _plan("session-2")]
    executed = []
    monkeypatch.setattr(teardown, "execute_dedicated_teardown", lambda plan: executed.append(plan.session_name))
    db = _RecordingDB()
    notified = []

    def completed(plan):
        if plan.session_name == "session-1":
            raise RuntimeError("completion recording failed")
        notified.append(plan.session_name)

    failures = teardown.execute_concurrent_dedicated_teardowns(plans, db=db, on_stopped=completed)

    assert [name for name, _ in failures] == ["session-1"]
    assert set(executed) == {"session-1", "session-2"}
    assert len(executed) == 2
    assert notified == ["session-2"]
    assert {name for _, name in db.calls} == {"session-1", "session-2"}
