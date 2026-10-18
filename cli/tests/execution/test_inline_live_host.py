"""Bounded decoded live application I/O and public endpoint admission."""

from __future__ import annotations

import hashlib
import shlex
import sys
import weakref
from dataclasses import dataclass, field

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader, encode_frame
from agentworks.execution._execution_operation import OwnedInlineOutcome
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._inline import (
    _LiveCollector,
    _RequestSource,
    execute_inline_candidate,
    prepare_inline_candidate,
)
from agentworks.execution._inline_control import (
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    WaitKind,
    empty_body,
    encode_stream_end,
    encode_wait,
)
from agentworks.execution._inline_observer import InlineObserver, ObservationError
from agentworks.execution._inline_request import REQUEST_HEADER_BYTES, OutputMode, decode_manifest
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, LiveInput, Retention, SinkOutput
from agentworks.execution.models import Command, Input, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExitCode
from tests.execution.test_execution_access import Bound
from tests.execution.test_inline_helper import LocalCarrier, _run

pytest_plugins = ("tests.execution.test_execution_access", "tests.execution.test_inline_helper")

NONCE = "0123456789abcdef0123456789abcdef"


@dataclass
class Sink:
    bound: int = 4096
    stalled: bool = False
    data: bytearray = field(default_factory=bytearray)
    calls: int = 0

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        if self.stalled:
            return None
        count = min(self.bound, len(data))
        self.data.extend(data[:count])
        return count


@dataclass
class Source:
    data: bytes
    calls: int = 0

    def try_read(self, limit: int) -> bytes | None:
        self.calls += 1
        chunk = self.data[:limit]
        self.data = self.data[len(chunk) :]
        return chunk


def test_public_endpoints_validate_passively_and_hide_values() -> None:
    class Descriptor:
        @property
        def try_read(self) -> object:
            pytest.fail("Endpoint validation executed a descriptor")

    with pytest.raises(ValidationError):
        Input.live(Descriptor())
    for invalid in (None, object(), 4, "canary"):
        with pytest.raises(ValidationError):
            Input.live(invalid)
        with pytest.raises(ValidationError):
            Output.live(invalid, Sink())
        with pytest.raises(ValidationError):
            Output.live(Sink(), invalid)
    source = Source(b"endpoint-canary")
    sink = Sink()
    assert Input.live(source, sensitive=True).is_sensitive
    assert source.calls == sink.calls == 0
    assert "endpoint-canary" not in repr(Input.live(source))
    assert "data=" not in repr(Output.live(sink, sink))


def test_request_prefix_does_not_read_source_until_envelope_is_complete() -> None:
    source = Source(b"application")
    request = _RequestSource(b"envelope", source)
    assert request.try_read(3) == b"env"
    assert request.try_read(100) == b"elope"
    assert source.calls == 0
    assert request.try_read(100) == b"application"
    request.release()
    assert request.try_read(100) == b""
    assert source.calls == 1


def test_one_frame_per_write_and_pending_chunk_block_further_decoding() -> None:
    stdout, stderr = Sink(bound=2), Sink()
    observer = InlineObserver(OutputMode.DELIVER, 0, stdout=stdout, stderr=stderr)
    reader = FrameReader(NONCE, observer.accept)
    collector = _LiveCollector(reader, observer)
    records = [
        encode_frame(NONCE, Frame(0, FrameKind.LAUNCHING, empty_body())),
        encode_frame(NONCE, Frame(1, FrameKind.STDOUT, b"abcdef")),
        encode_frame(NONCE, Frame(2, FrameKind.STDERR, b"error")),
    ]
    combined = memoryview(b"".join(records))
    assert collector.try_write(combined) == len(records[0])
    combined = combined[len(records[0]) :]
    assert collector.try_write(combined) == len(records[1]) - 1
    assert stdout.data == b"ab"
    combined = combined[len(records[1]) - 1 :]
    stdout.stalled = True
    assert collector.try_write(combined) is None
    assert stderr.calls == 0
    stdout.stalled = False
    assert collector.try_write(combined) is None
    assert stdout.data == b"abcd"
    assert collector.try_write(combined) == 1
    assert collector.try_write(combined[1:]) == len(records[2])
    assert stdout.data == b"abcdef" and stderr.data == b"error"
    observer.release()


