"""Managed host binding, bounded presentation and one-use endpoint borrowing."""

from __future__ import annotations

import hashlib
import weakref
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._borrowed_io import PresentationCollector
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader, WireError, encode_frame
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline_control import StreamEnd, StreamName, StreamRetention, encode_stream_end
from agentworks.execution._managed_job_request import ManagedLiveIO
from agentworks.execution._managed_live_bundle import ROOT_PROGRAM
from agentworks.execution._managed_live_exchange import (
    ManagedLiveIssue,
    PreparedManagedLive,
    _ManagedLiveObserver,
    execute_managed_live,
    prepare_managed_live,
)
from agentworks.execution._managed_live_protocol import (
    ENVELOPE_HEADER_BYTES,
    ManagedLiveRequest,
    decode_request,
    encode_envelope,
)
from agentworks.execution._process import SinkWriteError
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import (
    ByteSink,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    LiveInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

from .test_managed_job_store import _launch

NONCE = "0123456789abcdef" * 2
GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1234)
ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
BOOTSTRAP = _NumericGuestBootstrap(ROOT, GUEST)
RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")


def _receipt() -> bytes:
    value = wire.decode_fact(_launch())
    value.update(owner={"kind": "operation", "owner_id": "c" * 32}, lifetime="operation")
    cast("dict[str, object]", value["target"])["boot_id"] = vm_guest_boot_id(GUEST)
    return wire.encode_fact(value)


@dataclass
class Source:
    data: bytes = b"application-input-canary"
    calls: int = 0
    closed: bool = False

    def try_read(self, limit: int) -> bytes:
        self.calls += 1
        result, self.data = self.data[:limit], self.data[limit:]
        return result

    def close(self) -> None:
        self.closed = True


@dataclass
class Sink:
    bound: int = 4096
    stalls: int = 0
    data: bytearray = field(default_factory=bytearray)
    calls: int = 0
    closed: bool = False

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        if self.stalls:
            self.stalls -= 1
            return None
        count = min(len(data), self.bound)
        self.data.extend(data[:count])
        return count

    def close(self) -> None:
        self.closed = True


def _prepare(*, input: bool = True, output: bool = True, sensitive: bool = False) -> PreparedManagedLive:
    return prepare_managed_live(
        expected_launch=_receipt(),
        live_io=ManagedLiveIO(NONCE, input, output),
        bootstrap=BOOTSTRAP,
        runtime_selection=RUNTIME,
        source=Source() if input else None,
        stdout=Sink() if output else None,
        stderr=Sink() if output else None,
        sensitive=sensitive,
    )


def _frames(stdout: bytes = b"", stderr: bytes = b"", *, output: bool = True) -> list[Frame]:
    frames = [Frame(0, FrameKind.LAUNCHING, _receipt())]
    if output:
        for offset in range(0, max(len(stdout), len(stderr)), 4096):
            for kind, data in ((FrameKind.STDOUT, stdout), (FrameKind.STDERR, stderr)):
                if data[offset : offset + 4096]:
                    frames.append(Frame(len(frames), kind, data[offset : offset + 4096]))
        for stream, data in ((StreamName.STDOUT, stdout), (StreamName.STDERR, stderr)):
            end = StreamEnd(stream, len(data), hashlib.sha256(data).hexdigest(), True, False, StreamRetention.DELIVERED)
            frames.append(Frame(len(frames), FrameKind.STREAM_END, encode_stream_end(end)))
    frames.append(Frame(len(frames), FrameKind.FINISHED, b"{}"))
    return frames


def _wire(frames: list[Frame], nonce: str = NONCE) -> bytes:
    return b"".join(encode_frame(nonce, replace(frame, sequence=index)) for index, frame in enumerate(frames))


def _feed(sink: ByteSink, data: bytes) -> None:
    pending = memoryview(data)
    for _ in range(len(data) + 100):
        if not pending:
            return
        count = sink.try_write(pending)
        if count is not None:
            pending = pending[count:]
    pytest.fail("Synthetic presentation did not make bounded progress")


