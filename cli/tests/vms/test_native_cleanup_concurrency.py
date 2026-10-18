"""Aggregate cleanup serializes exact staged-helper custody across retries."""

from __future__ import annotations

from threading import Event, Thread

import pytest

from agentworks.errors import StateError
from agentworks.execution._prepared_helper_obligation import PREPARED_HELPER_OBLIGATION_KIND
from agentworks.execution.carrier import Deadline
from agentworks.vms._native_operation import VMOperationControlFact
from tests.execution.test_independent_execution_reads import observer as observer
from tests.execution.test_independent_execution_start import available as available
from tests.execution.test_independent_staged_start import bulk_start
from tests.execution.test_independent_staged_start import staged as staged

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("completion", [0, None])
def test_competing_retry_refuses_before_paused_cleanup_can_lose_its_call(staged, monkeypatch, completion):
    database, _, operation, access, carrier, workflow, _ = staged
    bulk_start(access)
    (cleanup,) = operation._prepared_stages
    before = len(carrier.requests)
    carrier.code = completion
    fact = VMOperationControlFact(workflow)
    entered, resume = Event(), Event()
    results: dict[str, BaseException | None] = {}
    stops: list[str] = []
    original_stop = workflow.owner.stop_admission
    first_budget = Deadline.after(30)

    def stop():
        original_stop()
        stops.append("stop")

    def pause(deadline):
        if entered.is_set():
            return
        assert deadline is first_budget
        held_call = cleanup._active
        assert held_call is not None and not held_call.entered
        entered.set()
        assert resume.wait(10)
        assert cleanup._active is held_call and not held_call.entered

    def retry(name, budget):
        try:
            fact.retry_cleanup(budget)
        except BaseException as control:
            results[name] = control
        else:
            results[name] = None

    monkeypatch.setattr(workflow.owner, "stop_admission", stop)
    cleanup._stage._before_dispatch = pause
    first = Thread(target=retry, args=("first", first_budget), name="first-cleanup")
    second = Thread(target=retry, args=("second", Deadline.after(30)), name="competing-cleanup")
    first.start()
    try:
        assert entered.wait(10)
        held = cleanup._active
        assert held is not None and not held.entered
        second.start()
        second.join(5)
        assert not second.is_alive()
        assert isinstance(results["second"], StateError)
        assert len(stops) == 2 and workflow.owner._close_requested.is_set()
        assert cleanup._active is held and not held.entered
        assert len(carrier.requests) == before and not workflow.owner._released
        claim = database.operations.inspect(workflow.owner.ownership.scope)
        assert claim is not None and claim.ownership == workflow.owner.ownership
        with pytest.raises(StateError):
            workflow.owner.borrow()
    finally:
        resume.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert len(carrier.requests) == before + 1
    if completion == 0:
        assert results["first"] is None
        assert cleanup._stage.cleaned and operation._prepared_stages == []
        assert workflow.owner._released
        assert database.operations.inspect(workflow.owner.ownership.scope) is None
    else:
        assert isinstance(results["first"], StateError)
        assert cleanup._active is held and held.entered
        assert not cleanup._stage.cleaned and not workflow.owner._released
        assert operation._prepared_stages == [cleanup]
        assert [row.obligation_kind for row in workflow.owner.list_pending_lifecycle_obligations()] == [
            PREPARED_HELPER_OBLIGATION_KIND
        ]
        with pytest.raises(StateError):
            fact.retry_cleanup(Deadline.after(5))
        assert len(carrier.requests) == before + 1 and cleanup._active is held


def test_reentrant_retry_refuses_and_outer_cleanup_keeps_its_exact_call(staged, monkeypatch):
    _, _, operation, access, carrier, workflow, _ = staged
    bulk_start(access)
    (cleanup,) = operation._prepared_stages
    before = len(carrier.requests)
    fact = VMOperationControlFact(workflow)
    stops = []
    original_stop = workflow.owner.stop_admission

    def stop():
        original_stop()
        stops.append("stop")

    def reenter(deadline):
        held = cleanup._active
        assert held is not None and not held.entered
        with pytest.raises(StateError):
            fact.retry_cleanup(Deadline.after(5))
        assert cleanup._active is held and not held.entered
        assert len(carrier.requests) == before and not workflow.owner._released
        assert workflow.owner._close_requested.is_set() and len(stops) == 2

    monkeypatch.setattr(workflow.owner, "stop_admission", stop)
    cleanup._stage._before_dispatch = reenter
    fact.retry_cleanup(Deadline.after(5))
    assert len(carrier.requests) == before + 1
    assert cleanup._stage.cleaned and workflow.owner._released


def test_route_control_releases_aggregate_guard_for_exact_later_retry(staged):
    _, _, operation, access, carrier, workflow, _ = staged
    bulk_start(access)
    (cleanup,) = operation._prepared_stages
    before = len(carrier.requests)
    fact = VMOperationControlFact(workflow)
    control = KeyboardInterrupt("route")

    def interrupt(deadline):
        raise control

    cleanup._stage._before_dispatch = interrupt
    with pytest.raises(KeyboardInterrupt) as caught:
        fact.retry_cleanup(Deadline.after(5))
    assert caught.value is control and len(carrier.requests) == before
    assert cleanup._active is not None and not cleanup._active.entered
    assert not workflow.owner._released and workflow.owner._close_requested.is_set()
    cleanup._stage._before_dispatch = None
    fact.retry_cleanup(Deadline.after(5))
    assert len(carrier.requests) == before + 1
    assert cleanup._stage.cleaned and workflow.owner._released
