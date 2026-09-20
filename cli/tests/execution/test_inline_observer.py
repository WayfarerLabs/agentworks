"""Adversarial transcript checks for the private inline evidence observer."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from agentworks.execution._evidence_wire import Frame, FrameKind, WireError, encode_frame
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_control import (
    ControlError,
    FailureCode,
    FailureFact,
    FailurePhase,
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    WaitKind,
    empty_body,
    encode_failure,
    encode_stream_end,
    encode_wait,
    parse_empty,
    parse_failure,
    parse_stream_end,
    parse_wait,
)
from agentworks.execution._inline_observer import ObservationError
from agentworks.execution._inline_request import IdentityExpectation
from agentworks.execution.carrier import (
    ByteSink,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command


def _write(sink: ByteSink, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)
        assert written is not None and 0 < written <= len(remaining)
        remaining = remaining[written:]


@dataclass
class TranscriptCarrier:
    stdout: bytes
    stderr: bytes = b""
    completion: int = 0
    stdout_complete: bool = True
    calls: int = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        assert isinstance(io.output, SinkOutput)
        _write(io.output.stdout, self.stdout)
        if self.stderr:
            _write(io.output.stderr, self.stderr)
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=self.completion),
            self.completion,
            CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED),
            CapturedOutput(complete=True, retention=Retention.DELIVERED),
        )


@pytest.fixture
def identity() -> IdentityExpectation:
    return IdentityExpectation(1001, 1002, (1002, 1003))


def _record(nonce: str, sequence: int, kind: FrameKind, body: bytes) -> bytes:
    return encode_frame(nonce, Frame(sequence, kind, body))


def _stream_end(stream: StreamName, data: bytes = b"") -> bytes:
    return encode_stream_end(
        StreamEnd(
            stream=stream,
            retained=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            complete=True,
            truncated=False,
            retention=StreamRetention.CAPTURED,
        )
    )


def _complete_transcript(nonce: str, *, stdout: bytes = b"", terminal: bool = True) -> bytes:
    sequence = 0
    records = [_record(nonce, sequence, FrameKind.LAUNCHING, empty_body())]
    sequence += 1
    if stdout:
        records.append(_record(nonce, sequence, FrameKind.STDOUT, stdout))
        sequence += 1
    records.append(_record(nonce, sequence, FrameKind.STREAM_END, _stream_end(StreamName.STDOUT, stdout)))
    sequence += 1
    records.append(_record(nonce, sequence, FrameKind.STREAM_END, _stream_end(StreamName.STDERR)))
    sequence += 1
    records.append(_record(nonce, sequence, FrameKind.WAITED, encode_wait(WaitFact(WaitKind.EXIT, 23))))
    sequence += 1
    if terminal:
        records.append(_record(nonce, sequence, FrameKind.FINISHED, empty_body()))
    return b"".join(records)


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (parse_empty, b'{"value":1,"value":1}'),
        (
            parse_stream_end,
            b'{"complete":true,"complete":true,"retained":0,"retention":"captured",'
            b'"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",'
            b'"stream":"stdout","truncated":false}',
        ),
        (parse_wait, b'{"kind":"exit","kind":"exit","value":0}'),
        (parse_failure, b'{"code":"invalid","code":"invalid","phase":"request"}'),
    ],
)
def test_control_schemas_reject_duplicate_keys(parser: Callable[[bytes], object], body: bytes) -> None:
    with pytest.raises(ControlError):
        parser(body)


def test_noise_reflection_requires_a_line_boundary_and_is_not_retained(identity: IdentityExpectation) -> None:
    canary = b"raw-hook-reflection-canary-09dba6"
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    same_line_decoy = canary + _record(prepared.nonce, 0, FrameKind.FINISHED, empty_body())
    carrier = TranscriptCarrier(same_line_decoy + _complete_transcript(prepared.nonce), stderr=canary)

    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(1))

    assert carrier.calls == 1
    assert result.observation.trusted_terminal
    assert result.observation.wait == WaitFact(WaitKind.EXIT, 23)
    assert canary.decode() not in repr(result)


def test_missing_terminal_preserves_independently_validated_wait(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    carrier = TranscriptCarrier(_complete_transcript(prepared.nonce, terminal=False))

    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(1))

    assert result.observation.wait == WaitFact(WaitKind.EXIT, 23)
    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.MISSING_TERMINAL


def test_truncated_matching_record_preserves_wait_but_not_terminal(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    transcript = _complete_transcript(prepared.nonce, terminal=False)
    partial = _record(prepared.nonce, 5, FrameKind.FINISHED, empty_body())[:-1]
    carrier = TranscriptCarrier(transcript + partial)

    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(1))

    assert result.observation.wait == WaitFact(WaitKind.EXIT, 23)
    assert not result.observation.trusted_terminal
    assert result.observation.error is not None


def test_post_terminal_frame_revokes_terminal_evidence(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    transcript = _complete_transcript(prepared.nonce)
    transcript += _record(prepared.nonce, 5, FrameKind.FINISHED, empty_body())

    result = execute_inline_candidate(
        TranscriptCarrier(transcript),
        prepared,
        deadline=Deadline.after(1),
    )

    assert result.observation.wait == WaitFact(WaitKind.EXIT, 23)
    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.POST_TERMINAL


def test_sensitive_data_frame_is_rejected_without_retaining_reflection(identity: IdentityExpectation) -> None:
    canary = b"sensitive-frame-reflection-635a4d"
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity, sensitive=True)
    transcript = _record(prepared.nonce, 0, FrameKind.LAUNCHING, empty_body())
    transcript += _record(prepared.nonce, 1, FrameKind.STDOUT, canary)

    result = execute_inline_candidate(
        TranscriptCarrier(transcript),
        prepared,
        deadline=Deadline.after(1),
    )

    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.ORDER
    assert result.observation.stdout is None
    assert canary.decode() not in repr(result)


def test_malformed_matching_wire_never_produces_terminal_evidence(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    malformed = b"AGWE1 " + prepared.nonce.encode() + b" 0 LAUNCHING 2 not-canonical-base64\n"

    result = execute_inline_candidate(
        TranscriptCarrier(malformed, completion=255),
        prepared,
        deadline=Deadline.after(1),
    )

    assert result.carrier_completion == ExitStatus(code=255)
    assert not result.observation.trusted_terminal
    assert result.observation.error is WireError.MALFORMED


def test_started_record_is_rejected_by_candidate_grammar(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    transcript = _record(prepared.nonce, 0, FrameKind.STARTED, empty_body())

    result = execute_inline_candidate(
        TranscriptCarrier(transcript),
        prepared,
        deadline=Deadline.after(1),
    )

    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.ORDER


@pytest.mark.parametrize("kind", [FrameKind.STDOUT, FrameKind.STDERR])
def test_launch_failure_after_empty_data_frame_is_rejected(kind: FrameKind, identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)
    transcript = _record(prepared.nonce, 0, FrameKind.LAUNCHING, empty_body())
    transcript += _record(prepared.nonce, 1, kind, b"")
    transcript += _record(
        prepared.nonce,
        2,
        FrameKind.FAILED,
        encode_failure(FailureFact(FailurePhase.LAUNCH, FailureCode.DISPATCH)),
    )
    transcript += _record(prepared.nonce, 3, FrameKind.FINISHED, empty_body())

    result = execute_inline_candidate(
        TranscriptCarrier(transcript),
        prepared,
        deadline=Deadline.after(1),
    )

    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.ORDER


@pytest.mark.parametrize("raw_status", [0, 255])
def test_raw_carrier_status_never_substitutes_for_helper_terminal(
    raw_status: int,
    identity: IdentityExpectation,
) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)

    result = execute_inline_candidate(
        TranscriptCarrier(b"account-shell-noise\n", completion=raw_status),
        prepared,
        deadline=Deadline.after(1),
    )

    assert result.carrier_completion == ExitStatus(code=raw_status)
    assert result.carrier_local_status == raw_status
    assert not result.observation.trusted_terminal
    assert result.observation.wait is None
    assert result.observation.error is ObservationError.MISSING_TERMINAL


def test_incomplete_carrier_stdout_revokes_otherwise_valid_terminal(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)

    result = execute_inline_candidate(
        TranscriptCarrier(_complete_transcript(prepared.nonce), stdout_complete=False),
        prepared,
        deadline=Deadline.after(1),
    )

    assert result.observation.wait == WaitFact(WaitKind.EXIT, 23)
    assert not result.observation.trusted_terminal
    assert result.observation.error is ObservationError.CARRIER
