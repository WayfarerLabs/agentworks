"""Private managed relay admission and helper custody under real local fencing."""

from __future__ import annotations

import hashlib
import weakref
from dataclasses import replace

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as core
from agentworks.execution._evidence_wire import Frame, FrameKind, encode_frame
from agentworks.execution._inline_control import StreamEnd, StreamName, StreamRetention, encode_stream_end
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_request import ManagedLiveIO
from agentworks.execution._managed_live_bundle import ROOT_PROGRAM
from agentworks.execution._managed_live_exchange import PreparedManagedLive, prepare_managed_live
from agentworks.execution._managed_observe_access import ManagedObserveControlFact
from agentworks.execution._managed_runs import ManagedRunRepository
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    LiveInput,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command, Input, JobRef, Output, Script, Shell

from . import test_managed_execution_access as managed
from .test_managed_execution_access import bound as bound
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_live_exchange import NONCE, Sink, Source, _feed
from .test_managed_start_operation import GUEST

pytestmark = pytest.mark.windows


def is_live(io: CarrierIO) -> bool:
    return isinstance(io.input, LiveInput) or (
        isinstance(io.input, FiniteInput) and io.input.data.startswith(ROOT_PROGRAM.prefix)
    )


class Delivery(managed.Delivery):
    def __init__(self) -> None:
        super().__init__()
        self.live = True
        self.refuse = False
        self.relay_calls = 0
        self.relay_validations = 0
        self.deadlines: list[Deadline] = []
        self.launch = b""
        self.metadata = ManagedLiveIO(NONCE, True, True)
        self.control: BaseException | None = None
        self.completion = ExitStatus(0)
        self.closure_proven = False

    @property
    def features(self):
        return ChannelFeatures(live_stdio=self.live)

    def validate(self, invocation, *, io):
        if is_live(io):
            self.relay_validations += 1
            assert isinstance(io.output, SinkOutput) and io.output.require_live
            if self.refuse:
                raise ValidationError("Synthetic passive refusal")
        else:
            super().validate(invocation, io=io)

    def execute(self, invocation, *, io, deadline, custody):
        if not is_live(io):
            return super().execute(invocation, io=io, deadline=deadline, custody=custody)
        self.relay_calls += 1
        self.deadlines.append(deadline)
        assert isinstance(io.output, SinkOutput)
        if self.control is not None:
            raise self.control
        frames = [Frame(0, FrameKind.LAUNCHING, self.launch)]
        if self.metadata.output:
            for stream in (StreamName.STDOUT, StreamName.STDERR):
                end = StreamEnd(stream, 0, hashlib.sha256(b"").hexdigest(), True, False, StreamRetention.DELIVERED)
                frames.append(Frame(len(frames), FrameKind.STREAM_END, encode_stream_end(end)))
        frames.append(Frame(len(frames), FrameKind.FINISHED, b"{}"))
        payload = f"AGW_RUNTIME_1:{NONCE}:ready:0\n".encode()
        payload += b"".join(encode_frame(NONCE, frame) for frame in frames)
        _feed(io.output.stdout, payload)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, self.completion, 0, output, output)

    def observe_closure(self, *, deadline, custody):
        return self.closure_proven


@pytest.fixture
def view(bound):
    database, owner, receipt = bound
    main, keeper = Delivery(), managed.Delivery()
    binding = NativeExecutionBinding(main, "root", RUNTIME, _new_managed_delivery=lambda: keeper)
    operation = core.ExecutionOperation(
        owner,
        receipt.spec.target,
        bootstrap=_NumericGuestBootstrap(PLAN, GUEST),
        managed_repository=ManagedRunRepository(database),
        native_binding=binding,
    )
    try:
        yield database, owner, operation, main, keeper
    finally:
        for run in operation.managed_runs:
            assert run.keeper.drain(Deadline.after(2)).drained


