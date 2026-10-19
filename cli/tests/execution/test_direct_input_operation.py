"""Finite DIRECT input composition and independent activation/cleanup custody."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact, OwnedInlineOutcome
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._file_stage_protocol import FileStageChunkRequest, FileStageCleanupRequest, FileStageOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import prepare_inline_candidate
from agentworks.execution._inline_bundle import PREPARED_PROGRAM
from agentworks.execution._inline_request import REQUEST_HEADER_BYTES, decode_prepared_manifest
from agentworks.execution._prepared_helper_obligation import PREPARED_HELPER_OBLIGATION_KIND
from agentworks.execution._prepared_helper_stage import PreparedHelperStage
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import ByteSink, ByteSource, CarrierReport, Deadline, Dispatch, FiniteInput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.models import Command, Input, Output, Script, Shell
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExitCode
from agentworks.operations import OperationOwner

from ._direct_input_support import DirectCarrier
from .test_execution_operation import RecordingCarrier
from .test_prepared_helper_stage import BOOTSTRAP, RUNTIME, TARGET

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux prepared DIRECT input")
PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
PAYLOAD = bytes(range(256)) * 300
SCRIPT = (
    "#" + "x" * 40_000 + "\n/usr/bin/python3 -c 'import sys,hashlib; "
    "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()); sys.exit(23)'\n"
)


@pytest.fixture
def owned(tmp_path):
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, TARGET.name), "direct")
    operation = ExecutionOperation(owner, TARGET, bootstrap=BOOTSTRAP)
    try:
        yield owner, operation, DirectCarrier(owner, tmp_path)
    finally:
        database.close()


def run(
    operation: ExecutionOperation,
    carrier: DirectCarrier,
    *,
    deadline: Deadline | None = None,
    sensitive: bool = False,
    capture_limit: int | None = 4096,
    source: ByteSource | None = None,
    stdout: ByteSink | None = None,
    stderr: ByteSink | None = None,
    plan: IdentityPlan = PLAN,
) -> OwnedInlineOutcome:
    return operation.run_inline(
        carrier,
        Script(SCRIPT, Shell.SH),
        plan=plan,
        runtime_selection=RUNTIME,
        stdin=PAYLOAD,
        deadline=Deadline.after(30) if deadline is None else deadline,
        sensitive=sensitive,
        capture_limit=capture_limit,
        source=source,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_large_source_and_binary_input_use_one_packed_application_and_original_budget(owned, interpreter):
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    owner, operation, carrier = owned
    carrier.interpreter = interpreter
    deadline = Deadline.after(30)
    routed: list[Deadline] = []
    operation._route_check = routed.append
    outcome = run(operation, carrier, deadline=deadline)
    result = reduce_owned_inline_result(outcome)
    assert result.application_state is ApplicationState.COMPLETED and result.status == ExitCode(23)
    assert result.stdout.data == hashlib.sha256(PAYLOAD).hexdigest().encode() + b"\n"
    assert result.owned_cleanup_confirmed and not outcome.requires_owner_retention
    assert carrier.activation_calls == 1
    assert len(carrier.deadlines) > 3 and all(value is deadline for value in carrier.deadlines)
    assert routed and all(value is deadline for value in routed)
    chunks = [request for request in carrier.requests if isinstance(request, FileStageChunkRequest)]
    assert chunks and all(len(request.data) <= 12_288 for request in chunks)
    assert carrier.data.startswith(PREPARED_PROGRAM.prefix)
    manifest = decode_prepared_manifest(bytes(carrier.data[len(PREPARED_PROGRAM.prefix) + REQUEST_HEADER_BYTES :]))
    assert manifest.source == SCRIPT.encode() and manifest.stdin == PAYLOAD and manifest.identity == PLAN.expected
    assert carrier.events == [
        ["admit", 0, 0, [0]],
        ["verified"],
        ["admit", 1001, 1002, [1002, 1003]],
        ["application_modules"],
    ]
    assert max(carrier.validated_sizes) <= 65_536
    assert not operation._prepared_stages and operation._inline_preparation is None
    assert owner._active_borrow is None
    assert PREPARED_HELPER_OBLIGATION_KIND not in [
        row.obligation_kind for row in owner.list_pending_lifecycle_obligations()
    ]


def test_ten_thousand_bytes_preserve_small_inline_delivery(owned, monkeypatch):
    _, operation, _ = owned
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    prepared = prepare_inline_candidate(
        Command(("/bin/cat",)), plan=PLAN, bootstrap=BOOTSTRAP, runtime_selection=RUNTIME, stdin=b"x" * 10_000
    )
    assert len(ProxmoxCarrier._request_body(prepared.invocation, prepared.io)) <= 65_536
    operation.run_inline(
        carrier,
        Command(("/bin/cat",)),
        plan=PLAN,
        runtime_selection=RUNTIME,
        deadline=Deadline.after(10),
        stdin=b"x" * 10_000,
    )
    assert carrier.calls == 1 and not operation._prepared_stages


@pytest.mark.parametrize("sensitive,limit", [(True, 4096), (False, None), (False, 17)])
def test_staged_capture_discard_and_suppression_keep_original_limit(owned, sensitive, limit):
    _, operation, carrier = owned
    result = reduce_owned_inline_result(run(operation, carrier, sensitive=sensitive, capture_limit=limit))
    assert result.status == ExitCode(23)
    assert len(result.stdout.data) <= (0 if sensitive or limit is None else limit)
    assert result.owned_cleanup_confirmed


def test_complete_carrier_refusal_selects_staging_even_with_small_manifest(owned):
    _, operation, carrier = owned
    carrier.force_stage = True
    outcome = operation.run_inline(
        carrier,
        Command(("/bin/true",)),
        plan=PLAN,
        runtime_selection=RUNTIME,
        deadline=Deadline.after(30),
        stdin=b"x" * 10_000,
    )
    assert reduce_owned_inline_result(outcome).status == ExitCode(0)
    assert carrier.activation_calls == 1 and carrier.requests
    assert max(carrier.validated_sizes) <= 65_536


@pytest.mark.parametrize("variant", ["guest", "prefix"])
def test_verified_loader_reenters_workload_admission_before_application_modules(owned, variant):
    _, operation, carrier = owned
    carrier.inner_guest_mismatch = variant == "guest"
    carrier.damage_prefix = variant == "prefix"
    outcome = run(operation, carrier)
    assert carrier.activation_calls == 1
    assert ["verified"] in carrier.events
    assert ["admit", 1001, 1002, [1002, 1003]] in carrier.events
    assert ["application_modules"] not in carrier.events
    assert reduce_owned_inline_result(outcome).status is None


def test_cleanup_failure_retains_status_output_and_separate_debt(owned):
    owner, operation, carrier = owned
    carrier.fail_cleanup = True
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.application_state is ApplicationState.COMPLETED
    assert result.stdout.data == hashlib.sha256(PAYLOAD).hexdigest().encode() + b"\n"
    assert result.failure is ExecutionFailure.CLEANUP and not result.owned_cleanup_confirmed and not result.ok
    assert outcome.scratch_cleanup_pending and outcome.requires_owner_retention
    assert not outcome.helper_requires_owner_retention
    assert owner._active_borrow is None and not operation.active_inline_calls
    assert len(operation._prepared_stages) == 1
    stage = operation._prepared_stages[0]._stage
    assert stage._activation_settled and not stage.cleaned
    rows = owner.list_pending_lifecycle_obligations()
    cleanup = next(row for row in rows if row.obligation_kind == PREPARED_HELPER_OBLIGATION_KIND)
    value = json.loads(cleanup.payload)
    assert set(value) == {"bootstrap", "runtime", "target", "token", "version"}
    assert SCRIPT.encode() not in cleanup.payload and PAYLOAD not in cleanup.payload
    carrier.fail_cleanup = False
    operation.finish_scratch_cleanup(Deadline.after(10))
    assert not operation._prepared_stages and stage.cleaned


def test_activation_preflight_refusal_has_no_scratch_or_owner_effect(owned):
    owner, operation, carrier = owned
    carrier.refuse_activation = True
    with pytest.raises(ValidationError):
        run(operation, carrier)
    assert not carrier.requests and carrier.activation_calls == 0
    assert not owner.list_pending_lifecycle_obligations() and not operation._prepared_stages
    assert operation._inline_preparation is None


def test_late_activation_validation_records_exact_nondispatch_and_removes_stage(owned):
    _, operation, carrier = owned
    carrier.before_return = lambda: setattr(carrier, "refuse_activation", True)
    with pytest.raises(ValidationError) as caught:
        run(operation, carrier)
    assert carrier.activation_calls == 0
    assert isinstance(caught.value.__cause__, InlineExecutionControlFact)
    assert not caught.value.__cause__.outcome.scratch_cleanup_pending
    assert any(isinstance(request, FileStageCleanupRequest) for request in carrier.requests)
    assert not operation._prepared_stages


@pytest.mark.parametrize("kind", ["unbounded", "host", "live-source", "live-sinks"])
def test_unsupported_staged_combinations_refuse_before_scratch(owned, kind):
    owner, operation, carrier = owned
    options: dict[str, object] = {}
    if kind == "unbounded":
        options["deadline"] = Deadline(None)
    elif kind == "host":
        operation._bootstrap = None
    elif kind == "live-source":

        class Source:
            def try_read(self, maximum):
                pytest.fail("unsupported staged source was consumed")

        options["source"] = Source()
    else:

        class Sink:
            def try_write(self, data):
                pytest.fail("unsupported staged sink was borrowed")

        options.update(stdout=Sink(), stderr=Sink())
    with pytest.raises(ValidationError):
        run(operation, carrier, **options)
    assert not carrier.requests and carrier.activation_calls == 0
    assert not owner.list_pending_lifecycle_obligations()


def test_preparation_reservation_refuses_reentrant_new_effects_between_chunks(owned, monkeypatch):
    owner, operation, carrier = owned
    operation._native_binding = NativeExecutionBinding(carrier, "private", RUNTIME)
    attempts = []
    original = PreparedHelperStage.append

    def append(stage, data):
        accepted = original(stage, data)
        assert owner._active_borrow is None and not operation.active_inline_calls

        def refused_inventory():
            pytest.fail("reentrant job admission reached owner inventory")

        options = dict(
            plan=PLAN,
            runtime_selection=RUNTIME,
            deadline=Deadline.after(10),
            input=Input.eof(),
            output=Output.capture(),
            env=None,
            cwd=None,
            sensitive=False,
        )
        with monkeypatch.context() as scope:
            scope.setattr(owner, "list_pending_lifecycle_obligations", refused_inventory)
            for action in (
                lambda: run(operation, carrier),
                operation.finish,
                lambda: operation._observe_workload_shell(carrier, PLAN, RUNTIME, Deadline.after(10)),
                lambda: operation.start_managed(carrier, Command(("/bin/true",)), **options),
                lambda: operation.start_resource_managed(carrier, Command(("/bin/true",)), **options),
            ):
                with pytest.raises(StateError):
                    action()
                attempts.append(action)
        return accepted

    monkeypatch.setattr(PreparedHelperStage, "append", append)
    result = reduce_owned_inline_result(run(operation, carrier))
    assert result.status == ExitCode(23) and attempts and carrier.activation_calls == 1


def test_unrepresentable_finite_budget_refuses_before_staging(owned):
    owner, operation, carrier = owned
    with pytest.raises(ValidationError):
        run(operation, carrier, deadline=Deadline.after(1e20))
    assert not carrier.requests and carrier.activation_calls == 0
    assert not owner.list_pending_lifecycle_obligations()


def test_explicit_root_workload_identity_is_bound_in_second_admission(owned):
    _, operation, carrier = owned
    root = BOOTSTRAP.root_entry
    carrier.target = (root.expected.euid, root.expected.egid, root.expected.groups)
    outcome = run(operation, carrier, plan=root)
    assert reduce_owned_inline_result(outcome).status == ExitCode(23)
    assert carrier.events == [
        ["admit", 0, 0, [0]],
        ["verified"],
        ["admit", 0, 0, [0]],
        ["application_modules"],
    ]


def test_cleanup_debt_preserves_earlier_output_failure(owned):
    _, operation, carrier = owned
    carrier.fail_cleanup = True
    outcome = run(operation, carrier, capture_limit=17)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and len(result.stdout.data) == 17
    assert result.failure is ExecutionFailure.OUTPUT_LIMIT and not result.owned_cleanup_confirmed
    assert outcome.scratch_cleanup_pending


def test_expired_original_budget_never_activates_or_renews_preparation(owned, monkeypatch):
    _, operation, carrier = owned
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    deadline = Deadline.after(10)
    routed: list[Deadline] = []
    operation._route_check = routed.append

    def expire_after_last_chunk():
        request = carrier.requests[-1]
        if (
            isinstance(request, FileStageChunkRequest)
            and request.offset + len(request.data) == request.reference._ownership._length
        ):
            clock[0] = 111.0

    carrier.before_return = expire_after_last_chunk
    with pytest.raises(StateError):
        run(operation, carrier, deadline=deadline)
    assert carrier.activation_calls == 0 and deadline.expired
    assert carrier.deadlines and all(value is deadline for value in carrier.deadlines)
    assert operation._scratch_cleanup_pending and len(operation._prepared_stages) == 1
    fresh = Deadline.after(10)
    operation.finish_scratch_cleanup(fresh)
    assert not operation._prepared_stages and routed[-1] is fresh


@pytest.mark.parametrize("phase", [FileStageOperation.BEGIN, FileStageOperation.CHUNK, FileStageOperation.CLEANUP])
def test_escaping_stage_or_cleanup_control_keeps_original_cause_and_exact_delivery(owned, phase):
    _, operation, carrier = owned
    cause = ValueError("original payload-free cause")
    control = KeyboardInterrupt()
    control.__cause__ = cause

    def interrupt():
        if carrier.requests[-1].operation is phase:
            raise control

    carrier.before_return = interrupt
    with pytest.raises(KeyboardInterrupt) as caught:
        run(operation, carrier)
    assert caught.value is control
    pending = [control]
    seen: list[BaseException] = []
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.append(item)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
    assert cause in seen
    assert isinstance(control.__cause__, InlineExecutionControlFact)
    assert control.__cause__.outcome.candidate is None and control.__cause__.outcome.scratch_cleanup_pending
    assert SCRIPT not in repr(control.__cause__) and str(PAYLOAD) not in repr(control.__cause__)
    assert sum(request.operation is phase for request in carrier.requests) == 1
    assert len(operation._prepared_stages) == 1
    stage = operation._prepared_stages[0]._stage
    assert stage.helper_uncertain and stage._active.operation.outstanding_attempt.local_delivery is carrier.custody
    assert carrier.activation_calls == (1 if phase is FileStageOperation.CLEANUP else 0)


class ClosureDelivery:
    def __init__(self, carrier: DirectCarrier) -> None:
        self.carrier = carrier
        self.activation = False
        self.closed = False
        self.observations = 0

    @property
    def features(self):
        return self.carrier.features

    @property
    def closure_proven(self):
        return not self.activation or self.closed

    def validate(self, invocation, *, io):
        self.carrier.validate(invocation, io=io)

    def execute(self, invocation, *, io, deadline, custody=None):
        assert isinstance(io.input, FiniteInput)
        self.activation = b'"sha256"' in io.input.data
        return self.carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

    def observe_closure(self, *, deadline, custody):
        assert custody.settled
        self.observations += 1
        self.closed = True
        return True


@pytest.mark.parametrize("escape", [False, True])
def test_native_activation_custody_outlives_application_wait_and_escaping_control(owned, escape):
    _, operation, carrier = owned
    deliveries = []

    def delivery(expectation):
        value = ClosureDelivery(carrier)
        deliveries.append(value)
        return value

    operation._native_binding = NativeExecutionBinding(carrier, "private", RUNTIME, _new_helper_delivery=delivery)
    original_cause = ValueError("original control cause")
    control = KeyboardInterrupt()
    control.__cause__ = original_cause
    if escape:
        carrier.activation_control = control
        with pytest.raises(KeyboardInterrupt) as caught:
            run(operation, carrier)
        assert caught.value is control
        assert isinstance(control.__cause__, InlineExecutionControlFact)
        assert control.__cause__.__cause__ is original_cause
        outcome = control.__cause__.outcome
        assert outcome.candidate is None
    else:
        outcome = run(operation, carrier)
        assert outcome.candidate is not None and outcome.candidate.observation is not None
        assert outcome.candidate.observation.trusted_terminal
    assert outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert len(operation.active_inline_calls) == 1
    active = operation.active_inline_calls[0]
    stage = active.input_stage
    assert active.input_delivery is carrier.custody and not stage._activation_settled
    assert active.prepared is None
    assert not any(isinstance(request, FileStageCleanupRequest) for request in carrier.requests)
    assert carrier.activation_calls == 1
    carrier.activation_control = None
    operation.observe_inline_cleanup(Deadline.after(10))
    assert stage._activation_settled and not operation.active_inline_calls
    operation.finish_scratch_cleanup(Deadline.after(10))
    assert stage.cleaned and not operation._prepared_stages
    assert carrier.activation_calls == 1
    assert sum(value.observations for value in deliveries) == 1


def test_positive_native_closure_still_requires_actual_local_delivery_settlement(owned):
    owner, operation, carrier = owned
    carrier.activation_unsettled = True

    def delivery(expectation):
        value = ClosureDelivery(carrier)
        value.closed = True
        return value

    operation._native_binding = NativeExecutionBinding(carrier, "private", RUNTIME, _new_helper_delivery=delivery)
    outcome = run(operation, carrier)
    assert outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    active = operation.active_inline_calls[0]
    stage = active.input_stage
    assert active.closure_delivery.closure_proven and not active.input_delivery.settled
    assert not stage._activation_settled and owner._active_borrow is active.borrow
    count = len(carrier.requests)
    with pytest.raises(StateError):
        operation.finish_scratch_cleanup(Deadline.after(10))
    assert len(carrier.requests) == count
    operation.observe_inline_cleanup(Deadline.after(10))
    assert stage._activation_settled and not operation.active_inline_calls
    operation.finish_scratch_cleanup(Deadline.after(10))
    assert stage.cleaned and carrier.activation_calls == 1
