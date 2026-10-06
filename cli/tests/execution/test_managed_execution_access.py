"""Bound managed admission and ordinary aggregate close with framed helpers."""

from __future__ import annotations

import hashlib
import inspect
import sys
import threading
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import (
    _managed_lease_bundle,
    _managed_observation_bundle,
    _managed_start_bundle,
    _managed_stop_bundle,
)
from agentworks.execution import (
    _managed_operation_keeper as keeper_module,
)
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import (
    FACT_ORDER,
    ControllerObservation,
    ControllerState,
    ManagedObservationRequest,
    ManagedResultControl,
)
from agentworks.execution._managed_operation_keeper import KeeperDrain
from agentworks.execution._managed_runs import ManagedRunRepository
from agentworks.execution._managed_stop_protocol import ManagedStopRequest, ManagedStopResult
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    FiniteInput,
    PreparedInvocation,
)
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.vms._native_operation import NativeVMOperation, _Workflow

from . import test_managed_observation as observation_tests
from . import test_managed_operation_keeper as keeper_tests
from . import test_managed_stop as stop_tests
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_lease_exchange import ScriptedCarrier as LeaseCarrier
from .test_managed_lease_exchange import _success as lease_success
from .test_managed_start_operation import GUEST
from .test_managed_start_operation import Carrier as StartCarrier
from .test_managed_start_operation import _records as start_records

pytestmark = pytest.mark.windows
bound = keeper_tests.bound


def fact(name: FactName, launch: bytes) -> bytes:
    value = decode_fact(launch)
    common = dict(
        version=1, run_id=value["run_id"], unit=value["unit"], receipt_sha256=hashlib.sha256(launch).hexdigest()
    )
    if name is FactName.LAUNCH:
        return launch
    if name is FactName.WAIT:
        return encode_fact(dict(common, kind="wait", exit_code=7, signal=None))
    if name is FactName.BOUNDARY_EMPTY:
        return encode_fact(dict(common, kind="boundary-empty"))
    return encode_fact(
        dict(
            common,
            kind="stream-end",
            stream="stdout" if name is FactName.STDOUT_END else "stderr",
            retained_bytes=0,
            retained_sha256=hashlib.sha256(b"").hexdigest(),
            disposition="discarded",
        )
    )


class Delivery:
    def __init__(self) -> None:
        self.clock = LeaseCarrier()
        self.start = StartCarrier(lambda request: start_records(request, receipt=True))
        self.stop = stop_tests.Carrier(self.stop_response)
        self.observe = observation_tests.ScriptedCarrier(self.observe_response)
        self.controller = ControllerState.EXITED
        self.facts = FACT_ORDER
        self.stop_empty = True

    @property
    def features(self) -> ChannelFeatures:
        return self.clock.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.selected(io).validate(invocation, io=io)

    def selected(
        self, io: CarrierIO
    ) -> LeaseCarrier | StartCarrier | stop_tests.Carrier | observation_tests.ScriptedCarrier:
        assert isinstance(io.input, FiniteInput)
        for prefix, carrier in (
            (_managed_lease_bundle.FIXED_BUNDLE.prefix, self.clock),
            (_managed_start_bundle.FIXED_BUNDLE.prefix, self.start),
            (_managed_stop_bundle.FIXED_BUNDLE.prefix, self.stop),
            (_managed_observation_bundle.FIXED_BUNDLE.prefix, self.observe),
        ):
            if io.input.data.startswith(prefix):
                return carrier
        raise AssertionError("unexpected helper")

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        selected = self.selected(io)
        return selected.execute(invocation, io=io, deadline=deadline, custody=custody)

    def stop_response(self, request: ManagedStopRequest) -> bytes:
        names = (FactName.LAUNCH, FactName.BOUNDARY_EMPTY) if self.stop_empty else (FactName.LAUNCH,)
        return stop_tests._records(
            request.nonce, ManagedStopResult(names), tuple(fact(name, request.expected_launch) for name in names)
        )

    def observe_response(self, request: ManagedObservationRequest) -> bytes:
        launch = decode_fact(request.expected_launch)
        controller = ControllerObservation(
            self.controller,
            launch["unit"],
            vm_guest_boot_id(request.guest),
            hashlib.sha256(request.expected_launch).hexdigest(),
        )
        return observation_tests._records(
            request.nonce,
            ManagedResultControl(self.facts, controller),
            tuple(fact(name, request.expected_launch) for name in self.facts),
        )