def start(view, *, metadata=None, source=None, stdout=None, stderr=None, invocation=None, deadline=None) -> JobRef:
    _, _, operation, main, _ = view
    if metadata is None:
        metadata = ManagedLiveIO(NONCE, True, True)
    source = source if source is not None else Source()
    stdout = stdout if stdout is not None else Sink()
    stderr = stderr if stderr is not None else Sink()
    reference: JobRef = operation.start_managed(
        main,
        invocation if invocation is not None else Command(["/bin/true"]),
        plan=PLAN,
        runtime_selection=RUNTIME,
        deadline=deadline if deadline is not None else Deadline.after(5),
        input=Input.live(source) if metadata.input else Input.bytes(b"finite-input"),
        output=Output.live(stdout, stderr) if metadata.output else Output.capture(),
        env=None,
        cwd=None,
        sensitive=True,
        live_io=metadata,
    )
    main.launch = encode_managed_job_fact(operation.managed_runs[-1].receipt)
    main.metadata = metadata
    return reference


@pytest.mark.parametrize("live", [False, True])
def test_refusal_precedes_shell_reservation_keeper_and_endpoints(view, monkeypatch, live):
    _, owner, operation, main, keeper = view
    main.live, main.refuse = live, True
    source, stdout, stderr = Source(), Sink(), Sink()
    calls = []
    monkeypatch.setattr(operation, "_observe_workload_shell", lambda *args: calls.append(True))
    with pytest.raises(ValidationError):
        start(view, source=source, stdout=stdout, stderr=stderr, invocation=Script("true", Shell.USER_DEFAULT))
    assert not calls and operation.managed_runs == ()
    assert owner.list_pending_lifecycle_obligations() == ()
    assert main.start.calls == keeper.clock.calls == keeper.start.calls == 0
    assert source.calls == stdout.calls == stderr.calls == 0
    assert not source.closed and not stdout.closed and not stderr.closed


@pytest.mark.parametrize(("input", "output"), [(True, True), (True, False), (False, True)])
def test_one_relay_preserves_deadline_and_directions_without_retaining_endpoints(view, monkeypatch, input, output):
    _, _, operation, main, keeper = view
    deadline = Deadline.after(5)
    source, stdout, stderr = Source(), Sink(), Sink()
    metadata = ManagedLiveIO(NONCE, input, output)
    preparations: list[PreparedManagedLive] = []

    def prepare(**kwargs):
        assert kwargs["deadline"] is deadline
        prepared = prepare_managed_live(**kwargs)
        preparations.append(prepared)
        return prepared

    monkeypatch.setattr(core, "prepare_managed_live", prepare)
    reference = start(view, metadata=metadata, source=source, stdout=stdout, stderr=stderr, deadline=deadline)
    run = operation.managed_runs[0]
    assert run.live_io == metadata and not run.live_relay_attempted and run.acknowledged
    assert preparations[0].io == CarrierIO()
    assert source.calls == stdout.calls == stderr.calls == 0
    candidate, custody = operation.relay_managed(
        reference,
        main,
        deadline=deadline,
        source=source if input else None,
        stdout=stdout if output else None,
        stderr=stderr if output else None,
        sensitive=True,
    )
    assert candidate.observation is not None and candidate.observation.trusted_terminal
    assert not custody.requires_owner_retention and custody.candidate is None
    assert not operation.active_inline_calls and not operation.unfinished_inline_executions
    assert main.deadlines == [deadline] and run.live_relay_attempted
    assert main.start.calls == 1 and keeper.stop.calls == 0 and not run.keeper._stop.is_set()
    assert all(prepared.io == CarrierIO() for prepared in preparations)
    with pytest.raises(StateError):
        operation.relay_managed(reference, main, deadline=deadline)
    refs = [weakref.ref(endpoint) for endpoint in (source, stdout, stderr)]
    del source, stdout, stderr
    assert all(ref() is None for ref in refs)


