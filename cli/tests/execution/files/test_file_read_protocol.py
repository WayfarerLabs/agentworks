"""Adversarial host checks for the private AGWF1 file-read protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.execution._file_read import (
    FileReadObservationError,
    FileReadObservationState,
    execute_file_read,
    prepare_file_read,
)
from agentworks.execution._file_read_protocol import (
    MAX_RECORD_BYTES,
    FileReadFailure,
    FileReadIdentity,
    FileReadMetadata,
    FileReadRecord,
    FileReadRecordKind,
    FileReadRequestError,
    FileReadResultControl,
    FileReadWireError,
    decode_file_read_request,
    empty_file_read_body,
    encode_file_read_failure,
    encode_file_read_record,
    encode_file_read_result,
)
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


def _write(sink: ByteSink, data: bytes) -> int:
    calls = 0
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)
        assert written is not None and 0 < written <= len(remaining)
        calls += 1
        remaining = remaining[written:]
    return calls


@dataclass
class TranscriptCarrier:
    stdout: bytes
    stderr: bytes = b""
    completion: int = 0
    stdout_complete: bool = True
    stderr_complete: bool = True
    calls: int = 0
    sink_calls: int = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        assert isinstance(io.output, SinkOutput)
        self.sink_calls += _write(io.output.stdout, self.stdout)
        if self.stderr:
            self.sink_calls += _write(io.output.stderr, self.stderr)
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=self.completion),
            self.completion,
            CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED),
            CapturedOutput(complete=self.stderr_complete, retention=Retention.DELIVERED),
        )


@pytest.fixture
def identity() -> FileReadIdentity:
    return FileReadIdentity(1001, 1002, (1002, 1003))


def _record(nonce: str, sequence: int, kind: FileReadRecordKind, body: bytes) -> bytes:
    return encode_file_read_record(nonce, FileReadRecord(sequence, kind, body))


def _success(nonce: str, data: bytes) -> bytes:
    records: list[bytes] = []
    sequence = 0
    for offset in range(0, len(data), 4096):
        records.append(_record(nonce, sequence, FileReadRecordKind.DATA, data[offset : offset + 4096]))
        sequence += 1
    metadata = FileReadMetadata(1, 2, stat.S_IFREG | 0o600, 1, 1001, 1002, len(data), 3, 4)
    control = FileReadResultControl(len(data), hashlib.sha256(data).digest(), metadata)
    records.append(_record(nonce, sequence, FileReadRecordKind.RESULT, encode_file_read_result(control)))
    records.append(_record(nonce, sequence + 1, FileReadRecordKind.FINISHED, empty_file_read_body()))
    return b"".join(records)


def _execute(
    identity: FileReadIdentity,
    transcript: bytes | None = None,
    *,
    max_bytes: int = 32_000,
    **carrier_options: object,
):
    prepared = prepare_file_read(
        trusted_root_path="/trusted/root-canary",
        relative_path="leaf-canary",
        max_bytes=max_bytes,
        identity=identity,
    )
    carrier = TranscriptCarrier(
        _success(prepared.nonce, b"payload") if transcript is None else transcript, **carrier_options
    )
    result = execute_file_read(carrier, prepared, deadline=Deadline.after(1))
    assert carrier.calls == 1
    return prepared, carrier, result


def test_complete_transcript_is_authoritative_even_with_nonzero_carrier_status(identity: FileReadIdentity) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32_000,
        identity=identity,
    )
    data = bytes(range(256)) * 80
    carrier = TranscriptCarrier(_success(prepared.nonce, data), completion=23)

    result = execute_file_read(carrier, prepared, deadline=Deadline.after(1))

    assert result.carrier_completion == ExitStatus(code=23)
    assert result.observation.state is FileReadObservationState.PRESENT
    assert result.observation.snapshot is not None and result.observation.snapshot.data == data
    assert carrier.sink_calls > 1
    assert len(carrier.stdout) > MAX_RECORD_BYTES


def test_exit_zero_without_a_complete_transcript_is_not_success(identity: FileReadIdentity) -> None:
    _, _, result = _execute(identity, b"")

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
            FileReadWireError.MALFORMED,
        ),
        (
            lambda nonce: _record("0" * 32, 0, FileReadRecordKind.ABSENT, empty_file_read_body()),
            FileReadObservationState.INVALID,
            FileReadWireError.NONCE,
        ),
        (
            lambda nonce: _record(nonce, 1, FileReadRecordKind.ABSENT, empty_file_read_body()),
            FileReadObservationState.INVALID,
            FileReadWireError.SEQUENCE,
        ),
        (
            lambda nonce: _record(nonce, 0, FileReadRecordKind.ABSENT, empty_file_read_body())[:-1],
            FileReadObservationState.INCOMPLETE,
            FileReadWireError.TRUNCATED,
        ),
        (
            lambda nonce: (
                _record(nonce, 0, FileReadRecordKind.ABSENT, empty_file_read_body())
                + _record(nonce, 1, FileReadRecordKind.ABSENT, empty_file_read_body())
            ),
            FileReadObservationState.INVALID,
            FileReadObservationError.ORDER,
        ),
        (
            lambda nonce: (
                _record(nonce, 0, FileReadRecordKind.ABSENT, empty_file_read_body())
                + _record(nonce, 1, FileReadRecordKind.FINISHED, empty_file_read_body())
                + _record(nonce, 2, FileReadRecordKind.FINISHED, empty_file_read_body())
            ),
            FileReadObservationState.INVALID,
            FileReadObservationError.POST_TERMINAL,
        ),
    ],
)
def test_malformed_reflected_wrong_nonce_order_truncation_and_trailing_records_are_rejected(
    identity: FileReadIdentity,
    build,
    expected_state: FileReadObservationState,
    expected_error: FileReadObservationError | FileReadWireError,
) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    carrier = TranscriptCarrier(build(prepared.nonce))

    result = execute_file_read(carrier, prepared, deadline=Deadline.after(1))

    assert result.observation.state is expected_state
    assert result.observation.error is expected_error
    assert result.observation.snapshot is None


def test_digest_mismatch_and_incomplete_stream_disclose_no_data_or_hash(identity: FileReadIdentity) -> None:
    canary = b"private-payload-canary"
    prepared = prepare_file_read(
        trusted_root_path="/trusted/root-canary",
        relative_path="leaf-canary",
        max_bytes=len(canary),
        identity=identity,
    )
    metadata = FileReadMetadata(1, 2, stat.S_IFREG | 0o600, 1, 1001, 1002, len(canary), 3, 4)
    wrong = FileReadResultControl(len(canary), b"x" * 32, metadata)
    transcript = (
        _record(prepared.nonce, 0, FileReadRecordKind.DATA, canary)
        + _record(prepared.nonce, 1, FileReadRecordKind.RESULT, encode_file_read_result(wrong))
        + _record(prepared.nonce, 2, FileReadRecordKind.FINISHED, empty_file_read_body())
    )
    invalid = execute_file_read(TranscriptCarrier(transcript), prepared, deadline=Deadline.after(1))

    assert invalid.observation.state is FileReadObservationState.INVALID
    assert invalid.observation.snapshot is None
    assert canary.decode() not in repr(invalid)
    assert hashlib.sha256(canary).hexdigest() not in repr(invalid)

    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=len(canary),
        identity=identity,
    )
    incomplete = execute_file_read(
        TranscriptCarrier(_success(prepared.nonce, canary), stdout_complete=False),
        prepared,
        deadline=Deadline.after(1),
    )
    assert incomplete.observation.state is FileReadObservationState.INCOMPLETE
    assert incomplete.observation.snapshot is None
    assert canary.decode() not in repr(incomplete)


def test_result_metadata_must_bind_the_exact_stream_length(identity: FileReadIdentity) -> None:
    data = b"content"
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    metadata = FileReadMetadata(1, 2, stat.S_IFREG | 0o600, 1, 1001, 1002, len(data) + 1, 3, 4)
    control = FileReadResultControl(len(data), hashlib.sha256(data).digest(), metadata)
    transcript = (
        _record(prepared.nonce, 0, FileReadRecordKind.DATA, data)
        + _record(prepared.nonce, 1, FileReadRecordKind.RESULT, encode_file_read_result(control))
        + _record(prepared.nonce, 2, FileReadRecordKind.FINISHED, empty_file_read_body())
    )

    result = execute_file_read(TranscriptCarrier(transcript), prepared, deadline=Deadline.after(1))

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is FileReadObservationError.CONTROL
    assert result.observation.snapshot is None


def test_stderr_noise_is_rejected_without_retention(identity: FileReadIdentity) -> None:
    canary = b"raw-diagnostic-canary"
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    carrier = TranscriptCarrier(_success(prepared.nonce, b"content"), stderr=canary)

    result = execute_file_read(carrier, prepared, deadline=Deadline.after(1))

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is FileReadObservationError.STDERR
    assert result.observation.snapshot is None
    assert canary.decode() not in repr(result)
    assert result.carrier_failure is None


def test_absent_and_refusal_are_complete_typed_outcomes_without_payload(identity: FileReadIdentity) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    absent_transcript = _record(prepared.nonce, 0, FileReadRecordKind.ABSENT, empty_file_read_body()) + _record(
        prepared.nonce, 1, FileReadRecordKind.FINISHED, empty_file_read_body()
    )
    absent = execute_file_read(TranscriptCarrier(absent_transcript), prepared, deadline=Deadline.after(1))

    assert absent.observation.state is FileReadObservationState.ABSENT
    assert absent.observation.snapshot is None
    assert absent.observation.trusted_terminal

    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    refusal_transcript = _record(
        prepared.nonce,
        0,
        FileReadRecordKind.FAILED,
        encode_file_read_failure(FileReadFailure.UNSUPPORTED_OBJECT),
    ) + _record(prepared.nonce, 1, FileReadRecordKind.FINISHED, empty_file_read_body())
    refusal = execute_file_read(TranscriptCarrier(refusal_transcript), prepared, deadline=Deadline.after(1))

    assert refusal.observation.state is FileReadObservationState.REFUSED
    assert refusal.observation.failure is FileReadFailure.UNSUPPORTED_OBJECT
    assert refusal.observation.snapshot is None
    assert refusal.observation.trusted_terminal


def test_oversized_record_is_bounded_and_rejected(identity: FileReadIdentity) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=32,
        identity=identity,
    )
    transcript = b"A" * (MAX_RECORD_BYTES + 20) + b"\n"
    result = execute_file_read(TranscriptCarrier(transcript), prepared, deadline=Deadline.after(1))

    assert result.observation.state is FileReadObservationState.INVALID
    assert result.observation.error is FileReadWireError.OVERSIZED
    assert result.observation.snapshot is None


@pytest.mark.windows
def test_host_import_does_not_require_posix_only_modules() -> None:
    script = r"""