def _observer(
    *, output: bool = True, stdout: ByteSink | None = None
) -> tuple[_ManagedLiveObserver, FrameReader, PresentationCollector]:
    observer = _ManagedLiveObserver(
        _receipt(),
        ManagedLiveIO(NONCE, True, output),
        stdout=(stdout if stdout is not None else Sink()) if output else None,
        stderr=Sink() if output else None,
    )
    reader = FrameReader(NONCE, observer.accept)
    return observer, reader, PresentationCollector(reader, observer)


class Carrier:
    def __init__(
        self, frames: bytes, *, state: str = "ready", complete: bool = True, error: BaseException | None = None
    ) -> None:
        self.frames, self.state, self.complete, self.error = frames, state, complete, error
        self.calls = 0
        self.validations = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures(live_stdio=True)

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1
        assert isinstance(io.output, SinkOutput) and io.output.require_live

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.output, SinkOutput)
        token = "-" if self.state == "missing" else "0"
        _feed(io.output.stdout, f"AGW_RUNTIME_1:{NONCE}:{self.state}:{token}\n".encode() + self.frames)
        assert io.output.stderr.try_write(memoryview(b"private-diagnostic-canary")) == 25
        if self.error is not None:
            raise self.error
        return CarrierReport(
            dispatch=Dispatch.SENT,
            completion=ExitStatus(code=255),
            local_status=7,
            stdout=CapturedOutput(complete=self.complete, retention=Retention.DELIVERED),
            stderr=CapturedOutput(complete=True, retention=Retention.DELIVERED),
            failure=None if self.complete else Failure.OUTPUT,
        )


@pytest.mark.parametrize(("input", "output"), [(True, True), (True, False), (False, True)])
def test_preparation_is_passive_and_binds_only_bootstrap_root(input: bool, output: bool) -> None:
    source, stdout, stderr = Source(), Sink(), Sink()
    live = ManagedLiveIO(NONCE, input, output)
    prepared = prepare_managed_live(
        expected_launch=_receipt(),
        live_io=live,
        bootstrap=BOOTSTRAP,
        runtime_selection=RUNTIME,
        source=source if input else None,
        stdout=stdout if output else None,
        stderr=stderr if output else None,
        sensitive=True,
    )
    assert source.calls == stdout.calls == stderr.calls == 0
    assert isinstance(prepared.io.input, FiniteInput | LiveInput)
    assert prepared.io.sensitive and prepared.io.input.sensitive
    assert prepared.closure_expectation.nonce == NONCE
    assert prepared.closure_expectation.guest == GUEST
    assert prepared.closure_expectation.candidates == ("/usr/bin/python3",)
    request = ManagedLiveRequest(live, ROOT.expected, GUEST, _receipt())
    prefix = ROOT_PROGRAM.prefix + encode_envelope(request)
    if isinstance(prepared.io.input, LiveInput):
        consumed = b""
        while len(consumed) < len(prefix):
            chunk = prepared.io.input.source.try_read(37)
            assert chunk is not None
            consumed += chunk
        assert consumed == prefix and source.calls == 0
        assert prepared.io.input.source.try_read(4096) == b"application-input-canary"
    else:
        assert isinstance(prepared.io.input, FiniteInput)
        assert prepared.io.input.data == prefix
        envelope = prepared.io.input.data[len(ROOT_PROGRAM.prefix) :]
        assert decode_request(envelope[ENVELOPE_HEADER_BYTES:]) == request
    carrier = Carrier(_wire(_frames(output=output)))
    carrier.validate(prepared.invocation, io=prepared.io)
    prepared.clear()
    assert not source.closed and not stdout.closed and not stderr.closed