def test_default_shell_revalidates_resolved_launch_before_reservation(view, monkeypatch):
    _, _, operation, main, _ = view
    deadline = Deadline.after(5)
    launches = []
    preparations: list[PreparedManagedLive] = []

    def prepare(**kwargs):
        assert kwargs["deadline"] is deadline
        launches.append(kwargs["expected_launch"])
        prepared = prepare_managed_live(**kwargs)
        preparations.append(prepared)
        return prepared

    def shell(*args):
        assert preparations[0].io == CarrierIO()
        assert not operation.managed_runs and main.relay_validations == 1
        return "/bin/bash"

    monkeypatch.setattr(core, "prepare_managed_live", prepare)
    monkeypatch.setattr(operation, "_observe_workload_shell", shell)
    start(view, invocation=Script("true", Shell.USER_DEFAULT), deadline=deadline)
    assert len(launches) == 2 and launches[0] != launches[1] == main.launch
    assert all(prepared.io == CarrierIO() for prepared in preparations)


@pytest.mark.parametrize(
    "mismatch", ["reference", "carrier", "binding", "cleanup", "disposed", "disposing", "directions", "ack"]
)
def test_exact_run_and_binding_precede_relay_admission(view, mismatch):
    _, _, operation, main, _ = view
    reference = start(view)
    source: Source | None = Source()
    stdout, stderr = Sink(), Sink()
    carrier = main
    if mismatch == "reference":
        reference = JobRef("f" * 32)
    elif mismatch == "carrier":
        carrier = Delivery()
    elif mismatch == "binding":
        operation._bootstrap = replace(operation._bootstrap, guest=replace(GUEST, init_start_ticks=1235))
    elif mismatch == "cleanup":
        operation.managed_runs[0].cleanup_complete = True
    elif mismatch == "disposed":
        operation.managed_runs[0].disposal_confirmed = True
    elif mismatch == "disposing":
        operation.managed_runs[0].disposal_attempted = True
    elif mismatch == "ack":
        operation.managed_runs[0].start_outcome = None
    elif mismatch == "directions":
        source = None
    with pytest.raises((ValidationError, StateError)):
        operation.relay_managed(
            reference, carrier, deadline=Deadline.after(3), source=source, stdout=stdout, stderr=stderr
        )
    assert not operation.managed_runs[0].live_relay_attempted and main.relay_calls == 0
    assert stdout.calls == stderr.calls == 0


def test_refused_admitted_attempt_is_not_replayed_and_preparation_clears(view, monkeypatch):
    _, _, operation, main, _ = view
    reference = start(view)
    preparations: list[PreparedManagedLive] = []

    def prepare(**kwargs):
        prepared = prepare_managed_live(**kwargs)
        preparations.append(prepared)
        return prepared

    monkeypatch.setattr(core, "prepare_managed_live", prepare)
    main.refuse = True
    with pytest.raises(ValidationError):
        operation.relay_managed(
            reference, main, deadline=Deadline.after(3), source=Source(), stdout=Sink(), stderr=Sink()
        )
    assert preparations[0].io == CarrierIO()
    assert operation.managed_runs[0].live_relay_attempted and main.relay_calls == 0
    with pytest.raises(StateError):
        operation.relay_managed(reference, main, deadline=Deadline.after(3))


@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit(7)])
def test_original_control_and_safe_custody_release_endpoints(view, monkeypatch, control):
    _, _, operation, main, _ = view
    reference = start(view)
    main.control = control
    cause = ValueError("synthetic original cause")
    control.__cause__ = cause
    source, stdout, stderr = Source(), Sink(), Sink()
    preparations: list[PreparedManagedLive] = []

    def prepare(**kwargs):
        prepared = prepare_managed_live(**kwargs)
        preparations.append(prepared)
        return prepared

    monkeypatch.setattr(core, "prepare_managed_live", prepare)
    with pytest.raises(type(control)) as caught:
        operation.relay_managed(
            reference, main, deadline=Deadline.after(3), source=source, stdout=stdout, stderr=stderr
        )
    assert caught.value is control and isinstance(control.__cause__, ManagedObserveControlFact)
    assert control.__cause__.__cause__ is cause
    assert control.__cause__.outcome.requires_owner_retention
    assert operation.unfinished_inline_executions[0].outcome.candidate is None
    assert not source.closed and not stdout.closed and not stderr.closed
    assert not operation.active_inline_calls
    assert preparations[0].io == CarrierIO() and not preparations[0]._observer._sinks
    assert operation.managed_runs[0]._prepared is None