@pytest.mark.parametrize("bad_digest", [False, True])
def test_delivered_stream_counts_and_digest_are_checked_without_retained_bytes(bad_digest: bool) -> None:
    stdout, stderr = Sink(), Sink()
    observer = InlineObserver(OutputMode.DELIVER, 0, stdout=stdout, stderr=stderr)
    reader = FrameReader(NONCE, observer.accept)
    collector = _LiveCollector(reader, observer)
    frames = [Frame(0, FrameKind.LAUNCHING, empty_body())]
    payload = bytes(range(256)) * 32
    for offset in range(0, len(payload), 4096):
        frames.append(Frame(len(frames), FrameKind.STDOUT, payload[offset : offset + 4096]))
    for stream, data in ((StreamName.STDOUT, payload), (StreamName.STDERR, b"")):
        digest = hashlib.sha256(data).hexdigest()
        if bad_digest and data:
            digest = "0" * 64
        end = StreamEnd(stream, len(data), digest, True, False, StreamRetention.DELIVERED)
        frames.append(Frame(len(frames), FrameKind.STREAM_END, encode_stream_end(end)))
    frames.append(Frame(len(frames), FrameKind.WAITED, encode_wait(WaitFact(WaitKind.EXIT, 255))))
    frames.append(Frame(len(frames), FrameKind.FINISHED, empty_body()))
    for frame in frames:
        encoded = encode_frame(NONCE, frame)
        assert collector.try_write(memoryview(encoded)) == len(encoded)
    observation = observer.finish(None, carrier_stdout_complete=True)
    assert stdout.data == payload
    if bad_digest:
        assert observation.error is ObservationError.STREAM
    else:
        assert observation.trusted_terminal
        assert observation.stdout is not None
        assert observation.stdout.count == len(payload)
        assert observation.stdout.data == b""
    observer.release()


@pytest.mark.parametrize("status", [0, 1, 255])
def test_packed_helper_duplex_handshake_precedes_live_source_read(plan, status: int) -> None:
    stdout, stderr = Sink(bound=3), Sink(bound=2)
    payload = bytes(range(256)) * 8

    class HandshakeSource(Source):
        def try_read(self, limit: int) -> bytes | None:
            if not stdout.data.startswith(b"ready\x00\xff"):
                return None
            return super().try_read(limit)

    source = HandshakeSource(payload)
    code = (
        "import os,sys;os.write(1,b'ready\\x00\\xff');"
        "data=sys.stdin.buffer.read();os.write(1,data);os.write(2,data[::-1]);"
        f"raise SystemExit({status})"
    )
    _, candidate, carrier = _run(
        Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", code]),
        plan,
        source=source,
        stdout=stdout,
        stderr=stderr,
        sensitive=True,
    )
    result = reduce_owned_inline_result(OwnedInlineOutcome(candidate=candidate))
    assert result.status == ExitCode(status)
    assert result.failure is None
    assert stdout.data == b"ready\x00\xff" + payload
    assert stderr.data == payload[::-1]
    assert result.stdout.data == result.stderr.data == b""
    assert result.stdout.retention is result.stderr.retention is Retention.DELIVERED
    assert carrier.last_report is not None
    assert carrier.last_report.stdout.data == carrier.last_report.stderr.data == b""


