"""Adversarial host checks for the private AGWF1 file-read protocol."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.execution import _file_read
from agentworks.execution._file_read import (
    FileReadCandidateResult,
    FileReadObservationError,
    FileReadObservationState,
    read_file,
)
from agentworks.execution._file_read_bundle import FIXED_SOURCE
from agentworks.execution._file_read_protocol import (
    FileReadFailure,
    FileReadResultControl,
    empty_file_read_body,
    encode_file_read_failure,
    encode_file_read_result,
)
from agentworks.execution._file_stat import FileStat
from agentworks.execution._file_wire import (
    MAX_RECORD_BYTES,
    FileRecord,
    FileRecordKind,
    FileRecordReader,
    FileRecordWriter,
    FileWireError,
    encode_file_record,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    ByteSink,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
    SinkOutput,
)


def _write(sink: ByteSink, data: bytes) -> int:
    calls = 0
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)
        assert written is not None and 0 < written <= len(remaining)
        calls += 1
        remaining = remaining[written:]
    return calls


def test_file_record_writer_completes_short_writes_and_sequences_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "0" * 32
    written = bytearray()
    calls = 0

    def short_write(descriptor: int, data: memoryview) -> int:
        nonlocal calls
        assert descriptor == 1
        calls += 1
        count = min(3, len(data))
        written.extend(data[:count])
        return count

    monkeypatch.setattr(os, "write", short_write)
    writer = FileRecordWriter(nonce)
    writer.write(FileRecordKind.DATA, b"payload")
    writer.write(FileRecordKind.FINISHED, b"{}")

    expected = encode_file_record(nonce, FileRecord(0, FileRecordKind.DATA, b"payload")) + encode_file_record(
        nonce,
        FileRecord(1, FileRecordKind.FINISHED, b"{}"),
    )
    assert bytes(written) == expected
    assert calls > 2


def test_file_record_writer_rejects_zero_write_without_advancing_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "0" * 32
    writer = FileRecordWriter(nonce)
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 0)

    with pytest.raises(OSError):
        writer.write(FileRecordKind.DATA, b"unwritten")

    written = bytearray()

    def complete_write(descriptor: int, data: memoryview) -> int:
        assert descriptor == 1
        written.extend(data)
        return len(data)

    monkeypatch.setattr(os, "write", complete_write)
    writer.write(FileRecordKind.FINISHED, b"{}")
    assert bytes(written) == encode_file_record(nonce, FileRecord(0, FileRecordKind.FINISHED, b"{}"))


@dataclass
class TranscriptCarrier:
    stdout: bytes | Callable[[str], bytes]
    stderr: bytes = b""
    completion: int = 0
    stdout_complete: bool = True
    stderr_complete: bool = True
    calls: int = 0
    sink_calls: int = 0
    invocation: PreparedInvocation | None = None
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.output, SinkOutput)
        transcript = self.stdout(invocation.argv[-1]) if callable(self.stdout) else self.stdout
        self.sink_calls += _write(io.output.stdout, transcript)
        if self.stderr:
            self.sink_calls += _write(io.output.stderr, self.stderr)
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=self.completion),
            self.completion,
            CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED),
            CapturedOutput(complete=self.stderr_complete, retention=Retention.DELIVERED),
        )


@dataclass
class InterruptingCarrier:
    build_stdout: Callable[[str], bytes]
    interruption: BaseException
    reader: FileRecordReader | None = None
    collector: _file_read._FileReadCollector | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        assert isinstance(io.output, SinkOutput)
        assert isinstance(io.output.stdout, FileRecordReader)
        self.reader = io.output.stdout
        callback_owner = getattr(self.reader._on_record, "__self__", None)
        assert isinstance(callback_owner, _file_read._FileReadCollector)
        self.collector = callback_owner
        _write(io.output.stdout, self.build_stdout(invocation.argv[-1]))
        raise self.interruption


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _record(nonce: str, sequence: int, kind: FileRecordKind, body: bytes) -> bytes:
    return encode_file_record(nonce, FileRecord(sequence, kind, body))


def _success(nonce: str, data: bytes) -> bytes:
    records: list[bytes] = []
    sequence = 0
    for offset in range(0, len(data), 4096):
        records.append(_record(nonce, sequence, FileRecordKind.DATA, data[offset : offset + 4096]))
        sequence += 1
    metadata = FileStat(1, 2, stat.S_IFREG | 0o600, 1, 1001, 1002, len(data), 3, 4)
    control = FileReadResultControl(hashlib.sha256(data).digest(), metadata)
    records.append(_record(nonce, sequence, FileRecordKind.RESULT, encode_file_read_result(control)))
    records.append(_record(nonce, sequence + 1, FileRecordKind.FINISHED, empty_file_read_body()))
    return b"".join(records)


def _read(
    carrier: TranscriptCarrier | InterruptingCarrier,
    plan: IdentityPlan,
    *,
    max_bytes: int = 32_000,
    deadline: Deadline | None = None,
) -> FileReadCandidateResult:
    return read_file(
        carrier,
        trusted_root_path="/trusted/root-canary",
        relative_path="leaf-canary",
        max_bytes=max_bytes,
        plan=plan,
        deadline=deadline or Deadline.after(1),
    )


def test_complete_transcript_is_authoritative_even_with_nonzero_carrier_status(plan: IdentityPlan) -> None:
    data = bytes(range(256)) * 80
    carrier = TranscriptCarrier(lambda nonce: _success(nonce, data), completion=23)

    result = _read(carrier, plan)

    assert result.carrier_completion == ExitStatus(code=23)
    assert result.observation.state is FileReadObservationState.PRESENT
    assert result.observation.snapshot is not None and result.observation.snapshot.data == data
    assert carrier.calls == 1 and carrier.sink_calls > 1


def test_exit_zero_without_a_complete_transcript_is_not_success(plan: IdentityPlan) -> None:
    result = _read(TranscriptCarrier(b""), plan)

    assert result.carrier_completion == ExitStatus(code=0)
    assert result.observation.state is FileReadObservationState.INCOMPLETE
    assert result.observation.snapshot is None
    assert result.observation.error is FileReadObservationError.MISSING_TERMINAL


@pytest.mark.parametrize(
    ("build", "expected_state", "expected_error"),
    [
        (
            lambda nonce: b"reflected-request-canary\n" + _success(nonce, b"secret"),
            FileReadObservationState.INVALID,
            FileWireError.MALFORMED,
        ),
        (
            lambda _nonce: _record("0" * 32, 0, FileRecordKind.ABSENT, empty_file_read_body()),
            FileReadObservationState.INVALID,
            FileWireError.NONCE,
        ),
        (
            lambda nonce: _record(nonce, 1, FileRecordKind.ABSENT, empty_file_read_body()),
            FileReadObservationState.INVALID,
            FileWireError.SEQUENCE,
        ),
        (
            lambda nonce: _record(nonce, 0, FileRecordKind.ABSENT, empty_file_read_body())[:-1],
            FileReadObservationState.INCOMPLETE,
            FileWireError.TRUNCATED,
        ),
        (
            lambda nonce: (
                _record(nonce, 0, FileRecordKind.ABSENT, empty_file_read_body())
                + _record(nonce, 1, FileRecordKind.ABSENT, empty_file_read_body())
            ),
            FileReadObservationState.INVALID,
            FileReadObservationError.ORDER,
        ),
        (
            lambda nonce: (
                _record(nonce, 0, FileRecordKind.ABSENT, empty_file_read_body())
                + _record(nonce, 1, FileRecordKind.FINISHED, empty_file_read_body())
                + _record(nonce, 2, FileRecordKind.FINISHED, empty_file_read_body())
            ),
            FileReadObservationState.INVALID,
            FileReadObservationError.POST_TERMINAL,
        ),
    ],
)
def test_malformed_reflected_wrong_nonce_order_truncation_and_trailing_records_are_rejected(
    plan: IdentityPlan,
    build: Callable[[str], bytes],
    expected_state: FileReadObservationState,
    expected_error: FileReadObservationError | FileWireError,
) -> None:
    result = _read(TranscriptCarrier(build), plan, max_bytes=32)

    assert result.observation.state is expected_state
    assert result.observation.error is expected_error
    assert result.observation.snapshot is None


def test_digest_mismatch_and_incomplete_stream_disclose_no_data_or_hash(plan: IdentityPlan) -> None:
    canary = b"private-payload-canary"
    metadata = FileStat(1, 2, stat.S_IFREG | 0o600, 1, 1001, 1002, len(canary), 3, 4)
    wrong = FileReadResultControl(b"x" * 32, metadata)

    def mismatched(nonce: str) -> bytes:
        return (
            _record(nonce, 0, FileRecordKind.DATA, canary)
            + _record(nonce, 1, FileRecordKind.RESULT, encode_file_read_result(wrong))
            + _record(nonce, 2, FileRecordKind.FINISHED, empty_file_read_body())
        )

    invalid = _read(TranscriptCarrier(mismatched), plan, max_bytes=len(canary))
    incomplete = _read(
        TranscriptCarrier(lambda nonce: _success(nonce, canary), stdout_complete=False),
        plan,
        max_bytes=len(canary),
    )

    for result in (invalid, incomplete):
        assert result.observation.snapshot is None
        assert canary.decode() not in repr(result)
        assert hashlib.sha256(canary).hexdigest() not in repr(result)
    assert invalid.observation.state is FileReadObservationState.INVALID
    assert incomplete.observation.state is FileReadObservationState.INCOMPLETE


@pytest.mark.parametrize("invalid_mode", [False, True])
def test_result_metadata_must_bind_stream_and_closed_regular_mode(
    plan: IdentityPlan,
    invalid_mode: bool,
) -> None:
    data = b"" if invalid_mode else b"content"
    mode = 0x80000000 | stat.S_IFREG | 0o600 if invalid_mode else stat.S_IFREG | 0o600
    size = len(data) if invalid_mode else len(data) + 1
    metadata = FileStat(1, 2, mode, 1, 1001, 1002, size, 3, 4)
    control = FileReadResultControl(hashlib.sha256(data).digest(), metadata)

    def transcript(nonce: str) -> bytes:
        prefix = b"" if not data else _record(nonce, 0, FileRecordKind.DATA, data)
        sequence = 0 if not data else 1
        return (
            prefix
            + _record(nonce, sequence, FileRecordKind.RESULT, encode_file_read_result(control))
            + _record(nonce, sequence + 1, FileRecordKind.FINISHED, empty_file_read_body())
        )

    result = _read(TranscriptCarrier(transcript), plan, max_bytes=32)

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is (
        FileReadObservationError.CONTROL if invalid_mode else FileReadObservationError.CONTENT
    )
    assert result.observation.snapshot is None


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_carrier_base_exception_clears_transcript_state_before_propagation(
    plan: IdentityPlan,
    interruption_type: type[BaseException],
) -> None:
    canary = b"collector-interrupt-canary"
    interruption = interruption_type()
    carrier = InterruptingCarrier(lambda nonce: _success(nonce, canary) + b"reader-canary", interruption)

    with pytest.raises(interruption_type) as raised:
        _read(carrier, plan, max_bytes=len(canary))

    assert raised.value is interruption
    assert carrier.collector is not None and carrier.reader is not None
    assert carrier.collector._data == bytearray()
    assert carrier.collector._result is None
    assert not carrier.collector._terminal
    assert carrier.reader._record == bytearray()
    assert canary.decode() not in repr(carrier)


@pytest.mark.parametrize("phase", ["reader", "collector"])
def test_finalization_base_exception_clears_transcript_state_before_propagation(
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    interruption = KeyboardInterrupt()

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise interruption

    target = FileRecordReader if phase == "reader" else _file_read._FileReadCollector
    monkeypatch.setattr(target, "finish", interrupt)
    carrier = TranscriptCarrier(lambda nonce: _success(nonce, b"collector-interrupt-canary"))

    with pytest.raises(KeyboardInterrupt) as raised:
        _read(carrier, plan)

    assert raised.value is interruption
    assert carrier.io is not None and isinstance(carrier.io.output, SinkOutput)
    reader = carrier.io.output.stdout
    assert isinstance(reader, FileRecordReader)
    collector = getattr(reader._on_record, "__self__", None)
    assert isinstance(collector, _file_read._FileReadCollector)
    assert collector._data == bytearray()
    assert collector._result is None
    assert not collector._terminal
    assert reader._record == bytearray()


def test_stderr_noise_is_rejected_without_retention(plan: IdentityPlan) -> None:
    canary = b"raw-diagnostic-canary"
    result = _read(
        TranscriptCarrier(lambda nonce: _success(nonce, b"content"), stderr=canary),
        plan,
        max_bytes=32,
    )

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is FileReadObservationError.STDERR
    assert result.observation.snapshot is None
    assert canary.decode() not in repr(result)
    assert result.carrier_failure is None


@pytest.mark.parametrize(
    ("kind", "failure", "expected_state"),
    [
        (FileRecordKind.ABSENT, None, FileReadObservationState.ABSENT),
        (FileRecordKind.FAILED, FileReadFailure.LOCK_UNSAFE, FileReadObservationState.REFUSED),
    ],
)
def test_absence_and_refusal_are_complete_typed_outcomes(
    plan: IdentityPlan,
    kind: FileRecordKind,
    failure: FileReadFailure | None,
    expected_state: FileReadObservationState,
) -> None:
    body = empty_file_read_body() if failure is None else encode_file_read_failure(failure)

    def transcript(nonce: str) -> bytes:
        return _record(nonce, 0, kind, body) + _record(nonce, 1, FileRecordKind.FINISHED, empty_file_read_body())

    result = _read(TranscriptCarrier(transcript), plan, max_bytes=32)

    assert result.observation.state is expected_state
    assert result.observation.failure is failure
    assert result.observation.snapshot is None


def test_oversized_record_is_bounded_and_rejected(plan: IdentityPlan) -> None:
    result = _read(TranscriptCarrier(b"A" * (MAX_RECORD_BYTES + 20) + b"\n"), plan, max_bytes=32)

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is FileWireError.OVERSIZED
    assert result.observation.snapshot is None


def test_record_callback_exception_clears_buffer_and_propagates_without_raw_retention() -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    canary = b"callback-canary"
    failure = RuntimeError("closed callback failure")

    def reject(_record: FileRecord) -> None:
        raise failure

    reader = FileRecordReader(nonce, reject)
    with pytest.raises(RuntimeError) as raised:
        _write(reader, encode_file_record(nonce, FileRecord(0, FileRecordKind.DATA, canary)))

    assert raised.value is failure
    assert reader.error is FileWireError.CALLBACK
    assert reader._record == bytearray()
    assert canary.decode() not in repr(reader)


@pytest.mark.windows
def test_host_import_does_not_require_posix_only_modules() -> None:
    script = r"""