@pytest.mark.parametrize("closed", [False, True])
def test_optional_native_closure_controls_settlement_and_retains_only_safe_state(view, closed):
    _, _, operation, main, _ = view
    reference = start(view)
    delivery = Delivery()
    delivery.launch, delivery.metadata = main.launch, main.metadata
    delivery.closure_proven = closed
    binding = operation._native_binding
    operation._native_binding = replace(binding, _new_helper_delivery=lambda expectation: delivery)
    source, stdout, stderr = Source(), Sink(), Sink()
    candidate, custody = operation.relay_managed(
        reference, main, deadline=Deadline.after(3), source=source, stdout=stdout, stderr=stderr
    )
    assert candidate.carrier_completion == ExitStatus(0)
    assert custody.requires_owner_retention is not closed
    assert main.relay_calls == 0 and delivery.relay_calls == 1
    if not closed:
        (active,) = operation.active_inline_calls
        assert active.prepared is None and active.candidate is None
        assert active.closure_expectation.nonce == NONCE and active.borrow.has_outstanding_attempt
    else:
        assert not operation.active_inline_calls
    refs = [weakref.ref(endpoint) for endpoint in (source, stdout, stderr)]
    del source, stdout, stderr
    assert all(ref() is None for ref in refs)
    if not closed:
        delivery.closure_proven = True
        operation.observe_inline_cleanup(Deadline.after(3))
        operation.retry_inline_bookkeeping()
        assert not operation.active_inline_calls
        assert main.relay_calls == 0 and delivery.relay_calls == 1


def test_route_fence_uses_original_deadline_and_failed_route_does_not_relay(view, monkeypatch):
    _, _, operation, main, keeper = view
    deadline = Deadline.after(5)
    routed: list[Deadline] = []
    events = []

    def check(actual):
        routed.append(actual)
        events.append("route")

    original_clock, original_start = keeper.clock.execute, main.start.execute

    def execute_clock(*args, **kwargs):
        if keeper.clock.calls == 0:
            assert events[-1] == "route" and kwargs["deadline"] is deadline
            events.append("clock")
        return original_clock(*args, **kwargs)

    def execute_start(*args, **kwargs):
        assert events[-1] == "route" and kwargs["deadline"] is deadline
        events.append("start")
        return original_start(*args, **kwargs)

    monkeypatch.setattr(keeper.clock, "execute", execute_clock)
    monkeypatch.setattr(main.start, "execute", execute_start)
    operation._route_check = check
    reference = start(view, deadline=deadline)
    assert events[0] == "route" and "clock" in events and events[-1] == "start"
    assert routed and all(actual is deadline for actual in routed)
    control = KeyboardInterrupt()

    def route(actual):
        assert actual is deadline
        raise control

    operation._route_check = route
    with pytest.raises(KeyboardInterrupt) as caught:
        operation.relay_managed(reference, main, deadline=deadline, source=Source(), stdout=Sink(), stderr=Sink())
    assert caught.value is control and main.relay_calls == 0
    assert operation.managed_runs[0].live_relay_attempted
    assert operation.managed_runs[0]._prepared is None
    operation.retry_inline_bookkeeping()
    assert not operation.active_inline_calls


def test_finite_run_cannot_gain_live_metadata_through_relay(view):
    _, _, operation, main, _ = view
    reference = operation.start_managed(
        main,
        Command(["/bin/true"]),
        plan=PLAN,
        runtime_selection=RUNTIME,
        deadline=Deadline.after(5),
        input=Input.bytes(b"finite"),
        output=Output.discard(),
        env=None,
        cwd=None,
        sensitive=False,
    )
    with pytest.raises(StateError):
        operation.relay_managed(
            reference, main, deadline=Deadline.after(3), source=Source(), stdout=Sink(), stderr=Sink()
        )
    assert operation.managed_runs[0].live_io is None and main.relay_calls == 0