@pytest.mark.parametrize(
    "change",
    [
        {"source": None},
        {"stdout": None},
        {"stderr": None},
        {"live_io": ManagedLiveIO(NONCE, False, True)},
        {"live_io": ManagedLiveIO(NONCE, True, False)},
        {"live_io": None},
        {"bootstrap": None},
        {"sensitive": 1},
        {"expected_launch": b"canary-invalid"},
        {"bootstrap": _NumericGuestBootstrap(ROOT, replace(GUEST, init_start_ticks=1235))},
        {"runtime_selection": RuntimeSelection(RuntimeTargetOS.DARWIN)},
    ],
)
def test_invalid_preparation_refuses_without_endpoint_touch(change: dict[str, object]) -> None:
    source, stdout, stderr = Source(), Sink(), Sink()
    options = dict(
        expected_launch=_receipt(),
        live_io=ManagedLiveIO(NONCE, True, True),
        bootstrap=BOOTSTRAP,
        runtime_selection=RUNTIME,
        source=source,
        stdout=stdout,
        stderr=stderr,
    )
    options.update(change)
    with pytest.raises(ValidationError):
        prepare_managed_live(**options)
    assert source.calls == stdout.calls == stderr.calls == 0


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [(b"", b""), (b"\x00\xff\x80", b"binary\x00"), (bytes(range(256)) * 512, b"error\x00\xff" * 6000)],
)
def test_binary_interleaved_presentation_has_no_retained_bytes(stdout: bytes, stderr: bytes) -> None:
    out, err = Sink(bound=3, stalls=5), Sink(bound=7, stalls=2)
    observer = _ManagedLiveObserver(_receipt(), ManagedLiveIO(NONCE, True, True), stdout=out, stderr=err)
    reader = FrameReader(NONCE, observer.accept)
    _feed(PresentationCollector(reader, observer), _wire(_frames(stdout, stderr)))
    reader.finish()
    result = observer.finish(reader.error, carrier_stdout_complete=True)
    assert result.error is None and result.bound and result.trusted_terminal
    assert out.data == stdout and err.data == stderr
    assert result.stdout == StreamEnd(
        StreamName.STDOUT, len(stdout), hashlib.sha256(stdout).hexdigest(), True, False, StreamRetention.DELIVERED
    )
    assert observer._pending is None
    assert not hasattr(result.stdout, "data")
    observer.release()


def test_codec_callback_does_no_external_io_and_stall_blocks_next_frame() -> None:
    sink = Sink(stalls=10)
    observer, reader, collector = _observer(stdout=sink)
    _feed(collector, _wire(_frames()[:1]))
    frame = Frame(1, FrameKind.STDOUT, b"x" * 4096)
    encoded = encode_frame(NONCE, frame)
    assert reader.try_write(memoryview(encoded)) == len(encoded)
    assert sink.calls == 0
    assert observer._pending is not None
    assert len(observer._pending[1]) == 4096
    next_frame = encode_frame(NONCE, Frame(2, FrameKind.STDERR, b"later"))
    assert collector.try_write(memoryview(next_frame)) is None
    assert len(observer._pending[1]) == 4096
    assert observer.finish(None, carrier_stdout_complete=True).error is ManagedLiveIssue.STREAM
    sink.stalls = 0
    _feed(collector, next_frame)
    assert sink.data == b"x" * 4096
    observer.release()