import importlib.abc
import sys

blocked = {"ctypes", "fcntl", "grp", "pwd"}
class BlockPosix(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in blocked:
            raise ImportError("blocked POSIX-only module")

sys.meta_path.insert(0, BlockPosix())
sys.path.insert(0, sys.argv[1])
import agentworks.execution._file_read
assert not blocked.intersection(sys.modules)
"""
    cli_root = Path(__file__).parents[3]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(cli_root)],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


@pytest.mark.windows
def test_protocol_is_an_independent_stdlib_module(tmp_path: Path) -> None:
    protocol_path = Path(__file__).parents[3] / "agentworks" / "execution" / "_file_read_protocol.py"
    script = r"""
import importlib.util
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("_standalone_file_read_protocol", path)
assert spec is not None and spec.loader is not None
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)
request = protocol.FileReadRequest(
    "0123456789abcdef0123456789abcdef",
    "/",
    "file",
    1,
    protocol.FileReadIdentity(1, 2, (2, 3)),
)
encoded = protocol.encode_file_read_request(request)
assert encoded.isascii()
assert protocol.decode_file_read_request(encoded) == request
assert "agentworks" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(protocol_path)],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_complete_qga_json_request_is_ascii_armored(identity: FileReadIdentity) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="snowman-\u2603",
        max_bytes=1024,
        identity=identity,
    )
    input_data = prepared.io.input.data  # type: ignore[union-attr]
    body = json.dumps({"command": prepared.invocation.argv, "input-data": input_data.decode("ascii")}).encode("ascii")

    assert input_data.isascii()
    assert all(argument.isascii() for argument in prepared.invocation.argv)
    assert body.isascii()
    assert len(body) > prepared.request_size


def test_invalid_request_exception_does_not_retain_raw_manifest_fields() -> None:
    manifest = json.dumps(
        {
            "identity": {"egid": 2, "euid": 1, "groups": [2]},
            "max_bytes": 1,
            "nonce": "0123456789abcdef0123456789abcdef",
            "operation": "read",
            "path": base64.b64encode(b"file").decode("ascii"),
            "root": base64.b64encode(b"/secret-\xff-canary").decode("ascii"),
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    with pytest.raises(FileReadRequestError) as raised:
        decode_file_read_request(manifest)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "canary" not in repr(raised.value)