def test_packed_script_keeps_live_input_separate_from_source(plan) -> None:
    payload = b"\x00stdin\xff\n"
    stdout, stderr = Sink(), Sink()
    code = "import os,sys;data=sys.stdin.buffer.read();os.write(1,data);os.write(2,b'err\\x00\\xff')"
    source = f"/usr/bin/python3 -I -S -B -c {shlex.quote(code)}\nexit 1\n"
    _, candidate, _ = _run(
        Script(source, Shell.SH),
        plan,
        source=Source(payload),
        stdout=stdout,
        stderr=stderr,
    )
    assert candidate.observation is not None and candidate.observation.trusted_terminal
    assert candidate.observation.wait == WaitFact(WaitKind.EXIT, 1)
    assert stdout.data == payload and stderr.data == b"err\x00\xff"


@pytest.mark.parametrize("sensitive", [False, True])
def test_live_input_can_capture_or_suppress_output(plan, sensitive: bool) -> None:
    _, candidate, _ = _run(
        Command(["/bin/cat"]),
        plan,
        source=Source(b"live-input"),
        sensitive=sensitive,
    )
    assert candidate.observation is not None
    assert candidate.observation.stdout is not None
    assert candidate.observation.stdout.data == (b"" if sensitive else b"live-input")
    assert candidate.observation.stdout.retention is (
        StreamRetention.SUPPRESSED if sensitive else StreamRetention.CAPTURED
    )


def test_finite_input_can_deliver_live_output(plan) -> None:
    stdout, stderr = Sink(), Sink()
    _, candidate, _ = _run(Command(["/bin/cat"]), plan, stdin=b"finite", stdout=stdout, stderr=stderr)
    assert candidate.observation is not None and candidate.observation.trusted_terminal
    assert stdout.data == b"finite"


def test_live_input_can_discard_without_presenting_bytes(plan) -> None:
    _, candidate, _ = _run(Command(["/bin/cat"]), plan, source=Source(b"discarded"), capture_limit=None)
    assert candidate.observation is not None and candidate.observation.trusted_terminal
    assert candidate.observation.stdout is not None
    assert candidate.observation.stdout.retention is StreamRetention.DISCARDED
    assert candidate.observation.stdout.data == b""


def test_public_run_forwards_live_endpoints_and_keeps_only_facts(bound: Bound) -> None:
    _, _, operation, _, access, _ = bound
    source, stdout, stderr = Source(b"public-live"), Sink(bound=2), Sink()
    source_ref, stdout_ref, stderr_ref = weakref.ref(source), weakref.ref(stdout), weakref.ref(stderr)
    result = access.run(
        Command(["/bin/cat"]), profile=Protection.DIRECT,
        stdin=Input.live(source, sensitive=True), output=Output.live(stdout, stderr),
    )
    assert result.status == ExitCode(0) and result.failure is None
    assert stdout.data == b"public-live"
    assert result.stdout.data == result.stderr.data == b""
    assert result.stdout.retention is Retention.DELIVERED
    assert not operation.unfinished_inline_executions
    del source, stdout, stderr
    assert source_ref() is None
    assert stdout_ref() is None
    assert stderr_ref() is None


def test_actual_prepared_io_keeps_sensitivity_and_explicit_envelope(plan) -> None:
    prepared = prepare_inline_candidate(
        Command(["/bin/true"]),
        plan=plan,
        source=Source(b""),
        stdout=Sink(),
        stderr=Sink(),
        sensitive=True,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )
    assert isinstance(prepared.io.input, LiveInput)
    envelope = prepared.io.input.source.try_read(32768 + REQUEST_HEADER_BYTES)
    assert envelope is not None and envelope[:5] == b"AGWI2"
    assert int(envelope[5:REQUEST_HEADER_BYTES], 16) == len(envelope) - REQUEST_HEADER_BYTES
    assert decode_manifest(envelope[REQUEST_HEADER_BYTES:]).stdin is None
    assert prepared.io.sensitive and prepared.io.input.sensitive
    assert isinstance(prepared.io.output, SinkOutput) and prepared.io.output.require_live
    prepared.release()