@pytest.mark.parametrize(
    "mutation",
    [
        "data-first",
        "end-first",
        "wrong-launch",
        "duplicate-launch",
        "waited",
        "started",
        "failed",
        "duplicate-end",
        "after-end",
        "bad-hash",
        "bad-count",
        "discarded",
        "incomplete",
        "truncated",
        "bad-control",
        "missing-end",
        "missing-terminal",
        "post-terminal",
        "partial",
        "wrong-nonce",
    ],
)
def test_managed_grammar_refuses_incomplete_or_wrong_evidence(mutation: str) -> None:
    frames = _frames(b"payload")
    if mutation == "data-first":
        frames.pop(0)
    elif mutation == "end-first":
        frames = [frames[2], *frames]
    elif mutation == "wrong-launch":
        frames[0] = Frame(0, FrameKind.LAUNCHING, b"{}")
    elif mutation == "duplicate-launch":
        frames.insert(1, frames[0])
    elif mutation in ("waited", "started", "failed"):
        frames.insert(1, Frame(1, FrameKind(mutation.upper()), b"{}"))
    elif mutation == "duplicate-end":
        frames.insert(3, frames[2])
    elif mutation == "after-end":
        frames.insert(3, frames[1])
    elif mutation in ("bad-hash", "bad-count", "discarded", "incomplete", "truncated"):
        data = b"payload"
        end = StreamEnd(
            StreamName.STDOUT, len(data), hashlib.sha256(data).hexdigest(), True, False, StreamRetention.DELIVERED
        )
        if mutation == "bad-hash":
            end = replace(end, sha256="0" * 64)
        elif mutation == "bad-count":
            end = replace(end, count=0)
        elif mutation == "discarded":
            end = replace(end, count=0, sha256=hashlib.sha256(b"").hexdigest(), retention=StreamRetention.DISCARDED)
        elif mutation == "incomplete":
            end = replace(end, complete=False)
        else:
            end = replace(end, complete=False, truncated=True)
        frames[2] = Frame(2, FrameKind.STREAM_END, encode_stream_end(end))
    elif mutation == "bad-control":
        frames[-1] = Frame(4, FrameKind.FINISHED, b'{"extra":true}')
    elif mutation == "missing-end":
        frames.pop(3)
    elif mutation == "missing-terminal":
        frames.pop()
    elif mutation == "post-terminal":
        frames.append(Frame(5, FrameKind.FINISHED, b"{}"))
    data = _wire(frames, "b" * 32 if mutation == "wrong-nonce" else NONCE)
    if mutation == "partial":
        data = data[:-1]
    observer, reader, collector = _observer()
    _feed(collector, data)
    reader.finish()
    result = observer.finish(reader.error, carrier_stdout_complete=True)
    assert result.error is not None and not result.trusted_terminal
    if mutation == "partial":
        assert result.error is WireError.TRUNCATED
    observer.release()


@pytest.mark.parametrize("kind", [FrameKind.STDOUT, FrameKind.STDERR, FrameKind.STREAM_END])
def test_input_only_rejects_application_output(kind: FrameKind) -> None:
    observer, reader, collector = _observer(output=False)
    frames = _frames(output=False)
    frames.insert(1, Frame(1, kind, b"{}"))
    _feed(collector, _wire(frames))
    reader.finish()
    assert observer.finish(reader.error, carrier_stdout_complete=True).error is not None
    observer.release()


@pytest.mark.parametrize("complete", [False, True])
def test_execute_reports_relay_evidence_separately_from_carrier_exit(complete: bool) -> None:
    prepared = _prepare(input=True, output=False, sensitive=True)
    carrier = Carrier(_wire(_frames(output=False)), complete=complete)
    result = execute_managed_live(carrier, prepared, deadline=Deadline.after(3))
    assert carrier.calls == 1
    assert result.carrier_completion == ExitStatus(code=255) and result.carrier_local_status == 7
    assert result.observation is not None and result.observation.bound
    assert result.observation.trusted_terminal is complete
    assert result.observation.stdout is result.observation.stderr is None
    assert result.carrier_failure is (None if complete else Failure.OUTPUT)
    assert result.closure_expectation == prepared.closure_expectation
    assert prepared.io == CarrierIO()
    with pytest.raises(ValidationError):
        execute_managed_live(carrier, prepared, deadline=Deadline.after(3))
    assert carrier.calls == 1


@pytest.mark.parametrize("state", ["missing", "unsupported_version", "unusable"])
def test_runtime_refusal_never_admits_application_frames(state: str) -> None:
    prepared = _prepare()
    result = execute_managed_live(Carrier(_wire(_frames()), state=state), prepared, deadline=Deadline.after(3))
    assert result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
    assert result.observation is None
    assert prepared.io == CarrierIO()


