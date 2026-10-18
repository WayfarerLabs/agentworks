"""Closed scratch capture grammar and independent immutable retrieval verification."""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _capture_read as reader
from agentworks.execution._capture_cleanup import CaptureCleanup
from agentworks.execution._capture_control import (
    CaptureCreation,
    CaptureStreamFact,
    encode_capture_fact,
    parse_capture_fact,
)
from agentworks.execution._capture_obligation import (
    CaptureObligation,
    decode_capture_obligation,
    encode_capture_obligation,
)
from agentworks.execution._evidence_wire import Frame, FrameKind
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotCandidateResult,
    FileSnapshotObservation,
    FileSnapshotObservationState,
)
from agentworks.execution._file_snapshot_protocol import FileSnapshotChunkResult
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_control import (
    ControlError,
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    WaitKind,
    empty_body,
    encode_wait,
)
from agentworks.execution._inline_observer import InlineObserver
from agentworks.execution._inline_request import OutputMode
from agentworks.execution._prepared_helper_obligation import PreparedHelperObligation
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimePrerequisiteState
from agentworks.execution._scratch import ReadyScratchReference, ScratchReference
from agentworks.execution._scratch_receipt import ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity
from agentworks.execution.carrier import Deadline, Dispatch, ExitStatus

from ._capture_support import PLAN
from .test_prepared_helper_stage import BOOTSTRAP, RUNTIME, TARGET

TOKENS = (b"a" * 16, b"b" * 16)


def fact(data: bytes = b"abc", stream: StreamName = StreamName.STDOUT) -> CaptureStreamFact:
    identity = _Identity(1, 2)
    digest = hashlib.sha256(data).digest()
    ready = ReadyScratchReference(
        ScratchReference(
            ScratchOwnership(
                TOKENS[0 if stream is StreamName.STDOUT else 1],
                ScratchReceiptContext(ScratchOperation.SNAPSHOT, PLAN.expected),
                identity,
                identity,
                identity,
                PLAN.expected.egid,
                len(data),
                identity,
            )
        ),
        digest,
        1,
        1,
    )
    return CaptureStreamFact(
        StreamEnd(stream, len(data), digest.hex(), True, False, StreamRetention.CAPTURED),
        CaptureCreation.POSSIBLE,
        ready,
    )


@pytest.mark.parametrize("mutation", ["length", "digest", "unknown", "inline"])
def test_ready_record_requires_exact_length_hash_and_closed_fields(mutation):
    value = fact()
    body = encode_capture_fact(value)
    tokens, identity = TOKENS, PLAN.expected
    fields = json.loads(body)
    if mutation == "length":
        fields["count"] += 1
    elif mutation == "digest":
        fields["sha256"] = "0" * 64
    elif mutation == "unknown":
        fields["other"] = 1
    else:
        fields.pop("capture")
    body = json.dumps(fields).encode()
    with pytest.raises(ControlError):
        parse_capture_fact(body, tokens, identity, 1_048_576)


def test_reference_syntax_gets_only_the_external_stream_token_and_workload_context():
    body = encode_capture_fact(fact())
    identity = IdentityExpectation(PLAN.expected.euid + 1, PLAN.expected.egid, PLAN.expected.groups)
    tokens = (b"c" * 16, TOKENS[1])
    decoded = parse_capture_fact(body, tokens, identity, 1_048_576)
    assert decoded.ready is not None
    ownership = decoded.ready._reference._ownership
    assert ownership._token == tokens[0]
    assert ownership._context == ScratchReceiptContext(ScratchOperation.SNAPSHOT, identity)
    # Syntax cannot grant live ownership: the immutable receipt must agree at
    # the actual snapshot read/cleanup boundary before that reference is usable.


def test_capture_grammar_accepts_only_two_records_before_actual_wait_and_no_inline_data():
    observer = InlineObserver(
        OutputMode.SCRATCH_CAPTURE, 1_048_576, capture_tokens=TOKENS, capture_identity=PLAN.expected
    )
    frames = [
        (FrameKind.LAUNCHING, empty_body()),
        (FrameKind.STREAM_END, encode_capture_fact(fact())),
        (FrameKind.STREAM_END, encode_capture_fact(fact(b"xyz", StreamName.STDERR))),
        (FrameKind.WAITED, encode_wait(WaitFact(WaitKind.EXIT, 23))),
        (FrameKind.FINISHED, empty_body()),
    ]
    for index, (kind, body) in enumerate(frames):
        observer.accept(Frame(index, kind, body))
    observation = observer.finish(None, carrier_stdout_complete=True)
    assert observation.trusted_terminal and observation.wait is not None and observation.wait.value == 23
    assert observation.stdout is None and observation.stderr is None and len(observation.capture_facts) == 2
    for replacement in (FrameKind.STDOUT, FrameKind.WAITED):
        observer = InlineObserver(
            OutputMode.SCRATCH_CAPTURE, 1_048_576, capture_tokens=TOKENS, capture_identity=PLAN.expected
        )
        observer.accept(Frame(0, FrameKind.LAUNCHING, empty_body()))
        observer.accept(
            Frame(
                1, replacement, b"abc" if replacement is FrameKind.STDOUT else encode_wait(WaitFact(WaitKind.EXIT, 23))
            )
        )
        assert observer.finish(None, carrier_stdout_complete=True).error is not None


