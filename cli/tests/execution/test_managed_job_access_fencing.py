"""Explicit job control fences current-generation admission and delivery."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution import _execution_operation as operation_module
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._managed_disposal_access import dispose_bound_managed_run
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import FACT_ORDER, ControllerState, ManagedResultControl
from agentworks.execution._managed_operation_run import ManagedOperationRun
from agentworks.execution.carrier import CarrierReport, Deadline, Dispatch, Retention
from agentworks.execution.jobs import JobStream
from agentworks.execution.models import Command, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure
from agentworks.operations import OperationBorrow, OperationOwner

from . import test_managed_observation as observation_tests
from .test_execution_operation import RecordingCarrier
from .test_managed_disposal import _disposed
from .test_managed_disposal_access import _not_ready
from .test_managed_execution_access import bound as bound
from .test_managed_execution_access import fact
from .test_managed_execution_access import view as view
from .test_managed_lease_exchange import PLAN, RUNTIME

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("refusal", ["not-ready", "not-sent", "pre-dispatch"])
def test_disposal_fresh_identity_publication_survives_interruption(view, monkeypatch, refusal):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    old_id = run.disposal_obligation_id
    assign = ManagedOperationRun.__setattr__
    published: list[str] = []

    def publish(self, name, value):
        assign(self, name, value)
        if self is run and name == "disposal_obligation_id" and value != old_id and not published:
            published.append(value)
            raise KeyboardInterrupt("fresh disposal identity publication interrupted")

    main.dispose.response = _not_ready
    main.dispose.dispatch = Dispatch.NOT_SENT if refusal == "not-sent" else Dispatch.SENT
    main.dispose.refuse_validation = refusal == "pre-dispatch"
    with monkeypatch.context() as patch:
        patch.setattr(ManagedOperationRun, "__setattr__", publish)
        with pytest.raises(KeyboardInterrupt):
            access.dispose(reference)
    assert run.disposal_attempted and run.disposal_obligation_id == published[0]
    assert run.terminal_proved(run.terminal_observation)
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    old_row = next(row for row in rows if row.obligation_id == old_id)
    assert old_row.state is LifecycleObligationState.RESOLVED
    assert not any(row.obligation_id == published[0] for row in rows)
    before = main.dispose.calls

    # The intermediate state can deliver on its new, unregistered identity
    # using retained positive proof; it never rearms the settled old helper.
    main.dispose.refuse_validation = False
    main.dispose.dispatch = Dispatch.SENT
    assert access.dispose(reference).disposed is False
    assert main.dispose.calls == before + 1 and main.observe.calls == 1
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    assert any(row.obligation_id == published[0] for row in rows)
    main.dispose.response = _disposed
    assert access.dispose(reference).disposed is True
    assert access.dispose(reference).disposed is True
    assert main.dispose.calls == before + 2
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    disposal_rows = [row for row in rows if row.obligation_kind == "managed-dispose"]
    assert len(disposal_rows) == 3
    assert all(row.state is LifecycleObligationState.RESOLVED for row in disposal_rows)
    assert next(row for row in disposal_rows if row.obligation_id == old_id) == old_row
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert keeper.stop.calls == keeper.observe.calls == 0
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


@pytest.mark.parametrize("boundary", ["fresh-id-before-publication", "refusal-helper-return", "disposed-helper-return"])
def test_disposal_settled_row_recovers_without_deleted_launch_observation(view, monkeypatch, boundary):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    old_id = run.disposal_obligation_id
    main.dispose.response = _disposed if boundary == "disposed-helper-return" else _not_ready
    with monkeypatch.context() as patch:
        if boundary == "fresh-id-before-publication":
            assign = ManagedOperationRun.__setattr__

            def before_publish(self, name, value):
                if self is run and name == "disposal_obligation_id":
                    raise KeyboardInterrupt("before fresh disposal identity publication")
                assign(self, name, value)

            patch.setattr(ManagedOperationRun, "__setattr__", before_publish)
        else:
            original = dispose_bound_managed_run

            def lose_helper_reply(*args, **kwargs):
                outcome = original(*args, **kwargs)
                assert not outcome.requires_owner_retention
                raise KeyboardInterrupt("settled disposal helper reply lost")

            patch.setattr(operation_module, "dispose_bound_managed_run", lose_helper_reply)
        with pytest.raises(KeyboardInterrupt):
            access.dispose(reference)
    assert run.disposal_attempted and run.disposal_obligation_id == old_id and not run.disposal_confirmed
    assert run.terminal_proved(run.terminal_observation)
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    old_row = next(row for row in rows if row.obligation_id == old_id)
    assert old_row.state is LifecycleObligationState.RESOLVED
    # A successful disposal can already have erased launch and output facts.
    # All these retries must use retained positive proof, not observe again.
    main.facts = ()
    main.dispose.response = _disposed
    assert access.dispose(reference).disposed is True
    assert access.dispose(reference).disposed is True
    assert main.dispose.calls == 2 and main.observe.calls == 1
    assert run.disposal_obligation_id != old_id
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    disposal_rows = [row for row in rows if row.obligation_kind == "managed-dispose"]
    assert len(disposal_rows) == 2 and all(row.state is LifecycleObligationState.RESOLVED for row in disposal_rows)
    assert next(row for row in disposal_rows if row.obligation_id == old_id) == old_row
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert keeper.stop.calls == keeper.observe.calls == 0
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


@pytest.mark.parametrize("field", ["obligation_kind", "payload_version", "payload", "payload_revision"])
def test_disposal_retry_refuses_mismatched_resolved_row(view, monkeypatch, field):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    original_dispose = dispose_bound_managed_run

    def lose_helper_reply(*args, **kwargs):
        original_dispose(*args, **kwargs)
        raise KeyboardInterrupt("settled disposal reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(operation_module, "dispose_bound_managed_run", lose_helper_reply)
        with pytest.raises(KeyboardInterrupt):
            access.dispose(reference)
    old_id = run.disposal_obligation_id
    read_rows = workflow.owner.list_lifecycle_obligations
    wrong = {"obligation_kind": "managed-stop", "payload_version": 2, "payload": b"wrong-run", "payload_revision": 1}

    def mismatched_rows():
        return tuple(
            replace(row, **{field: wrong[field]}) if row.obligation_id == old_id else row for row in read_rows()
        )

    with monkeypatch.context() as patch:
        patch.setattr(workflow.owner, "list_lifecycle_obligations", mismatched_rows)
        with pytest.raises(StateError):
            access.dispose(reference)
    assert main.dispose.calls == 1 and run.disposal_obligation_id == old_id
    assert access.dispose(reference).disposed is True
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("attempt_started", [False, True])
def test_stop_refuses_other_owner_borrow_before_keeper_drain(view, attempt_started):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    borrow = workflow.owner.borrow()
    attempt = borrow.begin_attempt() if attempt_started else None
    with pytest.raises(StateError):
        access.stop(reference)
    assert not run.keeper._stop.is_set()
    assert main.stop.calls == main.observe.calls == keeper.stop.calls == 0
    if attempt is not None:
        attempt.settle()
    borrow.close()
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_file_call_attempt_excludes_explicit_stop(view):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    operation = FileOperation(workflow.owner, run.receipt.spec.target)
    refused = []

    def interleave():
        with pytest.raises(StateError):
            access.stop(reference)
        refused.append(True)
        assert not run.keeper._stop.is_set()

    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT), before_dispatch=interleave)
    operation.stat(
        carrier,
        trusted_root_path="/approved",
        relative_path="file",
        plan=PLAN,
        deadline=Deadline.after(5),
        runtime_selection=RUNTIME,
    )
    assert refused == [True] and main.stop.calls == main.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_confirmed_disposal_publication_survives_interruption(view, monkeypatch):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    assign = ManagedOperationRun.__setattr__
    interrupted: list[bool] = []

    def publish(self, name, value):
        assign(self, name, value)
        if self is run and name == "disposal_confirmed" and value and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("confirmed disposal publication interrupted")

    monkeypatch.setattr(ManagedOperationRun, "__setattr__", publish)
    with pytest.raises(KeyboardInterrupt):
        access.dispose(reference)
    assert run.disposal_confirmed and not run.cleanup_complete
    main.facts = keeper.facts = ()
    assert access.dispose(reference).disposed
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert keeper.stop.calls == keeper.observe.calls == 0 and main.dispose.calls == 1
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


def test_unknown_explicit_stop_helper_retains_ordinary_custody(view):
    database, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.stop.code = None
    result = access.stop(reference)
    assert result.accepted is None and not result.terminated
    assert main.stop.calls == 1 and main.observe.calls == 0
    for method in (access.stop, access.observe):
        with pytest.raises(StateError):
            method(reference)
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


@pytest.mark.parametrize(
    "transition", ["install_dispatch_obligation", "arm_dispatch_obligation", "handoff_retained_effect"]
)
def test_stop_interrupted_bookkeeping_uses_retained_lifetime_custody(view, monkeypatch, transition):
    database, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    original = getattr(OperationBorrow, transition)
    interrupted: list[bool] = []

    def lose_reply(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("stop bookkeeping reply lost")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(OperationBorrow, transition, lose_reply)
        with pytest.raises(KeyboardInterrupt):
            access.stop(reference)
    assert main.stop.calls == (1 if transition == "handoff_retained_effect" else 0)
    assert run.keeper._stop.is_set() is (transition == "handoff_retained_effect")
    workflow.views.execution_operation.retry_inline_bookkeeping()
    access.observe(reference)
    rows = database.operations.list_lifecycle_obligations(workflow.owner.ownership)
    assert len([row for row in rows if row.obligation_kind == "carrier-dispatch"]) == 1
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_explicit_stop_does_not_clear_unknown_closing_helper(view):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    assert run.keeper.drain(Deadline.after(2)).drained
    keeper.stop.code = None
    with pytest.raises(StateError):
        run.keeper.request_stop(Deadline.after(2))
    assert run.keeper._closing_pending
    with pytest.raises(StateError):
        access.stop(reference)
    assert run.keeper._closing_pending and main.stop.calls == main.observe.calls == 0
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


@pytest.mark.parametrize("takeover", ["admission", "stop", "observation"])
def test_stop_takeover_refuses_old_generation_delivery(view, monkeypatch, takeover):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    recovered = [False]

    def recover() -> None:
        if not recovered[0]:
            OperationOwner.recover(database.operations, workflow.owner.ownership, "f" * 32)
            recovered[0] = True

    if takeover == "admission":
        recover()
    elif takeover == "stop":
        carrier = main.stop
        original = carrier.validate

        def validate(*args, **kwargs):
            original(*args, **kwargs)
            recover()

        monkeypatch.setattr(carrier, "validate", validate)
    else:
        original_observe = main.observe.validate

        def observe(*args, **kwargs):
            result = original_observe(*args, **kwargs)
            recover()
            return result

        monkeypatch.setattr(main.observe, "validate", observe)
    if takeover == "observation":
        result = access.stop(reference)
        assert result.accepted is True and not result.terminated
        assert result.failure is ExecutionFailure.OBSERVATION
        assert main.stop.calls == 1 and main.observe.calls == 0
    else:
        with pytest.raises(StateError):
            access.stop(reference)
        assert main.stop.calls == main.observe.calls == 0
        assert run.keeper._stop.is_set() is (takeover != "admission")
    assert not run.cleanup_complete
    assert keeper.stop.calls == keeper.observe.calls == 0


@pytest.mark.parametrize("sensitive", [False, True])
def test_policy_closed_empty_output_is_eof_without_capture(view, sensitive):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        lifetime=Lifetime.OPERATION,
        output=Output.discard(),
        sensitive=sensitive,
    )

    def response(request):
        end = decode_fact(fact(FactName.STDOUT_END, request.expected_launch))
        if sensitive:
            end["disposition"] = "sensitivity-suppressed"
        return observation_tests._records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (request.expected_launch, encode_fact(end)),
        )

    main.observe.response = response
    result = access.read_output(reference, stream=JobStream.STDOUT)
    assert result.eof and not result.capture_complete and result.data == b""
    assert result.retention is (Retention.SUPPRESSED if sensitive else Retention.DISCARDED)
    assert result.failure is None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("wait_fact", ["present", "absent", "late"])
def test_operation_wait_polls_until_positive_controller_closure(view, monkeypatch, wait_fact):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    snapshots = [0]

    def response(request):
        if request.stream is not None:
            name = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
            return observation_tests._records(
                request.nonce,
                ManagedResultControl((FactName.LAUNCH, name)),
                (request.expected_launch, fact(name, request.expected_launch)),
            )
        snapshots[0] += 1
        main.controller = ControllerState.RUNNING if snapshots[0] == 1 else ControllerState.EXITED
        main.facts = (
            FACT_ORDER
            if wait_fact == "present" or (wait_fact == "late" and snapshots[0] > 1)
            else (tuple(name for name in FACT_ORDER if name is not FactName.WAIT))
        )
        return main.observe_response(request)

    main.observe.response = response
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = access.wait(reference)
    assert snapshots[0] == 2 and result.owned_cleanup_confirmed
    assert result.application_state is (
        ApplicationState.UNKNOWN if wait_fact == "absent" else ApplicationState.COMPLETED
    )
    assert (result.status is None) is (wait_fact == "absent")
    assert keeper.stop.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))
