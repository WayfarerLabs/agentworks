"""Bound managed admission and ordinary aggregate close with framed helpers."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import (
    _managed_disposal_bundle,
    _managed_lease_bundle,
    _managed_observation_bundle,
    _managed_start_bundle,
    _managed_stop_bundle,
)
from agentworks.execution import (
    _managed_operation_keeper as keeper_module,
)
from agentworks.execution import _managed_service_guest as service_guest
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_request import ManagedJobRequest
from agentworks.execution._managed_job_store import FactName, ManagedJobStore
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_lease_wire import boottime_ns, sampled_lease
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
from agentworks.execution.jobs import JobStream
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure
from agentworks.operations import OperationBorrow
from agentworks.vms._native_operation import NativeVMOperation, _Workflow

from . import test_managed_disposal as disposal_tests
from . import test_managed_observation as observation_tests
from . import test_managed_operation_keeper as keeper_tests
from . import test_managed_stop as stop_tests
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_lease_exchange import ScriptedCarrier as LeaseCarrier
from .test_managed_lease_exchange import _success as lease_success
from .test_managed_service_guest import Boundary
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
        self.dispose = disposal_tests.ExchangeCarrier(disposal_tests._disposed)
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
    ) -> (
        LeaseCarrier
        | StartCarrier
        | stop_tests.Carrier
        | observation_tests.ScriptedCarrier
        | disposal_tests.ExchangeCarrier
    ):
        assert isinstance(io.input, FiniteInput)
        for prefix, carrier in (
            (_managed_lease_bundle.FIXED_BUNDLE.prefix, self.clock),
            (_managed_start_bundle.FIXED_BUNDLE.prefix, self.start),
            (_managed_stop_bundle.FIXED_BUNDLE.prefix, self.stop),
            (_managed_observation_bundle.FIXED_BUNDLE.prefix, self.observe),
            (_managed_disposal_bundle.FIXED_BUNDLE.prefix, self.dispose),
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


def test_bound_job_reads_share_one_lifetime_row(view, monkeypatch):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    for _ in range(150):
        status = access.observe(reference)
        assert status.application_state is ApplicationState.COMPLETED
        assert status.status.value == 7 and status.workload_cleanup_confirmed
    from agentworks.execution.carrier import CarrierReport, Dispatch

    from .test_execution_operation import RecordingCarrier

    ordinary = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    selected = main.selected

    def select(io):
        try:
            return selected(io)
        except AssertionError:
            return ordinary

    monkeypatch.setattr(main, "selected", select)
    for _ in range(3):
        access.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        access.observe(reference)
    rows = database.operations.list_pending_lifecycle_obligations(workflow.owner.ownership)
    assert len([row for row in rows if row.obligation_kind == "carrier-dispatch"]) == 1
    assert keeper.stop.calls == 0
    main.observe.response = lambda request: observation_tests._records(
        request.nonce,
        ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
        (request.expected_launch, fact(FactName.STDOUT_END, request.expected_launch)),
    )
    output = access.read_output(reference, stream=JobStream.STDOUT)
    assert output.eof and not output.capture_complete and output.data == b""
    assert output.failure is None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.skipif(sys.platform != "linux", reason="Actual POSIX helper filesystem requires Linux")
def test_actual_file_calls_mix_with_lifetime_job_reads(view, tmp_path, monkeypatch):
    from .files._file_read_support import LocalCarrier
    from .files._file_snapshot_support import install_fixture_bundle
    from .files._runtime_support import runtime_selection

    database, workflow, access, _, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    for _ in range(150):
        assert access.observe(reference).workload_cleanup_confirmed
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    file_plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )
    files = FileAccess(
        FileOperation(workflow.owner, workflow.views.execution_operation.managed_runs[0].receipt.spec.target),
        LocalCarrier(),
        trusted_root=PurePosixPath(tmp_path),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=file_plan,
        elevated_plan=None,
        entity_kind="file",
        entity_name="fixture",
        deadline=lambda: Deadline.after(5),
    )
    (tmp_path / "data").write_bytes(b"mixed-file-bytes")
    for _ in range(3):
        read = files.read_file(PurePosixPath(tmp_path / "data"), max_bytes=100)
        assert read is not None and read.data == b"mixed-file-bytes"
        access.observe(reference)
    rows = database.operations.list_pending_lifecycle_obligations(workflow.owner.ownership)
    assert len([row for row in rows if row.obligation_kind == "carrier-dispatch"]) == 1
    assert keeper.stop.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_foreign_job_reference_refused_before_delivery(view):
    _, _, access, main, keeper = view
    with pytest.raises(ValidationError):
        access.observe(JobRef("a" * 32))
    assert main.observe.calls == keeper.stop.calls == 0


@pytest.mark.parametrize("fault", ["workload", "target", "owner", "output", "receipt"])
def test_operation_row_mismatch_refused_before_job_effects(view, monkeypatch, fault):
    from agentworks.execution._managed_runs import ManagedLaunchState, ManagedOutputMode, ManagedOutputPolicy

    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    operation = workflow.views.execution_operation
    record = operation._managed_repository.inspect(operation.managed_runs[0].receipt.identity)
    if fault == "workload":
        record = replace(record, spec=replace(record.spec, workload=IdentityExpectation(111, 111, (111,))))
    elif fault == "target":
        record = replace(
            record, spec=replace(record.spec, target=replace(record.spec.target, incarnation="v1:" + "f" * 64))
        )
    elif fault == "owner":
        record = replace(record, spec=replace(record.spec, owner=replace(record.spec.owner, owner_id="e" * 32)))
    elif fault == "output":
        record = replace(record, output_policy=ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4))
    else:
        record = replace(record, launch_state=ManagedLaunchState.NOT_LAUNCHED)
    with monkeypatch.context() as patch:
        patch.setattr(operation._managed_repository, "inspect", lambda identity: record)
        for method in (access.observe, access.wait, access.stop, access.dispose):
            with pytest.raises(ValidationError):
                method(reference)
        with pytest.raises(ValidationError):
            access.read_output(reference, stream=JobStream.STDOUT)
    assert main.observe.calls == main.dispose.calls == keeper.stop.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_active_disposal_keeps_keeper_live(view):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.facts = (FactName.LAUNCH,)
    outcome = access.dispose(reference)
    assert outcome.disposed is False and main.dispose.calls == keeper.stop.calls == 0
    assert not workflow.views.execution_operation.managed_runs[0].keeper._stop.is_set()
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_explicit_stop_retains_terminal_proof_and_closes_normally(view):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    outcome = access.stop(reference)
    assert outcome.accepted and outcome.terminated
    assert main.stop.calls == main.observe.calls == 1
    assert keeper.stop.calls == keeper.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert keeper.stop.calls == keeper.observe.calls == 0


def test_stop_drains_only_selected_keeper(view):
    _, workflow, access, main, first_keeper = view
    first = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    operation = workflow.views.execution_operation
    second_keeper = Delivery()
    operation._native_binding = replace(operation._native_binding, _new_managed_delivery=lambda: second_keeper)
    second = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    assert access.stop(first).accepted
    assert main.stop.calls == 1 and first_keeper.stop.calls == second_keeper.stop.calls == 0
    assert operation.managed_runs[0].keeper._stop.is_set()
    assert not operation.managed_runs[1].keeper._stop.is_set()
    assert access.observe(second).reference == second
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert second_keeper.stop.calls == 1 and main.start.calls == 2


def test_accepted_stop_keeps_intent_when_observation_deadline_expires(view, monkeypatch):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    original = main.stop.execute
    clock = [0.0]

    def stop(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 2.0
        return result

    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: clock[0])
        patch.setattr(main.stop, "execute", stop)
        result = access.stop(reference, deadline=Deadline.after(1))
    assert result.accepted and not result.terminated and result.deadline_exceeded
    assert result.failure is ExecutionFailure.DEADLINE and main.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_operation_wait_requires_controller_and_stream_closure(view):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    observe_response = main.observe_response

    def response(request):
        if request.stream is None:
            return observe_response(request)
        end = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
        return observation_tests._records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, end)),
            (request.expected_launch, fact(end, request.expected_launch)),
        )

    main.observe.response = response
    main.controller = ControllerState.UNKNOWN
    assert not access.wait(reference, deadline=Deadline.after(0.01)).owned_cleanup_confirmed
    main.controller = ControllerState.EXITED
    assert access.wait(reference).owned_cleanup_confirmed
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_confirmed_disposal_retains_proof_for_aggregate_close(view):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    assert access.dispose(reference).disposed
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.disposal_confirmed and run.terminal_observation is not None
    assert keeper.stop.calls == keeper.observe.calls == 0
    main.facts = ()
    assert access.dispose(reference).disposed and main.dispose.calls == 1
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert keeper.stop.calls == keeper.observe.calls == 0
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


def test_known_terminated_lost_disposal_reply_reuses_exact_obligation(view):
    database, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.dispose.response = lambda request: b""
    (run,) = workflow.views.execution_operation.managed_runs
    old_id = run.disposal_obligation_id
    first = access.dispose(reference)
    assert first.disposed is None and first.failure is ExecutionFailure.OBSERVATION
    assert main.observe.calls == 1
    main.dispose.response = disposal_tests._disposed
    assert access.dispose(reference).disposed
    assert run.disposal_obligation_id == old_id
    assert main.observe.calls == 1 and keeper.stop.calls == 0
    row = workflow.owner.inspect_lifecycle_obligation(old_id)
    assert row is not None and row.state is LifecycleObligationState.RESOLVED
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("refusal", ["not-sent", "not-ready", "pre-dispatch"])
def test_disposal_clean_refusal_uses_new_row_on_later_explicit_attempt(view, refusal):
    from agentworks.execution.carrier import Dispatch

    from .test_managed_disposal_access import _not_ready

    database, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    old_id = run.disposal_obligation_id
    if refusal == "not-sent":
        main.dispose.dispatch = Dispatch.NOT_SENT
    elif refusal == "not-ready":
        main.dispose.response = _not_ready
    else:
        main.dispose.refuse_validation = True
    if refusal == "pre-dispatch":
        with pytest.raises(ValidationError):
            access.dispose(reference)
    else:
        assert access.dispose(reference).disposed is False
    main.dispose.dispatch = Dispatch.SENT
    main.dispose.response = disposal_tests._disposed
    main.dispose.refuse_validation = False
    next_id = run.disposal_obligation_id
    assert access.dispose(reference).disposed
    assert next_id != old_id
    disposed_rows = [workflow.owner.inspect_lifecycle_obligation(identity) for identity in (old_id, next_id)]
    assert all(row is not None and row.state is LifecycleObligationState.RESOLVED for row in disposed_rows)
    assert main.observe.calls == 2
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_unknown_disposal_helper_blocks_retry_and_aggregate_release(view):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.dispose.code = None
    (run,) = workflow.views.execution_operation.managed_runs
    old_id = run.disposal_obligation_id
    outcome = access.dispose(reference)
    assert outcome.disposed is None
    with pytest.raises(StateError):
        access.dispose(reference)
    assert run.disposal_obligation_id == old_id
    assert main.dispose.calls == 1 and main.observe.calls == 1
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))


def test_late_wait_snapshot_recovers_precision(view):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.facts = tuple(name for name in FACT_ORDER if name is not FactName.WAIT)
    first = access.observe(reference)
    assert first.status is None and first.application_state is ApplicationState.UNKNOWN
    assert first.workload_cleanup_confirmed
    main.facts = FACT_ORDER
    assert access.observe(reference).status.value == 7
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("disposition", ["truncated-capture", "complete-capture"])
def test_retained_output_eof_is_separate_from_capture_completeness(view, disposition):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.capture(3)
    )

    def response(request):
        end = decode_fact(fact(FactName.STDOUT_END, request.expected_launch))
        end.update(disposition=disposition, retained_bytes=3, retained_sha256=hashlib.sha256(b"abc").hexdigest())
        return observation_tests._records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (request.expected_launch, encode_fact(end)),
            b"abc",
        )

    main.observe.response = response
    first = access.read_output(reference, stream=JobStream.STDOUT, max_bytes=2)
    assert first.data == b"ab" and not first.eof
    assert first.capture_complete is (disposition == "complete-capture")
    second = access.read_output(reference, stream=JobStream.STDOUT, cursor=first.next_cursor)
    assert second.data == b"c" and second.eof
    assert second.capture_complete is (disposition == "complete-capture")
    assert second.failure is (ExecutionFailure.OUTPUT_LIMIT if disposition == "truncated-capture" else None)
    with pytest.raises(ValidationError):
        access.read_output(reference, stream=JobStream.STDOUT, cursor=4)
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("argv", [("/definitely-absent-agw-executable",), ("/bin/sh", "-c", "kill -TERM $$")])
@pytest.mark.skipif(sys.platform != "linux", reason="Actual forked guest producer requires Linux")
def test_real_controller_without_wait_can_close_operation(view, tmp_path, argv):
    _, workflow, access, main, keeper = view
    access._ordinary_plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )
    reference = access.start(
        Command(list(argv)), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    (run,) = workflow.views.execution_operation.managed_runs
    launch = encode_managed_job_fact(run.receipt)
    assert run.keeper.drain(Deadline.after(2)).drained
    tmp_path.chmod(0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        store = ManagedJobStore(reference.run_id, _anchor_fd=fd, _namespace="managed", _owner_uid=os.getuid())
    finally:
        os.close(fd)
    try:
        store.publish_request(
            ManagedJobRequest(
                launch,
                "command",
                argv,
                None,
                "discard",
                None,
                (),
                b"",
                b"",
                operation_lease=sampled_lease(launch, boottime_ns()),
            )
        )
        assert (
            service_guest.run(
                reference.run_id,
                _store=store,
                _boundary=Boundary(),
                _notify=lambda: None,
                _identity_check=False,
                _apply_identity=False,
            )
            == 0
        )
        assert store.read_fact(FactName.WAIT) is None
        names = tuple(name for name in FACT_ORDER if store.read_fact(name) is not None)
        facts = tuple(store.read_fact(name) for name in names)

        def response(request):
            controller = ControllerObservation(
                ControllerState.EXITED,
                run.receipt.unit_name,
                run.receipt.spec.target.boot_id,
                hashlib.sha256(launch).hexdigest(),
            )
            return observation_tests._records(request.nonce, ManagedResultControl(names, controller), facts)

        main.observe.response = keeper.observe.response = response
        status = access.observe(reference)
        assert status.application_state is ApplicationState.UNKNOWN and status.status is None
        assert status.workload_cleanup_confirmed
        workflow.close(cleanup_deadline=Deadline.after(5))
        assert run.cleanup_complete
    finally:
        store.close()


def test_wait_deadline_preserves_job_and_keeper(view, monkeypatch):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    main.facts = (FactName.LAUNCH,)
    clock = [0.0]
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: clock[0])
        patch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        result = access.wait(reference, deadline=Deadline.after(0.3))
    assert result.job == reference and result.deadline_exceeded
    assert result.failure is ExecutionFailure.DEADLINE
    assert keeper.stop.calls == 0
    assert not workflow.views.execution_operation.managed_runs[0].keeper._stop.is_set()
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize(
    "transition", ["install_dispatch_obligation", "arm_dispatch_obligation", "handoff_retained_effect"]
)
def test_read_lost_bookkeeping_reply_reuses_retained_lifetime_row(view, monkeypatch, transition):
    database, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    original = getattr(OperationBorrow, transition)
    lost = [False]

    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not lost[0]:
            lost[0] = True
            raise KeyboardInterrupt("lost local reply")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(OperationBorrow, transition, interrupted)
        with pytest.raises(KeyboardInterrupt):
            access.observe(reference)
    operation = workflow.views.execution_operation
    operation.retry_inline_bookkeeping()
    access.observe(reference)
    rows = database.operations.list_pending_lifecycle_obligations(workflow.owner.ownership)
    assert len([row for row in rows if row.obligation_kind == "carrier-dispatch"]) == 1
    assert main.observe.calls <= 2
    workflow.close(cleanup_deadline=Deadline.after(5))


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
        invocation = Script("true", Shell.USER_DEFAULT, interactive=True)
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


@pytest.mark.parametrize("missing", [FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY, "controller"])
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
    for method in (access.observe, access.wait, access.stop, access.dispose):
        with pytest.raises(ValidationError):
            method(caught.value.__cause__.reference)
    with pytest.raises(ValidationError):
        access.read_output(caught.value.__cause__.reference, stream=JobStream.STDOUT)
    assert main.observe.calls == main.dispose.calls == 0
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    rows = workflow.owner.list_pending_lifecycle_obligations()
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
    if boundary == "resolution":
        original_resolve = LifecycleObligation.resolve

        def resolve(handle):
            original_resolve(handle)
            raise control

        monkeypatch.setattr(LifecycleObligation, "resolve", resolve)
    elif boundary == "delivery":
        original_execute = _ClosingCarrier.execute

        def execute(*args, **kwargs):
            original_execute(*args, **kwargs)
            raise control

        monkeypatch.setattr(_ClosingCarrier, "execute", execute)
    else:
        original_settle = run.keeper._settle_closing

        def settle(deadline):
            if run.keeper._closing_pending:
                raise control
            original_settle(deadline)

        monkeypatch.setattr(run.keeper, "_settle_closing", settle)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            workflow.close(cleanup_deadline=Deadline.after(2))
        assert caught.value is control
    finally:
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
