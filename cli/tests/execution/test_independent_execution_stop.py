"""RESOURCE stop uses exact namespace and ordinary custody without job adoption."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as execution_module
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_observation_protocol import ControllerState
from agentworks.execution._managed_runs import (
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
)
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.models import Command, JobRef, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExecutionFailure
from agentworks.operations import OperationOwner

from . import test_independent_execution_reads as reads
from .test_managed_job_access import RUN

observer = reads.observer


@pytest.mark.windows
def test_resource_stop_and_fresh_observation_reuse_shared_row_then_finish_leaves_job(observer):
    database, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    reference = JobRef(RUN.run_id)
    for _ in range(140):
        assert access.observe(reference).workload_cleanup_confirmed
        stopped = access.stop(reference)
        assert stopped.reference == reference and stopped.accepted and stopped.terminated and stopped.failure is None
    (row,) = workflow.owner.list_pending_lifecycle_obligations()
    assert row.obligation_kind == "carrier-dispatch" and row.payload == b""
    assert operation.managed_runs == () and repository.inspect(RUN) == before
    assert main.stop.calls == 140 and main.observe.calls == 280
    assert main.clock.calls == main.start.calls == main.dispose.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert main.stop.calls == 140 and repository.inspect(RUN) == before
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


@pytest.mark.parametrize("controller", (ControllerState.RUNNING, ControllerState.UNKNOWN))
def test_accepted_resource_stop_does_not_prove_controller_closure(observer, controller):
    _, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    main.controller = controller
    outcome = access.stop(JobRef(RUN.run_id))
    assert outcome.accepted and not outcome.terminated and outcome.failure is ExecutionFailure.OBSERVATION
    assert main.stop.calls == main.observe.calls == 1 and operation.managed_runs == ()
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert main.stop.calls == main.observe.calls == 1 and repository.inspect(RUN) == before


@pytest.mark.parametrize(
    "missing", (FactName.LAUNCH, FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY)
)
def test_stop_requires_fresh_complete_resource_proof(observer, missing):
    _, _, _, access, main, _ = observer
    main.facts = tuple(name for name in main.facts if name is not missing)
    outcome = access.stop(JobRef(RUN.run_id))
    assert outcome.accepted and not outcome.terminated and main.observe.calls == 1


def test_missing_application_wait_still_permits_positive_resource_stop_closure(observer):
    _, _, operation, access, main, _ = observer
    main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    outcome = access.stop(JobRef(RUN.run_id))
    assert outcome.accepted and outcome.terminated and outcome.failure is None
    assert operation.managed_runs == ()


def test_accepted_stop_survives_unknown_followup_helper_without_claiming_closure(observer):
    _, _, operation, access, main, _ = observer
    main.observe.dispatch, main.observe.code = Dispatch.UNKNOWN, None
    result = access.stop(JobRef(RUN.run_id))
    assert result.accepted and not result.terminated and result.failure is ExecutionFailure.OBSERVATION
    assert operation.unfinished_inline_executions and operation.managed_runs == ()
    with pytest.raises(StateError):
        access.stop(JobRef(RUN.run_id))
    with pytest.raises(StateError):
        operation.finish()
    assert main.stop.calls == main.observe.calls == 1


@pytest.mark.parametrize("dispatch", (Dispatch.NOT_SENT, Dispatch.UNKNOWN))
def test_unaccepted_stop_is_truthful_and_unknown_custody_blocks_new_work(observer, dispatch):
    _, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    main.stop.dispatch, main.stop.code = dispatch, None
    outcome = access.stop(JobRef(RUN.run_id))
    assert outcome.accepted is (False if dispatch is Dispatch.NOT_SENT else None)
    assert not outcome.terminated and outcome.failure is ExecutionFailure.OBSERVATION
    assert main.stop.calls == 1 and main.observe.calls == 0 and operation.managed_runs == ()
    if dispatch is Dispatch.UNKNOWN:
        assert operation.unfinished_inline_executions
        for action in (
            lambda: access.stop(JobRef(RUN.run_id)),
            lambda: access.observe(JobRef(RUN.run_id)),
            operation.finish,
        ):
            with pytest.raises(StateError):
                action()
        operation.retry_inline_bookkeeping()
        assert operation.unfinished_inline_executions
        assert main.stop.calls == 1 and repository.inspect(RUN) == before
    else:
        assert not operation.unfinished_inline_executions
        operation.retry_inline_bookkeeping()
        assert main.stop.calls == 1 and workflow.owner._active_borrow is None


@pytest.mark.parametrize("state", ("reserved", "possible"))
def test_unconfirmed_resource_receipt_refuses_stop_before_borrow(observer, state):
    _, repository, _, access, main, workflow = observer
    original = repository.inspect(RUN)
    record = repository.reserve(
        original.spec, identity=ManagedRunIdentity("f" * 32), output_policy=original.output_policy
    )
    if state == "possible":
        record = repository.mark_possible_dispatch(record)
    with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(ValidationError):
        access.stop(JobRef(record.identity.run_id))
    borrow.assert_not_called()
    assert main.stop.calls == main.observe.calls == 0


@pytest.mark.parametrize("namespace", (None, ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "foreign-resource")))
def test_missing_or_foreign_namespace_refuses_resource_stop_before_effects(observer, namespace):
    _, repository, operation, access, main, workflow = observer
    operation._resource_owner = namespace
    with (
        patch.object(repository, "inspect", wraps=repository.inspect) as inspect,
        patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow,
        pytest.raises(ValidationError),
    ):
        access.stop(JobRef(RUN.run_id))
    if namespace is None:
        inspect.assert_not_called()
    borrow.assert_not_called()
    assert main.stop.calls == main.observe.calls == 0


@pytest.mark.windows
@pytest.mark.parametrize("closing", (False, True))
def test_stale_or_closing_owner_refuses_resource_lookup_and_stop(observer, closing):
    database, repository, _, access, main, workflow = observer
    successor = None
    debt = None
    if closing:
        debt = workflow.owner.register_lifecycle_obligation("unused-test-custody", payload_version=1, payload=b"")
        with pytest.raises(StateError):
            workflow.owner.close()
    else:
        successor = OperationOwner.recover(database.operations, workflow.owner.ownership, "b" * 32)
    try:
        with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(StateError):
            access.stop(JobRef(RUN.run_id))
        inspect.assert_not_called()
        assert main.stop.calls == main.observe.calls == 0
    finally:
        if debt is not None:
            debt.resolve()
        if successor is not None:
            successor.record_effects_resolved()
            successor.close()


@pytest.mark.windows
@pytest.mark.parametrize("outstanding", (False, True))
def test_active_file_borrow_refuses_stop_before_dispatch(observer, outstanding):
    _, _, operation, access, main, workflow = observer
    files = workflow.views.file_operation
    borrow = files._owner.borrow()
    attempt = borrow.begin_attempt() if outstanding else None
    try:
        with pytest.raises(StateError):
            access.stop(JobRef(RUN.run_id))
        assert main.stop.calls == main.observe.calls == 0 and operation.managed_runs == ()
        assert files._owner._active_borrow is borrow and operation._dispatch_id is None
    finally:
        if attempt is not None:
            attempt.settle()
        borrow.close()


def test_selected_route_refusal_prevents_resource_stop_delivery(observer):
    _, _, operation, access, main, _ = observer
    route = Mock(spec=WSL2OwnedOperation)
    refusal = StateError("selected route changed")
    route.require_selected_route.side_effect = refusal
    operation._wsl2_route = route
    with pytest.raises(StateError) as raised:
        access.stop(JobRef(RUN.run_id))
    assert raised.value is refusal and main.stop.calls == main.observe.calls == 0


def test_planned_op_id_never_falls_back_to_resource_stop(observer, monkeypatch):
    _, repository, operation, access, main, _ = observer
    identifiers = iter((UUID(RUN.run_id), UUID("b" * 32), UUID("c" * 32)))
    monkeypatch.setattr(execution_module, "uuid4", lambda: next(identifiers))
    with pytest.raises(StateError):
        access.start(
            Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
        )
    assert operation.managed_runs
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        access.stop(JobRef(RUN.run_id))
    inspect.assert_not_called()
    assert main.stop.calls == main.observe.calls == 0


@pytest.mark.parametrize("control", (KeyboardInterrupt("stop control"), SystemExit("stop control")))
def test_stop_control_keeps_identity_cause_reference_and_unknown_custody(observer, control):
    _, _, operation, access, main, _ = observer
    cause = OSError("prior cause")
    control.__cause__ = cause

    def interrupted(request):
        raise control

    main.stop.response = interrupted
    with pytest.raises(type(control)) as raised:
        access.stop(JobRef(RUN.run_id))
    assert raised.value is control and control.__cause__.reference == JobRef(RUN.run_id)
    assert control.__cause__.__cause__.__cause__ is cause
    assert operation.unfinished_inline_executions and main.stop.calls == 1 and main.observe.calls == 0


@pytest.mark.windows
def test_lost_stop_bookkeeping_reply_retries_only_local_custody(observer, monkeypatch):
    _, _, operation, access, main, workflow = observer
    capture = operation._capture
    control = KeyboardInterrupt("stop bookkeeping")

    def interrupted(active, outcome):
        raise control

    monkeypatch.setattr(operation, "_capture", interrupted)
    with pytest.raises(KeyboardInterrupt) as raised:
        access.stop(JobRef(RUN.run_id))
    assert raised.value is control and operation.active_inline_calls and workflow.owner._active_borrow is not None
    monkeypatch.setattr(operation, "_capture", capture)
    operation.retry_inline_bookkeeping()
    assert main.stop.calls == 1 and main.observe.calls == 0 and workflow.owner._active_borrow is None
    assert not operation.active_inline_calls and operation.managed_runs == ()


@pytest.mark.parametrize("mismatch", ("vm", "incarnation", "boot", "prior-op"))
def test_foreign_resource_record_refuses_stop_before_borrow(observer, mismatch):
    _, repository, _, access, main, workflow = observer
    original = repository.inspect(RUN)
    spec = original.spec
    if mismatch == "prior-op":
        spec = replace(
            spec, owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32), lifetime=ManagedRunLifetime.OPERATION
        )
    else:
        changes = {
            "vm": {"name": "vm-two"},
            "incarnation": {"incarnation": "v1:" + "b" * 64},
            "boot": {"boot_id": "00000000-0000-4000-8000-000000000002"},
        }[mismatch]
        spec = replace(spec, target=replace(spec.target, **changes))
    foreign = repository.reserve(spec, output_policy=original.output_policy)
    with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(ValidationError):
        access.stop(JobRef(foreign.identity.run_id))
    borrow.assert_not_called()
    assert main.stop.calls == main.observe.calls == 0


@pytest.mark.parametrize("binding", ("carrier", "runtime"))
def test_wrong_selected_access_binding_refuses_resource_lookup(observer, binding):
    _, repository, _, access, main, _ = observer
    if binding == "carrier":
        access._carrier = Mock()
    else:
        access._runtime_selection = replace(access._runtime_selection, explicit_path="/bin/python3")
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        access.stop(JobRef(RUN.run_id))
    inspect.assert_not_called()
    assert main.stop.calls == main.observe.calls == 0


def test_budget_exhausted_after_accepted_stop_does_not_observe_or_renew(observer, monkeypatch):
    _, _, operation, access, main, _ = observer
    clock = [0.0]
    execute = main.stop.execute

    def stop(*args, **kwargs):
        result = execute(*args, **kwargs)
        clock[0] = 2.0
        return result

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main.stop, "execute", stop)
    outcome = access.stop(JobRef(RUN.run_id), deadline=Deadline.after(1))
    assert outcome.accepted and not outcome.terminated and outcome.deadline_exceeded
    assert outcome.failure is ExecutionFailure.DEADLINE and main.observe.calls == 0
    assert operation.managed_runs == () and main.clock.calls == 0


def test_stop_and_fresh_observation_use_the_same_selected_deadline(observer):
    _, _, _, access, main, _ = observer
    execute = main.execute
    deadlines = []

    def deliver(invocation, *, io, deadline, custody):
        deadlines.append(deadline)
        return execute(invocation, io=io, deadline=deadline, custody=custody)

    main.execute = deliver
    selected = Deadline.after(2)
    assert access.stop(JobRef(RUN.run_id), deadline=selected).terminated
    assert len(deadlines) == 2 and all(deadline is selected for deadline in deadlines)


@pytest.mark.windows
def test_unused_stop_setup_control_closes_borrow_and_preserves_original_traceback(observer, monkeypatch):
    _, _, operation, access, main, workflow = observer
    control = MemoryError("helper allocation")
    cause = OSError("earlier cause")
    control.__cause__ = cause
    traceback = []

    def allocation(*args):
        try:
            raise control
        except MemoryError:
            traceback.append(control.__traceback__)
            raise

    with monkeypatch.context() as setup:
        setup.setattr(execution_module, "BorrowedFixedHelperCarrier", allocation)
        with pytest.raises(MemoryError) as raised:
            access.stop(JobRef(RUN.run_id))
    assert raised.value is control and control.__cause__.__cause__ is cause
    current = control.__traceback__
    while current is not traceback[0]:
        assert current is not None
        current = current.tb_next
    assert workflow.owner._active_borrow is None and not operation.active_inline_calls
    assert workflow.owner.list_pending_lifecycle_obligations() == () and main.stop.calls == main.observe.calls == 0
    assert access.stop(JobRef(RUN.run_id)).terminated


@pytest.mark.windows
@pytest.mark.parametrize("boundary", ("before-stop", "before-observation"))
def test_takeover_after_admission_fences_actual_helper_delivery(observer, monkeypatch, boundary):
    database, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    admit = operation._admit
    admissions = []

    def takeover(active):
        admit(active)
        admissions.append(active)
        selected = 1 if boundary == "before-stop" else 2
        if len(admissions) == selected:
            database.operations.recover_takeover(workflow.owner.ownership, "b" * 32)

    monkeypatch.setattr(operation, "_admit", takeover)
    if boundary == "before-stop":
        with pytest.raises(StateError):
            access.stop(JobRef(RUN.run_id))
        assert main.stop.calls == 0
    else:
        result = access.stop(JobRef(RUN.run_id))
        assert result.accepted and not result.terminated and result.failure is ExecutionFailure.OBSERVATION
        assert main.stop.calls == 1
    assert main.observe.calls == 0 and repository.inspect(RUN) == before and operation.managed_runs == ()
    assert operation.active_inline_calls and workflow.owner._active_borrow is not None
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert main.stop.calls == (0 if boundary == "before-stop" else 1) and main.observe.calls == 0