@pytest.mark.parametrize("error", [RuntimeError("private-exception-canary"), KeyboardInterrupt(), SystemExit(71)])
def test_original_carrier_exceptions_preserve_identity_and_clear_endpoints(error: BaseException) -> None:
    prepared = _prepare()
    with pytest.raises(type(error)) as caught:
        execute_managed_live(Carrier(_wire(_frames()), error=error), prepared, deadline=Deadline.after(3))
    assert caught.value is error
    assert prepared.io == CarrierIO() and prepared._observer._sinks == {} and prepared._observer._pending is None


def test_stream_count_cannot_overflow() -> None:
    observer, reader, collector = _observer()
    _feed(collector, _wire(_frames()[:1]))
    observer._counts[StreamName.STDOUT] = 2**63 - 1
    _feed(collector, encode_frame(NONCE, Frame(1, FrameKind.STDOUT, b"x")))
    assert observer.finish(reader.error, carrier_stdout_complete=True).error is ManagedLiveIssue.STREAM
    observer.release()


@pytest.mark.parametrize("control", [None, KeyboardInterrupt(), SystemExit(73)])
def test_sink_faults_are_outside_decoder_and_controls_keep_identity(control: BaseException | None) -> None:
    class Fault:
        def try_write(self, data: memoryview) -> int:
            raise control if control is not None else RuntimeError("private-sink-canary")

    prepared = prepare_managed_live(
        expected_launch=_receipt(),
        live_io=ManagedLiveIO(NONCE, False, True),
        bootstrap=BOOTSTRAP,
        runtime_selection=RUNTIME,
        stdout=Fault(),
        stderr=Sink(),
        sensitive=True,
    )
    with pytest.raises(type(control) if control is not None else SinkWriteError) as caught:
        execute_managed_live(Carrier(_wire(_frames(b"x"))), prepared, deadline=Deadline.after(3))
    if control is not None:
        assert caught.value is control
    assert prepared._reader.error is None and prepared._observer._sinks == {} and prepared._observer._pending is None


@pytest.mark.parametrize("outcome", ["return", "exception", "refusal"])
def test_clearing_releases_endpoint_weakrefs_without_closing_them(outcome: str) -> None:
    source, stdout, stderr = Source(), Sink(), Sink()
    refs = [weakref.ref(endpoint) for endpoint in (source, stdout, stderr)]
    prepared = prepare_managed_live(
        expected_launch=_receipt(),
        live_io=ManagedLiveIO(NONCE, True, True),
        bootstrap=BOOTSTRAP,
        runtime_selection=RUNTIME,
        source=source,
        stdout=stdout,
        stderr=stderr,
        sensitive=True,
    )
    held_io = prepared.io
    if outcome == "refusal":

        class RejectCarrier(Carrier):
            def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
                raise ValidationError("Unsupported live carrier")

        with pytest.raises(ValidationError):
            try:
                RejectCarrier(b"").validate(prepared.invocation, io=prepared.io)
            finally:
                prepared.clear()
        assert source.calls == stdout.calls == stderr.calls == 0
    elif outcome == "exception":
        with pytest.raises(KeyboardInterrupt):
            execute_managed_live(
                Carrier(_wire(_frames()), error=KeyboardInterrupt()), prepared, deadline=Deadline.after(3)
            )
    else:
        result = execute_managed_live(Carrier(_wire(_frames(b"payload-canary"))), prepared, deadline=Deadline.after(3))
        assert "payload-canary" not in repr(result)
    assert not source.closed and not stdout.closed and not stderr.closed
    del source, stdout, stderr
    assert all(reference() is None for reference in refs)
    assert isinstance(held_io.input, LiveInput) and held_io.input.source.try_read(4096) == b""
    assert prepared._observer._pending is None
    with pytest.raises(ValidationError):
        prepared.claim()
