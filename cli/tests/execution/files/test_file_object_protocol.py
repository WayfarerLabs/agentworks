"""Strict boundary and reducer checks for file-object exchanges."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass

import pytest

from agentworks.execution._file_object_exchange import (
    FileObjectMutationUncertain,
    FileObjectObservationError,
    FileObjectObservationState,
    remove_file,
    stat_file,
)
from agentworks.execution._file_object_protocol import (
    FileObjectControlError,
    FileObjectFailureCode,
    FileObjectFailureControl,
    FileObjectOperation,
    FileObjectRequest,
    FileObjectRequestError,
    FileObjectResultControl,
    FileObjectResultKind,
    decode_file_object_request,
    empty_file_object_body,
    encode_file_object_failure,
    encode_file_object_request,
    encode_file_object_result,
    parse_file_object_failure,
    parse_file_object_result,
)
from agentworks.execution._file_objects import FileKind, FileObjectFailureKind, FileObjectPhase
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileWireError, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    Retention,
    SinkOutput,
)


def _revision(kind: FileKind = FileKind.REGULAR, digest: bytes | None = None) -> FileRevision:
    mode = {
        FileKind.REGULAR: stat.S_IFREG | 0o600,
        FileKind.DIRECTORY: stat.S_IFDIR | 0o700,
        FileKind.SOCKET: stat.S_IFSOCK | 0o700,
    }[kind]
    return FileRevision(FileStat(1, 2, mode, 1, 1001, 1002, 7, 3, 4), digest)


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _request(operation: FileObjectOperation, *, digest: bytes | None = None) -> FileObjectRequest:
    expected_kind = FileKind.REGULAR if operation is FileObjectOperation.REMOVE else None
    expected = _revision(digest=digest) if operation is FileObjectOperation.REMOVE else None
    return FileObjectRequest(
        "0123456789abcdef0123456789abcdef",
        operation,
        "/trusted/root",
        "nested/leaf",
        1.25,
        IdentityExpectation(1001, 1002, (1002, 1003)),
        expected_kind,
        expected,
    )


@pytest.mark.parametrize("digest", [None, b"d" * 32])
def test_remove_request_round_trips_optional_regular_digest(digest: bytes | None) -> None:
    request = _request(FileObjectOperation.REMOVE, digest=digest)

    decoded = decode_file_object_request(encode_file_object_request(request))

    assert decoded == request
    assert request.root_path not in repr(decoded)
    assert request.relative_path not in repr(decoded)


def test_stat_request_round_trips_explicit_unbounded_duration() -> None:
    request = _request(FileObjectOperation.STAT)
    request = FileObjectRequest(
        request.nonce,
        request.operation,
        request.root_path,
        request.relative_path,
        None,
        request.identity,
    )
    assert decode_file_object_request(encode_file_object_request(request)) == request


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=1),
        lambda value: value.update(version=True),
        lambda value: value.update(remaining_seconds=1),
        lambda value: value.update(remaining_seconds=True),
        lambda value: value.update(remaining_seconds=float("nan")),
        lambda value: value.update(remaining_seconds=float("inf")),
        lambda value: value.update(operation="unknown"),
        lambda value: value.update(path="Li4="),
        lambda value: value.pop("expected_kind"),
        lambda value: value.update(expected_kind="socket"),
    ],
)
def test_request_rejects_extra_invalid_nonfinite_and_mismatched_fields(mutate) -> None:
    value = json.loads(encode_file_object_request(_request(FileObjectOperation.REMOVE)))
    mutate(value)
    data = json.dumps(value, allow_nan=True, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(FileObjectRequestError):
        decode_file_object_request(data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":1}',
        b'{ "version": 1 }',
        b"[]",
        b"\xff",
        b"{" + b'"x":' + b'"a"' * 40_000 + b"}",
    ],
)
def test_request_rejects_duplicate_noncanonical_invalid_and_oversized_json(data: bytes) -> None:
    with pytest.raises(FileObjectRequestError) as raised:
        decode_file_object_request(data)
    assert "a" * 100 not in repr(raised.value)


def test_request_rejects_huge_integer_duration_as_closed_invalid_request() -> None:
    value = json.loads(encode_file_object_request(_request(FileObjectOperation.STAT)))
    marker = b'"remaining_seconds":'
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    start = encoded.index(marker) + len(marker)
    end = encoded.index(b",", start)
    malformed = encoded[:start] + b"9" * 500 + encoded[end:]

    with pytest.raises(FileObjectRequestError) as raised:
        decode_file_object_request(malformed)

    assert raised.value.failure is FileObjectFailureCode.INVALID_REQUEST


@pytest.mark.parametrize("kind", [FileKind.REGULAR, FileKind.DIRECTORY, FileKind.SOCKET])
def test_stat_result_round_trips_metadata_only_supported_kinds(kind: FileKind) -> None:
    result = FileObjectResultControl(FileObjectResultKind.PRESENT, kind, _revision(kind))
    assert parse_file_object_result(encode_file_object_result(result), FileObjectOperation.STAT) == result


def test_result_rejects_operation_state_and_digest_combinations() -> None:
    wrong_operation = encode_file_object_result(FileObjectResultControl(FileObjectResultKind.CHANGED))
    with pytest.raises(FileObjectControlError):
        parse_file_object_result(wrong_operation, FileObjectOperation.STAT)

    digest_stat = encode_file_object_result(
        FileObjectResultControl(FileObjectResultKind.PRESENT, FileKind.REGULAR, _revision(digest=b"d" * 32))
    )
    with pytest.raises(FileObjectControlError):
        parse_file_object_result(digest_stat, FileObjectOperation.STAT)


@pytest.mark.parametrize("kind", list(FileObjectFailureKind))
@pytest.mark.parametrize("phase", list(FileObjectPhase))
def test_primitive_failure_round_trips_every_kind_and_phase(
    kind: FileObjectFailureKind, phase: FileObjectPhase
) -> None:
    failure = FileObjectFailureControl(FileObjectFailureCode.OBJECT, kind, phase)
    assert parse_file_object_failure(encode_file_object_failure(failure)) == failure


def _write(sink: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)  # type: ignore[attr-defined]
        assert written is not None and written > 0
        remaining = remaining[written:]


@dataclass
class TranscriptCarrier:
    build: object
    dispatch: Dispatch = Dispatch.SENT
    stderr: bytes = b""
    complete: bool = True
    failure: Failure | None = None
    calls: int = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        assert isinstance(io.input, FiniteInput)
        assert io.input.sensitive and isinstance(io.output, SinkOutput)
        request = decode_file_object_request(io.input.data)
        transcript = self.build(request)  # type: ignore[operator]
        _write(io.output.stdout, transcript)
        _write(io.output.stderr, self.stderr)
        output = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            ExitStatus(code=29) if self.dispatch is Dispatch.SENT else None,
            29,
            output,
            output,
            self.failure,
        )


def _records(request: FileObjectRequest, body: bytes, kind: FileRecordKind = FileRecordKind.RESULT) -> bytes:
    return encode_file_record(request.nonce, FileRecord(0, kind, body)) + encode_file_record(
        request.nonce, FileRecord(1, FileRecordKind.FINISHED, empty_file_object_body())
    )


def test_complete_stat_result_does_not_depend_on_carrier_exit(plan: IdentityPlan) -> None:
    revision = _revision(FileKind.SOCKET)
    carrier = TranscriptCarrier(
        lambda request: _records(
            request,
            encode_file_object_result(FileObjectResultControl(FileObjectResultKind.PRESENT, FileKind.SOCKET, revision)),
        )
    )

    result = stat_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="socket",
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert carrier.calls == 1
    assert result.carrier_completion == ExitStatus(code=29)
    assert result.observation.state is FileObjectObservationState.PRESENT
    assert result.observation.revision == revision
    assert result.observation.object_kind is FileKind.SOCKET


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [
        (Dispatch.NOT_SENT, FileObjectObservationState.INCOMPLETE),
        (Dispatch.SENT, FileObjectObservationState.UNCERTAIN),
        (Dispatch.UNKNOWN, FileObjectObservationState.UNCERTAIN),
    ],
)
def test_lost_remove_acknowledgement_is_never_retried_or_reported_unchanged(
    plan: IdentityPlan, dispatch: Dispatch, expected: FileObjectObservationState
) -> None:
    carrier = TranscriptCarrier(lambda _request: b"", dispatch=dispatch)

    result = remove_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="leaf",
        expected_kind=FileKind.REGULAR,
        expected_revision=_revision(),
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert carrier.calls == 1
    assert result.dispatch is dispatch
    assert result.observation.state is expected
    assert result.observation.error is FileObjectObservationError.MISSING_TERMINAL


def test_complete_primitive_uncertainty_retains_kind_and_phase(plan: IdentityPlan) -> None:
    failure = FileObjectFailureControl(
        FileObjectFailureCode.OBJECT,
        FileObjectFailureKind.UNCERTAIN,
        FileObjectPhase.REMOVAL,
    )
    carrier = TranscriptCarrier(
        lambda request: _records(request, encode_file_object_failure(failure), FileRecordKind.FAILED)
    )

    result = remove_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="leaf",
        expected_kind=FileKind.REGULAR,
        expected_revision=_revision(),
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert result.observation.state is FileObjectObservationState.UNCERTAIN
    assert result.observation.failure == failure
    assert result.observation.error is None


def test_stat_rejects_mutation_only_uncertain_failure_phase(plan: IdentityPlan) -> None:
    failure = FileObjectFailureControl(
        FileObjectFailureCode.OBJECT,
        FileObjectFailureKind.UNCERTAIN,
        FileObjectPhase.REMOVAL,
    )
    carrier = TranscriptCarrier(
        lambda request: _records(request, encode_file_object_failure(failure), FileRecordKind.FAILED)
    )

    result = stat_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="leaf",
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert result.observation.state is FileObjectObservationState.INVALID
    assert result.observation.error is FileObjectObservationError.CONTROL
    assert result.observation.failure is None


@pytest.mark.parametrize(
    ("build", "stderr", "error"),
    [
        (
            lambda request: (
                b"noise\n"
                + _records(request, encode_file_object_result(FileObjectResultControl(FileObjectResultKind.UNCHANGED)))
            ),
            b"",
            FileWireError.MALFORMED,
        ),
        (
            lambda request: encode_file_record("0" * 32, FileRecord(0, FileRecordKind.RESULT, b"{}")),
            b"",
            FileWireError.NONCE,
        ),
        (
            lambda request: _records(
                request, encode_file_object_result(FileObjectResultControl(FileObjectResultKind.UNCHANGED))
            )[:-1],
            b"",
            FileWireError.TRUNCATED,
        ),
        (
            lambda request: _records(
                request, encode_file_object_result(FileObjectResultControl(FileObjectResultKind.UNCHANGED))
            ),
            b"diagnostic-canary",
            FileObjectObservationError.STDERR,
        ),
    ],
)
def test_noisy_wrong_nonce_truncated_and_stderr_remove_responses_are_uncertain(
    plan: IdentityPlan, build, stderr: bytes, error: object
) -> None:
    result = remove_file(
        TranscriptCarrier(build, stderr=stderr),
        trusted_root_path="/trusted",
        relative_path="leaf",
        expected_kind=FileKind.REGULAR,
        expected_revision=_revision(),
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert result.observation.state is FileObjectObservationState.UNCERTAIN
    assert result.observation.error is error
    assert "diagnostic-canary" not in repr(result)


def test_control_interruption_propagates_with_safe_remove_uncertainty(plan: IdentityPlan) -> None:
    class InterruptingCarrier:
        @property
        def features(self) -> ChannelFeatures:
            return ChannelFeatures()

        def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            del invocation, io, deadline
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        remove_file(
            InterruptingCarrier(),
            trusted_root_path="/trusted",
            relative_path="leaf",
            expected_kind=FileKind.REGULAR,
            expected_revision=_revision(),
            plan=plan,
            deadline=Deadline.after(1),
        )
    assert isinstance(raised.value.__cause__, FileObjectMutationUncertain)
