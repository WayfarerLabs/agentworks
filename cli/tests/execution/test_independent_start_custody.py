"""Exact RESOURCE start interruption boundaries use actual SQLite custody."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_resource_start as resource_start
from agentworks.execution import _managed_start_exchange as start_exchange
from agentworks.execution._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import ControllerState
from agentworks.execution._managed_runs import ManagedLaunchState, ManagedRunIdentity
from agentworks.execution._managed_start_operation import start_borrowed_managed_run
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import Deadline
from agentworks.execution.models import Command, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExitCode
from agentworks.operations import OperationOwner

from . import test_independent_execution_reads as reads
from .test_independent_execution_reads import observer as observer
from .test_independent_execution_start import available as available
from .test_independent_execution_start import start
from .test_managed_execution_access import fact as original_fact


@pytest.mark.windows
def test_confirmed_receipt_never_hides_actual_local_delivery_debt(available, monkeypatch):
    _, repository, operation, access, main, workflow = available
    original = main.start.execute
    local = []

    def execute(*args, **kwargs):
        custody = kwargs["custody"]
        custody.begin_process()
        local.append(custody)
        return original(*args, **kwargs)

    monkeypatch.setattr(main.start, "execute", execute)
    with pytest.raises(StateError) as caught:
        start(access)
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    (active,) = operation.active_inline_calls
    assert active.start is not None and active.operation.has_outstanding_attempt
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert workflow.owner._active_borrow is active.borrow
    with pytest.raises(StateError):
        start(access)
    with pytest.raises(StateError):
        operation.finish()
    assert local[0].close(Deadline.after(2))
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert main.start.calls == 1 and operation.active_inline_calls == ()


@pytest.mark.parametrize("committed", [False, True])
def test_possible_dispatch_commit_reply_loss_is_not_automatic_launch(available, monkeypatch, committed):
    _, repository, operation, access, main, _ = available
    original = repository.mark_possible_dispatch
    control = KeyboardInterrupt("possible dispatch")

    def possible(record):
        if committed:
            original(record)
        raise control

    monkeypatch.setattr(repository, "mark_possible_dispatch", possible)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control and isinstance(control.__cause__, ManagedExecutionControlFact)
    record = repository.inspect(ManagedRunIdentity(control.__cause__.reference.run_id))
    assert record.launch_state is (ManagedLaunchState.POSSIBLE_DISPATCH if committed else ManagedLaunchState.RESERVED)
    operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == ()
    assert repository.inspect(record.identity) == record
    assert main.start.calls == 0


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.windows
def test_tracker_publication_failure_keeps_actual_call_if_committed(available, after):
    _, _, operation, access, main, workflow = available
    control = MemoryError("publication")

    class Calls(dict):
        def __setitem__(self, key, value):
            if after:
                super().__setitem__(key, value)
            raise control

    operation._active_inline_calls = Calls()
    with pytest.raises(MemoryError) as caught:
        start(access)
    assert caught.value is control and main.start.calls == 0
    assert workflow.owner._active_borrow is None
    assert len(operation.active_inline_calls) == int(after)
    operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == ()


def test_candidate_publication_control_retains_actual_ack_before_reconcile(available, monkeypatch):
    _, repository, operation, access, main, _ = available
    original = start_borrowed_managed_run
    control = SystemExit("candidate publication reply")

    def start_kernel(*args, **kwargs):
        publish = kwargs["publish_candidate"]

        def interrupt(candidate):
            publish(candidate)
            raise control

        kwargs["publish_candidate"] = interrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(resource_start, "start_borrowed_managed_run", start_kernel)
    with pytest.raises(SystemExit) as caught:
        start(access)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    assert active.candidate is not None and active.start is not None
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    operation.retry_inline_bookkeeping()
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert main.start.calls == 1 and operation.active_inline_calls == ()


def test_finish_during_reservation_refuses_actual_launch_and_bookkeeping_does_not_replay(available, monkeypatch):
    _, repository, operation, access, main, _ = available
    original = repository.reserve

    def reserve(*args, **kwargs):
        record = original(*args, **kwargs)
        with pytest.raises(StateError):
            operation.finish()
        return record

    monkeypatch.setattr(repository, "reserve", reserve)
    with pytest.raises(StateError):
        start(access)
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert operation.active_inline_calls == () and main.start.calls == 0


@pytest.mark.parametrize("outstanding", [False, True])
@pytest.mark.windows
def test_existing_other_component_borrow_refuses_before_reserve(available, monkeypatch, outstanding):
    _, repository, operation, access, main, workflow = available
    borrow = workflow.owner.borrow()
    attempt = borrow.begin_attempt() if outstanding else None
    monkeypatch.setattr(repository, "reserve", lambda *args, **kwargs: pytest.fail("reserve"))
    try:
        with pytest.raises(StateError):
            start(access)
        assert operation.active_inline_calls == () and main.start.calls == 0
        assert workflow.owner._active_borrow is borrow
    finally:
        if attempt is not None:
            attempt.settle()
        borrow.close()


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.windows
def test_exact_action_resolution_loss_retries_without_reservation_or_helper(available, after):
    _, repository, operation, access, main, workflow = available
    ledger = workflow.owner._repository
    original = type(ledger).resolve_lifecycle_obligation
    control = KeyboardInterrupt("resolution")
    injected = False

    def resolve(current, *args, **kwargs):
        nonlocal injected
        if current is ledger and not injected:
            injected = True
            if after:
                original(current, *args, **kwargs)
            raise control
        return original(current, *args, **kwargs)

    with (
        patch.object(type(ledger), "resolve_lifecycle_obligation", resolve),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        start(access)
    assert caught.value is control
    with patch.object(repository, "reserve", side_effect=AssertionError("replayed reserve")):
        operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == () and main.start.calls == 1


@pytest.mark.parametrize("state", ["absent", "conflicting", "unreadable"])
def test_unknown_changed_receipt_never_resolves_original_helper_debt(available, monkeypatch, state):
    _, repository, operation, access, main, _ = available
    original = repository.reconcile
    control = OSError("receipt reply")

    def reconcile(*args, **kwargs):
        original(*args, **kwargs)
        raise control

    monkeypatch.setattr(repository, "reconcile", reconcile)
    with pytest.raises(OSError):
        start(access)
    (active,) = operation.active_inline_calls
    assert active.start is not None
    record = repository.inspect(active.start.receipt.identity)
    if state == "absent":
        monkeypatch.setattr(repository, "inspect", lambda identity: None)
    elif state == "conflicting":
        other = repository.inspect(ManagedRunIdentity("e" * 32))
        monkeypatch.setattr(repository, "inspect", lambda identity: other)
    else:

        def unreadable(identity):
            raise StateError("unreadable")

        monkeypatch.setattr(repository, "inspect", unreadable)
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == (active,) and main.start.calls == 1
    monkeypatch.undo()
    assert repository.inspect(active.start.receipt.identity) == record
    operation.retry_inline_bookkeeping()


@pytest.mark.parametrize("missing_wait", [False, True])
def test_foreground_success_and_unknown_application_share_positive_resource_closure(
    available, monkeypatch, missing_wait
):
    _, _, operation, access, main, _ = available

    def fact(name, launch):
        value = decode_fact(original_fact(name, launch))
        if name is FactName.WAIT:
            value["exit_code"] = 0
        return encode_fact(value)

    monkeypatch.setattr(reads, "fact", fact)
    if missing_wait:
        main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    result = access.run(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        lifetime=Lifetime.INDEPENDENT,
        output=Output.discard(),
        check=not missing_wait,
    )
    assert result.owned_cleanup_confirmed and result.job is not None
    assert result.status is None if missing_wait else result.status == ExitCode(0)
    assert result.application_state is (ApplicationState.UNKNOWN if missing_wait else ApplicationState.COMPLETED)
    assert result.failure is (ExecutionFailure.OBSERVATION if missing_wait else None)
    assert main.start.calls == 1 and operation.managed_runs == ()


def test_true_active_cap_refusal_closes_never_dispatched_start_borrow(available):
    _, _, operation, access, main, workflow = available
    count = 128
    rows = []
    try:
        for _ in range(count):
            rows.append(workflow.owner.register_lifecycle_obligation("test-debt", payload_version=1, payload=b""))
        with pytest.raises(StateError):
            start(access)
        operation.retry_inline_bookkeeping()
        assert workflow.owner._active_borrow is None and operation.active_inline_calls == ()
        assert len(workflow.owner.list_pending_lifecycle_obligations()) == count and main.start.calls == 0
    finally:
        for row in rows:
            row.resolve()


def test_carrier_return_lost_before_candidate_publication_retains_actual_attempt(available):
    _, repository, operation, access, main, workflow = available
    control = MemoryError("candidate")
    with (
        patch.object(start_exchange, "ManagedStartCandidate", side_effect=control),
        pytest.raises(MemoryError) as caught,
    ):
        start(access)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    assert active.candidate is None and active.start is not None
    assert active.operation.outstanding_attempt is not None
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert workflow.owner._active_borrow is active.borrow
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    with pytest.raises(StateError):
        operation.finish()
    assert main.start.calls == 1


def test_resource_launch_reconnects_under_fresh_owner_for_same_controls(available):
    database, repository, original, access, main, workflow = available
    job = start(access)
    workflow.close(cleanup_deadline=Deadline.after(2))
    owner = OperationOwner.acquire(database.operations, workflow.owner.ownership.scope, "reconnect")
    operation = ExecutionOperation(
        owner,
        original._target,
        bootstrap=original._bootstrap,
        managed_repository=repository,
        native_binding=original._native_binding,
        resource_owner=original._resource_owner,
    )
    reconnected = ExecutionAccess(
        operation,
        main,
        runtime_selection=access._runtime_selection,
        ordinary_plan=access._ordinary_plan,
        elevated_plan=access._elevated_plan,
        entity_kind="vm",
        entity_name="vm-one",
        deadline=lambda: Deadline.after(5),
    )
    try:
        assert reconnected.observe(job).workload_cleanup_confirmed
        assert reconnected.wait(job).owned_cleanup_confirmed
        assert reconnected.stop(job).accepted is True
        assert reconnected.dispose(job).disposed is True
        assert operation.managed_runs == () and main.start.calls == 1
        operation.finish()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        if not owner._released:
            operation.finish()
            owner.seal_lifecycle_obligations()
            owner.record_effects_resolved()
            owner.close()


def test_foreground_later_poll_expiry_preserves_job_without_implicit_stop(available, monkeypatch):
    _, repository, operation, access, main, workflow = available
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    access._deadline = lambda: Deadline(101.0)
    main.controller = ControllerState.RUNNING

    def sleep(seconds):
        now[0] = 102.0

    monkeypatch.setattr(time, "sleep", sleep)
    result = access.run(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, output=Output.discard()
    )
    assert result.job is not None and result.failure is ExecutionFailure.DEADLINE
    assert not result.owned_cleanup_confirmed and result.status == ExitCode(7)
    record = repository.inspect(ManagedRunIdentity(result.job.run_id))
    workflow.close(cleanup_deadline=Deadline(103.0))
    assert repository.inspect(record.identity) == record
    assert operation.managed_runs == () and main.start.calls == 1 and main.stop.calls == main.dispose.calls == 0


def test_foreground_truncated_capture_retains_exact_prefix_without_replay(available):
    _, _, operation, access, main, _ = available
    main.disposition, main.content = "truncated-capture", b"abc"
    result = access.run(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, output=Output.capture(3)
    )
    assert result.job is not None and result.failure is ExecutionFailure.OUTPUT_LIMIT
    assert result.stdout.data == result.stderr.data == b"abc"
    assert not result.stdout.complete and not result.stderr.complete
    assert result.owned_cleanup_confirmed and main.start.calls == 1 and operation.managed_runs == ()


@pytest.mark.parametrize("change", ["finish", "binding", "takeover"])
def test_durable_possible_mark_rechecks_live_selected_admission_before_delivery(available, monkeypatch, change):
    database, repository, operation, access, main, workflow = available
    original = repository.mark_possible_dispatch

    def mark(record):
        possible = original(record)
        if change == "finish":
            with pytest.raises(StateError):
                operation.finish()
        elif change == "binding":
            operation._native_binding = replace(operation._native_binding, carrier=reads.ResourceDelivery())
        else:
            database.operations.recover_takeover(workflow.owner.ownership, "b" * 32)
        return possible

    monkeypatch.setattr(repository, "mark_possible_dispatch", mark)
    with pytest.raises((StateError, ValidationError)) as caught:
        start(access)
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    identity = ManagedRunIdentity(caught.value.__cause__.reference.run_id)
    assert repository.inspect(identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert main.start.calls == 0
    if change == "takeover":
        with pytest.raises(StateError):
            operation.retry_inline_bookkeeping()
        assert len(operation.active_inline_calls) == 1
    else:
        operation.retry_inline_bookkeeping()
        assert operation.active_inline_calls == () and workflow.owner._active_borrow is None
