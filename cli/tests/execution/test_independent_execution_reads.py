"""Exact RESOURCE reads share ordinary execution custody without owning the job."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as execution_module
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._managed_bound_run import ManagedDeadlineExpired, preflight_bound_read, preflight_bound_run
from agentworks.execution._managed_job_store import FactName, Stream
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import (
    ControllerObservation,
    ControllerState,
    ManagedOperation,
    ManagedResultControl,
)
from agentworks.execution._managed_result import collect_bound_managed_result
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunRepository,
)
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Deadline, Dispatch, Retention
from agentworks.execution.jobs import JobStream
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure
from agentworks.operations import OperationOwner
from agentworks.vms._native_operation import NativeVMOperation, _Workflow

from .test_managed_execution_access import Delivery, fact
from .test_managed_job_access import RUN, _call
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_observation import _records
from .test_managed_start_operation import GUEST
from .test_managed_start_operation import Carrier as StartCarrier
from .test_managed_start_operation import _records as start_records

RESOURCE = ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "vm:" + GUEST.instance_marker)


class ResourceDelivery(Delivery):
    disposition = "discarded"
    content = b""

    def observe_response(self, request):
        names: tuple[FactName, ...] = self.facts
        controller = None
        if request.operation is ManagedOperation.READ_OUTPUT:
            end = FactName.STDOUT_END if request.stream is Stream.STDOUT else FactName.STDERR_END
            names = (FactName.LAUNCH, end)
        else:
            launch = decode_fact(request.expected_launch)
            controller = ControllerObservation(
                self.controller,
                launch["unit"],
                vm_guest_boot_id(request.guest),
                hashlib.sha256(request.expected_launch).hexdigest(),
            )
        facts = []
        for name in names:
            data = fact(name, request.expected_launch)
            if name in {FactName.STDOUT_END, FactName.STDERR_END}:
                value = decode_fact(data)
                value.update(
                    disposition=self.disposition,
                    retained_bytes=len(self.content),
                    retained_sha256=hashlib.sha256(self.content).hexdigest(),
                )
                data = encode_fact(value)
            facts.append(data)
        return _records(
            request.nonce,
            ManagedResultControl(names, controller),
            tuple(facts),
            self.content if request.operation is ManagedOperation.READ_OUTPUT else b"",
        )


@pytest.fixture
def observer(tmp_path):
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    scope = OperationScope(OperationResourceKind.VM, "vm-one")
    initial = OperationOwner.acquire(database.operations, scope, "initial")
    launched = _call(
        repository,
        initial,
        StartCarrier(lambda request: start_records(request, receipt=True)),
        output=Output.discard(),
        run_owner=RESOURCE,
    )
    assert launched.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    initial.seal_lifecycle_obligations()
    initial.record_effects_resolved()
    initial.close()
    owner = OperationOwner.acquire(database.operations, scope, "observer")
    assert owner.ownership.operation_id != initial.ownership.operation_id
    record = repository.inspect(RUN)
    assert record is not None
    main = ResourceDelivery()
    keeper = Delivery()
    binding = NativeExecutionBinding(main, "root", RUNTIME, _new_managed_delivery=lambda: keeper)
    bootstrap = _NumericGuestBootstrap(PLAN, GUEST)
    operation = ExecutionOperation(
        owner,
        record.spec.target,
        bootstrap=bootstrap,
        managed_repository=repository,
        native_binding=binding,
        resource_owner=RESOURCE,
    )
    access = ExecutionAccess(
        operation,
        main,
        runtime_selection=RUNTIME,
        ordinary_plan=PLAN,
        elevated_plan=PLAN,
        entity_kind="vm",
        entity_name="vm-one",
        deadline=lambda: Deadline.after(5),
    )
    files = FileOperation(owner, record.spec.target, bootstrap=bootstrap)
    file_access = FileAccess(
        files,
        main,
        runtime_selection=RUNTIME,
        trusted_root=PurePosixPath("/tmp"),
        ordinary_plan=PLAN,
        elevated_plan=PLAN,
        entity_kind="vm",
        entity_name="vm-one",
        deadline=lambda: Deadline.after(5),
    )
    workflow = _Workflow(
        owner, Deadline.after(5), views=NativeVMOperation(owner, file_access, access, files, operation)
    )
    try:
        yield database, repository, operation, access, main, workflow
    finally:
        for run in operation.managed_runs:
            assert run.keeper.drain(Deadline.after(2)).drained
        with suppress(StateError):
            workflow.close(cleanup_deadline=Deadline.after(2))
        database.close()


def options(observer) -> dict[str, Any]:
    _, repository, operation, _, main, workflow = observer
    return dict(
        target=operation._target,
        guest=GUEST,
        root_plan=PLAN,
        runtime_selection=RUNTIME,
        deadline=Deadline.after(5),
        owner=workflow.owner,
        execution_operation=operation,
    )


def test_reconnect_and_repeated_reads_share_one_row_then_close_leaves_job(observer):
    database, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    job = JobRef(RUN.run_id)
    for _ in range(140):
        status = access.observe(job)
        assert status.status.value == 7 and status.workload_cleanup_confirmed
    output = access.read_output(job, stream=JobStream.STDOUT)
    assert output.eof and output.retention is Retention.DISCARDED and not output.capture_complete
    result = access.wait(job)
    assert result.status.value == 7 and result.owned_cleanup_confirmed and result.job == job
    rows = workflow.owner.list_pending_lifecycle_obligations()
    assert len(rows) == 1 and rows[0].obligation_kind == "carrier-dispatch"
    assert operation.managed_runs == ()
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert repository.inspect(RUN) == before
    assert database.operations.inspect(workflow.owner.ownership.scope) is None
    assert main.start.calls == main.stop.calls == main.dispose.calls == main.clock.calls == 0


@pytest.mark.parametrize("missing", (FactName.WAIT, None))
@pytest.mark.parametrize("controller", (ControllerState.EXITED, ControllerState.ABSENT))
def test_resource_terminal_proof_does_not_require_application_wait(observer, missing, controller):
    _, _, operation, access, main, _ = observer
    main.facts = tuple(name for name in main.facts if name is not missing)
    main.controller = controller
    result = access.wait(JobRef(RUN.run_id))
    assert result.owned_cleanup_confirmed
    assert result.application_state is (
        ApplicationState.UNKNOWN if missing is FactName.WAIT else ApplicationState.COMPLETED
    )
    assert result.status is None if missing is FactName.WAIT else result.status.value == 7
    assert main.observe.calls == 3 and operation.managed_runs == ()


@pytest.mark.parametrize("controller", (ControllerState.RUNNING, ControllerState.UNKNOWN))
@pytest.mark.parametrize("missing_wait", (False, True))
def test_resource_live_or_unknown_controller_stays_pollable(observer, controller, missing_wait):
    _, repository, operation, access, main, _ = observer
    main.controller = controller
    if missing_wait:
        main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    assert not access.observe(JobRef(RUN.run_id)).workload_cleanup_confirmed
    outcome = collect_bound_managed_result(repository, RUN, carrier=main, **options(observer))
    assert not outcome.result.owned_cleanup_confirmed and outcome.awaiting_facts
    assert operation.managed_runs == ()


@pytest.mark.parametrize("missing_wait", (False, True))
def test_resource_wait_polls_controller_progress_under_same_deadline(observer, missing_wait):
    _, _, _, access, main, _ = observer
    main.controller = ControllerState.RUNNING
    if missing_wait:
        main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    response = main.observe.response
    deadlines = []
    execute = main.execute

    def deliver(invocation, *, io, deadline, custody):
        deadlines.append(deadline)
        return execute(invocation, io=io, deadline=deadline, custody=custody)

    def progress(request):
        if request.operation is ManagedOperation.OBSERVE and main.observe.calls > 3:
            main.controller = ControllerState.EXITED
        return response(request)

    main.execute = deliver
    main.observe.response = progress
    deadline = Deadline.after(3)
    result = access.wait(JobRef(RUN.run_id), deadline=deadline)
    assert result.owned_cleanup_confirmed and main.observe.calls == 6
    assert all(selected is deadline for selected in deadlines)


@pytest.mark.parametrize("launch_state", (ManagedLaunchState.RESERVED, ManagedLaunchState.POSSIBLE_DISPATCH))
def test_terminal_looking_unconfirmed_row_does_not_reconcile_or_prove_closure(observer, launch_state):
    _, repository, _, access, main, _ = observer
    original = repository.inspect(RUN)
    before = repository.reserve(original.spec, output_policy=original.output_policy)
    if launch_state is ManagedLaunchState.POSSIBLE_DISPATCH:
        before = repository.mark_possible_dispatch(before)
    reference = JobRef(before.identity.run_id)
    assert not access.observe(reference).workload_cleanup_confirmed
    with pytest.raises(ValidationError):
        access.wait(reference)
    assert repository.inspect(before.identity) == before and main.observe.calls == 1


@pytest.mark.parametrize("mismatch", ("namespace", "prior-op", "vm", "incarnation", "boot"))
@pytest.mark.parametrize("method", ("observe", "read_output", "wait"))
def test_foreign_persisted_job_refuses_before_borrow(observer, mismatch, method):
    _, repository, operation, access, main, workflow = observer
    original = repository.inspect(RUN)
    spec = original.spec
    if mismatch == "namespace":
        spec = replace(spec, owner=ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "vm:" + "b" * 32))
    elif mismatch == "prior-op":
        spec = replace(
            spec, owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32), lifetime=ManagedRunLifetime.OPERATION
        )
    else:
        target = replace(
            spec.target,
            **{
                "vm": {"name": "vm-two"},
                "incarnation": {"incarnation": "v1:" + "b" * 64},
                "boot": {"boot_id": "00000000-0000-4000-8000-000000000002"},
            }[mismatch],
        )
        spec = replace(spec, target=target)
    foreign = repository.reserve(spec, output_policy=original.output_policy)
    kwargs = {"stream": JobStream.STDOUT} if method == "read_output" else {}
    with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(ValidationError):
        getattr(access, method)(JobRef(foreign.identity.run_id), **kwargs)
    borrow.assert_not_called()
    assert main.observe.calls == 0 and operation.managed_runs == ()


@pytest.mark.parametrize("mismatch", ("namespace", "repository", "owner", "target", "guest", "root_plan", "runtime"))
def test_context_mismatch_refuses_before_managed_lookup(observer, mismatch):
    _, repository, operation, _, main, workflow = observer
    arguments = options(observer)
    if mismatch == "namespace":
        operation = ExecutionOperation(
            workflow.owner,
            operation._target,
            bootstrap=operation._bootstrap,
            managed_repository=repository,
            native_binding=operation._native_binding,
        )
        arguments["execution_operation"] = operation
    else:
        key = {
            "repository": "repository",
            "owner": "owner",
            "target": "target",
            "guest": "guest",
            "root_plan": "root_plan",
            "runtime": "runtime_selection",
        }[mismatch]
        changes = {
            "repository": ManagedRunRepository(observer[0]),
            "owner": object(),
            "target": replace(operation._target, name="vm-two"),
            "guest": replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1),
            "root_plan": replace(PLAN, expected=replace(PLAN.expected, groups=(0, 1))),
            "runtime_selection": replace(RUNTIME, explicit_path="/bin/python3"),
        }
        if key == "repository":
            repository = changes[key]
        else:
            arguments[key] = changes[key]
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        preflight_bound_read(repository, RUN, **arguments)
    inspect.assert_not_called()
    assert main.observe.calls == 0


def test_resource_binding_keeps_strict_operation_preflight_and_expired_wait_refusal(observer):
    _, repository, operation, _, main, _ = observer
    with pytest.raises(ValidationError):
        preflight_bound_run(repository, RUN, **options(observer))
    with pytest.raises(ManagedDeadlineExpired):
        operation.wait_job(JobRef(RUN.run_id), main, Deadline.after(0))
    assert main.observe.calls == main.stop.calls == main.dispose.calls == 0


def test_unknown_read_helper_blocks_observer_close_without_adopting_job(observer):
    _, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    main.observe.dispatch = Dispatch.UNKNOWN
    main.observe.code = None
    status = access.observe(JobRef(RUN.run_id))
    assert not status.workload_cleanup_confirmed and status.failure is ExecutionFailure.OBSERVATION
    assert operation.unfinished_inline_executions and operation.managed_runs == ()
    with pytest.raises(StateError):
        access.observe(JobRef(RUN.run_id))
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert repository.inspect(RUN) == before and main.observe.calls == 1
    assert main.stop.calls == main.dispose.calls == 0


def test_resource_observation_proof_does_not_override_later_unknown_output_helper(observer):
    _, repository, operation, _, main, _ = observer
    before = repository.inspect(RUN)
    response = main.observe.response

    def unknown_output(request):
        if request.stream is not None:
            main.observe.dispatch = Dispatch.UNKNOWN
            main.observe.code = None
        return response(request)

    main.observe.response = unknown_output
    outcome = collect_bound_managed_result(repository, RUN, carrier=main, **options(observer))
    assert outcome.attempts[0].terminal_proved and outcome.attempts[1].requires_owner_retention
    assert not outcome.result.owned_cleanup_confirmed and not outcome.awaiting_facts
    assert outcome.result.failure is ExecutionFailure.OBSERVATION and operation.unfinished_inline_executions
    assert repository.inspect(RUN) == before and operation.managed_runs == () and main.observe.calls == 2


def test_planned_unacknowledged_op_id_never_falls_back_to_matching_resource(observer, monkeypatch):
    _, repository, operation, access, main, _ = observer
    before = repository.inspect(RUN)
    identifiers = iter((UUID(RUN.run_id), UUID("b" * 32), UUID("c" * 32)))
    monkeypatch.setattr(execution_module, "uuid4", lambda: next(identifiers))
    with pytest.raises(StateError):
        access.start(
            Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
        )
    (run,) = operation.managed_runs
    assert run.reservation_uncertain and not run.acknowledged
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        access.observe(JobRef(RUN.run_id))
    inspect.assert_not_called()
    assert repository.inspect(RUN) == before and main.observe.calls == 0


def test_selected_route_refusal_fences_resource_helper_delivery(observer):
    _, repository, operation, access, main, _ = observer
    before = repository.inspect(RUN)
    route = Mock(spec=WSL2OwnedOperation)
    refusal = StateError("selected route changed")
    route.require_selected_route.side_effect = refusal
    operation._wsl2_route = route
    with pytest.raises(StateError) as raised:
        access.observe(JobRef(RUN.run_id))
    assert raised.value is refusal
    route.require_selected_route.assert_called_once()
    assert main.observe.calls == 0 and repository.inspect(RUN) == before and operation.managed_runs == ()


@pytest.mark.parametrize("binding", (object(), ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32)))
def test_malformed_constructor_resource_binding_refuses_without_effects(observer, binding):
    _, repository, operation, _, main, workflow = observer
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        ExecutionOperation(
            workflow.owner,
            operation._target,
            bootstrap=operation._bootstrap,
            managed_repository=repository,
            native_binding=operation._native_binding,
            resource_owner=binding,
        )
    inspect.assert_not_called()
    assert main.observe.calls == 0 and workflow.owner.list_pending_lifecycle_obligations() == ()


@pytest.mark.parametrize("disposition", ("complete-capture", "truncated-capture", "sensitivity-suppressed"))
def test_resource_output_preserves_policy_cursors_and_capture_completeness(observer, disposition):
    _, repository, operation, access, main, workflow = observer
    identity = ManagedRunIdentity("f" * 32)
    suppressed = disposition == "sensitivity-suppressed"
    launched = _call(
        repository,
        workflow.owner,
        StartCarrier(lambda request: start_records(request, receipt=True)),
        identity=identity,
        obligation_id="a" * 32,
        run_owner=RESOURCE,
        output=Output.capture(3),
        input=Input.sensitive(b"private input") if suppressed else Input.eof(),
    )
    assert launched.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    main.disposition = disposition
    main.content = b"" if suppressed else b"abc"
    job = JobRef(identity.run_id)
    first = access.read_output(job, stream=JobStream.STDOUT, max_bytes=2)
    assert first.data == (b"" if suppressed else b"ab")
    assert first.eof is suppressed
    assert first.capture_complete is (disposition == "complete-capture")
    last = access.read_output(job, stream=JobStream.STDOUT, cursor=first.next_cursor)
    assert last.data == (b"" if suppressed else b"c") and last.eof
    assert last.capture_complete is (disposition == "complete-capture")
    assert last.failure is (ExecutionFailure.OUTPUT_LIMIT if disposition == "truncated-capture" else None)
    with pytest.raises(ValidationError):
        access.read_output(job, stream=JobStream.STDOUT, cursor=len(main.content) + 1)
    result = access.wait(job)
    assert result.stdout.data == main.content and result.stderr.data == main.content
    assert result.owned_cleanup_confirmed and operation.managed_runs == ()
    assert repository.inspect(identity).output_policy == launched.attempt.record.output_policy


def test_stale_observer_generation_refuses_before_resource_borrow_or_delivery(observer):
    database, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    successor = OperationOwner.recover(database.operations, workflow.owner.ownership, "b" * 32)
    try:
        with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(StateError):
            access.observe(JobRef(RUN.run_id))
        borrow.assert_not_called()
        assert main.observe.calls == 0 and repository.inspect(RUN) == before and operation.managed_runs == ()
        assert successor.list_pending_lifecycle_obligations() == ()
        successor.record_effects_resolved()
        successor.close()
    finally:
        assert main.stop.calls == main.dispose.calls == 0


@pytest.mark.parametrize("method", ("observe", "read_output", "wait"))
def test_resource_reads_refuse_another_components_active_borrow(observer, method):
    _, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    borrow = workflow.owner.borrow()
    try:
        kwargs = {"stream": JobStream.STDOUT} if method == "read_output" else {}
        with pytest.raises(StateError):
            getattr(access, method)(JobRef(RUN.run_id), **kwargs)
        assert main.observe.calls == 0 and repository.inspect(RUN) == before and operation.managed_runs == ()
        assert operation._dispatch_id is None and workflow.owner._active_borrow is borrow
    finally:
        borrow.close()