import sys

blocked = {"ctypes", "fcntl", "grp", "pwd"}
sys.modules.update(dict.fromkeys(blocked))
sys.path.insert(0, sys.argv[1])
import agentworks.execution._file_read
assert all(sys.modules[name] is None for name in blocked)
"""
    cli_root = Path(__file__).parents[3]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(cli_root)],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


@pytest.mark.windows
@pytest.mark.parametrize("interpreter", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_fixed_helper_bundle_is_an_independent_stdlib_package(tmp_path: Path, interpreter: Path) -> None:
    if not interpreter.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {interpreter}")
    nonce = "0123456789abcdef0123456789abcdef"
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", FIXED_SOURCE, nonce],
        cwd=tmp_path,
        input=b"",
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stderr == b""
    assert completed.stdout.startswith(f"AGWF1 {nonce} ".encode("ascii"))
    assert completed.stdout.isascii()


def test_complete_qga_json_request_is_ascii_armored_and_within_full_body_limit(plan: IdentityPlan) -> None:
    carrier = TranscriptCarrier(b"")
    read_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="snowman-☃",
        max_bytes=1024,
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert carrier.io is not None and carrier.invocation is not None
    input_data = carrier.io.input.data  # type: ignore[union-attr]
    body = json.dumps({"command": carrier.invocation.argv, "input-data": input_data.decode("ascii")}).encode("ascii")

    assert input_data.isascii()
    assert all(argument.isascii() for argument in carrier.invocation.argv)
    assert body.isascii()
    assert len(input_data) < len(body) <= 65_536