@pytest.fixture
def view(bound):
    database, owner, receipt = bound
    main, keeper = Delivery(), Delivery()
    binding = NativeExecutionBinding(main, "root", RUNTIME, _new_managed_delivery=lambda: keeper)
    bootstrap = _NumericGuestBootstrap(PLAN, GUEST)
    operation = ExecutionOperation(
        owner,
        receipt.spec.target,
        bootstrap=bootstrap,
        managed_repository=ManagedRunRepository(database),
        native_binding=binding,
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
    files = FileOperation(owner, receipt.spec.target, bootstrap=bootstrap)
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
        yield database, workflow, access, main, keeper
    finally:
        for run in operation.managed_runs:
            assert run.keeper.drain(Deadline.after(2)).drained


@pytest.mark.parametrize("invocation", [Command(["/usr/bin/true"]), Script("printf hello", Shell.SH)])
def test_bound_ack_and_normal_aggregate_close(view, invocation):
    database, workflow, access, main, keeper = view
    reference = access.start(
        invocation,
        profile=Protection.MANAGED,
        lifetime=Lifetime.OPERATION,
        stdin=Input.bytes(b"input"),
        env={"LANG": "C"},
        cwd="/tmp",
        output=Output.discard(),
    )
    assert isinstance(reference, JobRef)
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.receipt.identity.run_id == reference.run_id and run.acknowledged
    assert main.start.calls == 1 and keeper.clock.calls >= 1
    assert run.keeper._carrier is keeper and main is not keeper
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert run.cleanup_complete and keeper.stop.calls == keeper.observe.calls == 1
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


def test_uncommitted_reservation_interruption_is_not_a_launched_run(view, monkeypatch):
    database, workflow, access, main, keeper = view
    control = KeyboardInterrupt("reserve interrupted before commit")

    def reserve(*args, **kwargs):
        raise control

    monkeypatch.setattr(workflow.views.execution_operation._managed_repository, "reserve", reserve)
    with pytest.raises(KeyboardInterrupt) as caught:
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    (run,) = workflow.views.execution_operation.managed_runs
    assert caught.value is control and run.reservation_started and not run.reservation_uncertain
    assert run.reservation_observation is None and run.reserved is None
    assert keeper.clock.calls == main.start.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert run.cleanup_complete and database.operations.inspect(workflow.owner.ownership.scope) is None


def test_known_failed_clock_never_claims_start_and_normal_close_resolves_only_probe(view):
    database, workflow, access, main, keeper = view
    keeper.clock.runtime = "missing"
    with pytest.raises(StateError) as caught:
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.start_outcome is None and not run.acknowledged and run.keeper.last_clock is not None
    assert main.start.calls == 0 and run.keeper._worker is None
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert keeper.stop.calls == keeper.observe.calls == 0
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


@pytest.mark.parametrize("change", ["profile", "lifetime", "shell", "body", "unsupported", "closed"])
def test_invalid_admission_has_no_reservation_or_clock(view, change):
    database, workflow, access, main, keeper = view
    args: dict[str, Any] = dict(profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    invocation: Command | Script = Command(["/bin/true"])
    if change == "profile":
        args["profile"] = Protection.DIRECT
    if change == "lifetime":
        args["lifetime"] = Lifetime.INDEPENDENT
    if change == "shell":
        invocation = Script("true", Shell.USER_DEFAULT)
    if change == "body":
        args["env"] = {"BAD=NAME": "value"}
    if change == "unsupported":
        workflow.views.execution_operation._native_binding = replace(
            workflow.views.execution_operation._native_binding, _new_managed_delivery=None
        )
    if change == "closed":
        workflow.owner.stop_admission()
    with pytest.raises((StateError, ValidationError)):
        access.start(invocation, **args)
    assert not workflow.views.execution_operation.managed_runs
    assert main.start.calls == keeper.clock.calls == 0
    assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    workflow.close(cleanup_deadline=Deadline.after(2))


@pytest.mark.parametrize(
    "missing", [FactName.WAIT, FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY, "controller"]
)
def test_cleanup_independent_facts_block_release_then_retry(view, missing):
    database, workflow, access, _, keeper = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    if missing == "controller":
        keeper.controller = ControllerState.UNKNOWN
    else:
        keeper.facts = tuple(name for name in FACT_ORDER if name is not missing)
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    keeper.facts, keeper.controller = FACT_ORDER, ControllerState.ABSENT
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_all_keeper_drains_attempted_before_any_guarded_finish(view, monkeypatch, interrupted):
    _, workflow, access, _, _ = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    # The platform factory must supply a new dedicated delivery for each run.
    operation = workflow.views.execution_operation
    operation._native_binding = replace(operation._native_binding, _new_managed_delivery=Delivery)
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    first, second = operation.managed_runs
    for run in (first, second):
        assert run.keeper.drain(Deadline.after(2)).drained
    calls = []
    control = KeyboardInterrupt("drain interrupted")

    def blocked(deadline):
        calls.append((first, deadline))
        if interrupted:
            raise control
        return KeeperDrain(True, False, False)

    monkeypatch.setattr(first.keeper, "drain", blocked)
    monkeypatch.setattr(
        second.keeper, "drain", lambda deadline: calls.append((second, deadline)) or KeeperDrain(False, False, True)
    )
    monkeypatch.setattr(operation, "finish", lambda: pytest.fail("guarded finish before all drains"))
    budget = Deadline.after(2)
    with pytest.raises(KeyboardInterrupt if interrupted else StateError) as caught:
        workflow.close(cleanup_deadline=budget)
    if interrupted:
        assert caught.value is control
    assert calls == [(first, budget), (second, budget)]
    monkeypatch.undo()


def test_unknown_start_keeps_independent_debt_even_with_terminal_store(view):
    database, workflow, access, main, keeper = view
    main.start.code = None
    with pytest.raises(StateError) as caught:
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    (run,) = workflow.views.execution_operation.managed_runs
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    assert caught.value.__cause__.reference.run_id == run.receipt.identity.run_id
    assert not run.acknowledged and run.keeper._worker is None
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    rows = workflow.owner.list_lifecycle_obligations()
    assert any(
        row.obligation_kind == "managed-start" and row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows
    )
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


def test_stop_accepted_is_not_workload_empty(view):
    database, workflow, access, _, keeper = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    keeper.stop_empty = False
    keeper.facts = (FactName.LAUNCH,)
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert keeper.stop.calls == keeper.observe.calls == 1
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    keeper.facts = FACT_ORDER
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert keeper.stop.calls == 1


def test_pure_closing_preparation_refusal_does_not_arm_exchange(view, monkeypatch):
    _, workflow, access, _, keeper = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    control = KeyboardInterrupt("validation interrupted")
    validate = keeper.stop.validate

    def refuse(*args, **kwargs):
        raise control

    monkeypatch.setattr(keeper.stop, "validate", refuse)
    with pytest.raises(KeyboardInterrupt) as caught:
        workflow.close(cleanup_deadline=Deadline.after(2))
    (run,) = workflow.views.execution_operation.managed_runs
    assert caught.value is control and not run.keeper._closing_pending and keeper.stop.calls == 0
    monkeypatch.setattr(keeper.stop, "validate", validate)
    workflow.close(cleanup_deadline=Deadline.after(2))


def test_current_unknown_helper_cannot_be_cleared_by_old_cleanup_reply(view, monkeypatch):
    _, workflow, access, _, keeper = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    (run,) = workflow.views.execution_operation.managed_runs
    workflow.owner.stop_admission()
    assert run.keeper.drain(Deadline.after(2)).drained
    run.keeper.request_stop(Deadline.after(2))
    run.keeper.observe_cleanup(Deadline.after(2))
    keeper.stop.code = None
    with pytest.raises(StateError):
        run.keeper.request_stop(Deadline.after(2))
    assert run.keeper._closing_pending and run.keeper.last_cleanup is not None
    assert run.keeper.drain(Deadline.after(2)).drained
    monkeypatch.setattr(workflow.views.execution_operation, "finish", lambda: pytest.fail("finish before remote gate"))
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert keeper.stop.calls == 2 and keeper.observe.calls == 1


@pytest.mark.parametrize("boundary", ["delivery", "candidate", "resolution"])
def test_interrupted_known_cleanup_bookkeeping_is_retryable(view, monkeypatch, boundary):
    from agentworks.execution._managed_operation_keeper import _ClosingCarrier
    from agentworks.operations import LifecycleObligation

    database, workflow, access, _, keeper = view
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    (run,) = workflow.views.execution_operation.managed_runs
    control = KeyboardInterrupt("cleanup bookkeeping interrupted")
    method = _ClosingCarrier.execute if boundary == "delivery" else run.keeper.request_stop
    if boundary == "resolution":
        original = LifecycleObligation.resolve

        def resolve(handle):
            original(handle)
            raise control

        monkeypatch.setattr(LifecycleObligation, "resolve", resolve)
    else:
        lines, first = inspect.getsourcelines(method)
        statement = "return report" if boundary == "delivery" else "self._settle_closing(deadline)"
        target = first + next(index for index, line in enumerate(lines) if line.strip() == statement)

        def interrupt(frame, event, arg):
            if event == "line" and frame.f_code is method.__code__ and frame.f_lineno == target:
                sys.settrace(None)
                raise control
            return interrupt

        sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            workflow.close(cleanup_deadline=Deadline.after(2))
        assert caught.value is control
    finally:
        sys.settrace(None)
        monkeypatch.undo()
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert database.operations.inspect(workflow.owner.ownership.scope) is None
    assert keeper.stop.calls == (2 if boundary == "delivery" else 1)


def test_escaping_initial_clock_retains_original_and_safe_reference(view):
    _, workflow, access, main, keeper = view
    control = KeyboardInterrupt("clock interrupted")
    keeper.clock.interrupt = control
    with pytest.raises(KeyboardInterrupt) as caught:
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    assert caught.value is control and isinstance(control.__cause__, ManagedExecutionControlFact)
    (run,) = workflow.views.execution_operation.managed_runs
    assert control.__cause__.reference.run_id == run.receipt.identity.run_id
    assert run.keeper.failed and run.reserved is not None and main.start.calls == 0
    assert run.keeper._worker is None


def test_bound_renewal_during_serial_ordinary_command_attempt(view, monkeypatch):
    from agentworks.execution.carrier import CarrierReport, Dispatch

    from .test_execution_operation import RecordingCarrier

    _, workflow, access, main, keeper = view
    monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0.001)
    ordinary_active, published = threading.Event(), threading.Event()

    def lease_response(request):
        if keeper.clock.calls > 1 and request.lease is None:
            assert ordinary_active.wait(5)
        if request.lease is not None:
            with workflow.owner._guard:
                assert workflow.owner._active_borrow is not None
                assert workflow.owner._outstanding_attempt is not None
            workflow.views.execution_operation.managed_runs[0].keeper._stop.set()
            published.set()
        return lease_success(request)

    keeper.clock.response = lease_response
    access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)

    def ordinary_dispatch():
        ordinary_active.set()
        assert published.wait(5)

    ordinary = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT), before_dispatch=ordinary_dispatch)
    selected = main.selected

    def select(io):
        try:
            return selected(io)
        except AssertionError:
            return ordinary

    monkeypatch.setattr(main, "selected", select)
    try:
        access.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        assert ordinary.calls == 1 and published.is_set()
    finally:
        ordinary_active.set()
    workflow.close(cleanup_deadline=Deadline.after(5))