def test_unsupported_live_carrier_refuses_before_operation_borrow(bound: Bound, monkeypatch) -> None:
    _, owner, operation, carrier, access, _ = bound
    source = Source(b"unused")
    sink = Sink()

    def refuse(invocation: object, *, io: CarrierIO) -> None:
        assert isinstance(io.input, LiveInput)
        raise StateError("unsupported live channel")

    def no_borrow():
        pytest.fail("Borrowed an operation before pure live-I/O validation")

    monkeypatch.setattr(carrier, "validate", refuse)
    monkeypatch.setattr(owner, "borrow", no_borrow)
    with pytest.raises(StateError):
        access.run(
            Command(["/bin/true"]), profile=Protection.DIRECT, stdin=Input.live(source), output=Output.live(sink, sink)
        )
    assert source.calls == sink.calls == carrier.calls == 0
    assert not operation.unfinished_inline_executions


@pytest.mark.parametrize(
    "start,lifetime",
    [
        (False, Lifetime.OPERATION),
        (False, Lifetime.INDEPENDENT),
        (True, Lifetime.OPERATION),
        (True, Lifetime.INDEPENDENT),
    ],
)
def test_managed_live_choices_refuse_before_effects(bound: Bound, start: bool, lifetime: Lifetime) -> None:
    _, _, operation, carrier, access, _ = bound
    source = Source(b"unused")
    method = access.start if start else access.run
    with pytest.raises(StateError):
        method(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=lifetime, stdin=Input.live(source))
    assert source.calls == carrier.calls == 0
    assert not operation.unfinished_inline_executions


@pytest.mark.parametrize("side", ["input", "output"])
@pytest.mark.parametrize("fault", ["invalid", "exception", "control"])
def test_endpoint_failure_is_safe_and_detaches_borrowed_values(plan, side: str, fault: str) -> None:
    control = KeyboardInterrupt("original endpoint control") if fault == "control" else ValueError("endpoint-canary")

    class FaultSource:
        def try_read(self, limit: int) -> bytes | None:
            if fault == "invalid":
                return "invalid"  # type: ignore[return-value]
            raise control

    class FaultSink:
        def try_write(self, data: memoryview) -> int | None:
            if fault == "invalid":
                return True
            raise control

    prepared = prepare_inline_candidate(
        Command(["/bin/cat"] if side == "input" else ["/bin/echo", "payload"]),
        plan=plan,
        source=FaultSource() if side == "input" else None,
        stdout=FaultSink() if side == "output" else Sink(),
        stderr=Sink(),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )
    carrier = LocalCarrier()
    if fault == "control":
        with pytest.raises(KeyboardInterrupt) as caught:
            execute_inline_candidate(carrier, prepared, deadline=Deadline.after(3))
        assert caught.value is control
    else:
        candidate = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(3))
        assert candidate.carrier_failure is (Failure.INPUT if side == "input" else Failure.OUTPUT)
        result = reduce_owned_inline_result(OwnedInlineOutcome(candidate=candidate))
        assert result.failure is (ExecutionFailure.INPUT if side == "input" else ExecutionFailure.OUTPUT)
        assert "endpoint-canary" not in repr(result)
    assert not isinstance(prepared.io.input, LiveInput)
    assert not prepared._observer._sinks and prepared._observer._pending is None


def test_stalled_output_ends_with_truthful_failure_and_releases_pending_bytes(plan) -> None:
    sink = Sink(stalled=True)
    prepared = prepare_inline_candidate(
        Command(["/bin/echo", "payload"]),
        plan=plan,
        stdout=sink,
        stderr=Sink(),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )
    candidate = execute_inline_candidate(LocalCarrier(), prepared, deadline=Deadline.after(3))
    result = reduce_owned_inline_result(OwnedInlineOutcome(candidate=candidate))
    assert result.failure is not None
    assert not result.stdout.complete
    assert result.stdout.data == b""
    assert result.application_state is ApplicationState.UNKNOWN
    assert not prepared._observer._sinks and prepared._observer._pending is None