def test_ordinary_stream_grammar_does_not_accept_scratch_records():
    observer = InlineObserver(OutputMode.CAPTURE, 4096)
    observer.accept(Frame(0, FrameKind.LAUNCHING, empty_body()))
    observer.accept(Frame(1, FrameKind.STREAM_END, encode_capture_fact(fact())))
    assert observer.finish(None, carrier_stdout_complete=True).error is not None


def test_token_only_cleanup_binding_is_canonical_and_excludes_remote_references():
    binding = CaptureObligation(PreparedHelperObligation(TARGET, BOOTSTRAP, RUNTIME, TOKENS[0]), PLAN, TOKENS[1])
    payload = encode_capture_obligation(binding)
    assert decode_capture_obligation(payload) == binding
    assert set(json.loads(payload)) == {"version", "target", "bootstrap", "runtime", "workload", "tokens"}
    with pytest.raises(ValidationError):
        decode_capture_obligation(payload + b" ")


@pytest.mark.parametrize("damage", ["offset", "range_digest", "whole_digest"])
def test_local_retrieval_checks_range_and_final_digest_without_fabricating_complete(monkeypatch, damage):
    data = b"a" * 262_144 + b"b" * 5
    remote = fact(data)
    calls: list[int] = []

    class Cleanup:
        helper_pending = False
        accepted = staticmethod(CaptureCleanup.accepted)

        def call(self, action, deadline):
            offset = 0 if not calls else 262_144
            block = data[offset : offset + 262_144]
            calls.append(offset)
            digest = hashlib.sha256(block).digest()
            if len(calls) == 2:
                if damage == "offset":
                    offset += 1
                elif damage == "range_digest":
                    digest = bytes(32)
                else:
                    block = b"c" * len(block)
                    digest = hashlib.sha256(block).digest()
            chunk = FileSnapshotChunkResult(offset, len(block), block, digest)
            return FileSnapshotCandidateResult(
                Dispatch.SENT,
                ExitStatus(0),
                None,
                None,
                RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3"),
                FileSnapshotObservation(FileSnapshotObservationState.CHUNK, chunk=chunk),
            )

    output = reader._read(Cleanup(), remote, Deadline.after(30))
    assert output.data.startswith(data[:262_144]) and not output.verified and calls == [0, 262_144]
    if damage != "whole_digest":
        assert output.data == data[:262_144]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux capture guest")
def test_publication_failure_preserves_prior_process_failure_and_actual_wait(monkeypatch):
    import os

    from agentworks.execution import _capture_guest as publisher
    from agentworks.execution import _inline_guest as guest
    from agentworks.execution._inline_control import FailureCode
    from agentworks.execution._inline_guest_io import FrameWriter
    from agentworks.execution._inline_request import InlineManifest, InvocationKind
    from agentworks.execution._process import ProcessFailure, ProcessResult, StreamResult

    manifest = InlineManifest(
        "a" * 32,
        InvocationKind.COMMAND,
        ("/bin/true",),
        None,
        None,
        (),
        None,
        b"",
        OutputMode.SCRATCH_CAPTURE,
        1_048_576,
        PLAN.expected,
        TOKENS,
    )
    monkeypatch.setattr(publisher, "read_request", lambda: manifest)
    monkeypatch.setattr(guest, "matches_current_identity", lambda identity: True)
    monkeypatch.setattr(guest, "_prepare_launch", lambda request: guest._Launch(["/bin/true"], None))
    monkeypatch.setattr(
        guest,
        "run_owned_process",
        lambda *args, **kwargs: ProcessResult(
            True, 23, 23, StreamResult(b"x", True), StreamResult(b"y", True), ProcessFailure.INPUT
        ),
    )

    def refuse(*args, **kwargs):
        raise OSError()

    monkeypatch.setattr(publisher, "open_scratch_root", lambda **kwargs: 123456)
    monkeypatch.setattr(publisher, "begin_scratch", refuse)
    original_close = os.close

    def close(descriptor: int) -> None:
        if descriptor == 123456:
            raise OSError()
        original_close(descriptor)

    monkeypatch.setattr(os, "close", close)
    frames = []
    monkeypatch.setattr(FrameWriter, "emit", lambda self, kind, body: frames.append((kind, body)))
    assert publisher.main(manifest.nonce) == 0
    observer = InlineObserver(
        OutputMode.SCRATCH_CAPTURE, 1_048_576, capture_tokens=TOKENS, capture_identity=PLAN.expected
    )
    for index, (kind, body) in enumerate(frames):
        observer.accept(Frame(index, kind, body))
    observation = observer.finish(None, carrier_stdout_complete=True)
    assert observation.trusted_terminal and observation.wait is not None and observation.wait.value == 23
    assert observation.failure is not None and observation.failure.code is FailureCode.INPUT
