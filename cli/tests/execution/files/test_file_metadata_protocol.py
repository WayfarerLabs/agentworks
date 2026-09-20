"""Adversarial host checks for the private metadata protocol."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import agentworks.execution._file_metadata_exchange as exchange_module
from agentworks.errors import ValidationError
from agentworks.execution._file_metadata import (
    MetadataEffect,
    MetadataFailureKind,
    MetadataPhase,
    MetadataStep,
)
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataMutationUncertain,
    FileMetadataObservationError,
    FileMetadataObservationState,
    ensure_file_directory,
    set_file_metadata,
)
from agentworks.execution._file_metadata_protocol import (
    FileMetadataControlError,
    FileMetadataFailureCode,
    FileMetadataFailureControl,
    FileMetadataOperation,
    FileMetadataRequest,
    FileMetadataRequestError,
    FileMetadataResultControl,
    FileMetadataResultKind,
    decode_file_metadata_request,
    empty_file_metadata_body,
    encode_file_metadata_failure,
    encode_file_metadata_request,
    encode_file_metadata_result,
    parse_file_metadata_failure,
    parse_file_metadata_result,
)
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


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _exception_details(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    details: list[object] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        details.extend(current.args)
        details.append(getattr(current, "object", None))
        details.append(getattr(current, "doc", None))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return repr(details)


@pytest.mark.windows
def test_host_import_is_safe_without_posix_only_modules() -> None:
    script = r"""
import sys
blocked = {"ctypes", "fcntl", "grp", "pwd"}
sys.modules.update(dict.fromkeys(blocked))
sys.path.insert(0, sys.argv[1])
import agentworks.execution._file_metadata_exchange
assert all(sys.modules[name] is None for name in blocked)
"""
    cli_root = Path(__file__).parents[3]
    completed = subprocess.run([sys.executable, "-I", "-c", script, str(cli_root)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _revision() -> FileRevision:
    return FileRevision(FileStat(1, 2, stat.S_IFREG | 0o640, 1, 1001, 1002, 7, 3, 4))


def _request(operation: FileMetadataOperation = FileMetadataOperation.SET_METADATA) -> FileMetadataRequest:
    return FileMetadataRequest(
        "0123456789abcdef0123456789abcdef",
        operation,
        "/trusted/root",
        "nested/leaf",
        1001,
        1002,
        0o640,
        1.25,
        IdentityExpectation(1001, 1002, (1002, 1003)),
    )


@pytest.mark.parametrize("operation", list(FileMetadataOperation))
def test_request_round_trips_both_closed_operations(operation: FileMetadataOperation) -> None:
    request = _request(operation)

    decoded = decode_file_metadata_request(encode_file_metadata_request(request))

    assert decoded == request
    assert request.root_path not in repr(decoded)
    assert request.relative_path not in repr(decoded)


def test_request_round_trips_explicit_unbounded_duration_and_numeric_metadata() -> None:
    request = _request(FileMetadataOperation.ENSURE_DIRECTORY)
    request = FileMetadataRequest(
        request.nonce,
        request.operation,
        request.root_path,
        request.relative_path,
        0,
        2**32 - 1,
        0o7777,
        None,
        request.identity,
    )
    assert decode_file_metadata_request(encode_file_metadata_request(request)) == request


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
        lambda value: value.update(uid=-1),
        lambda value: value.update(gid=2**32),
        lambda value: value.update(mode=0o10000),
    ],
)
def test_request_rejects_extra_invalid_nonfinite_and_out_of_range_fields(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    value = json.loads(encode_file_metadata_request(_request()))
    mutate(value)
    data = json.dumps(value, allow_nan=True, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(FileMetadataRequestError):
        decode_file_metadata_request(data)


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
    with pytest.raises(FileMetadataRequestError) as raised:
        decode_file_metadata_request(data)
    assert "a" * 100 not in _exception_details(raised.value)


def test_request_rejects_path_over_4096_decoded_bytes() -> None:
    value = json.loads(encode_file_metadata_request(_request()))
    value["path"] = base64.b64encode(b"a" * 4097).decode("ascii")
    with pytest.raises(FileMetadataRequestError):
        decode_file_metadata_request(_json(value))


@pytest.mark.parametrize(
    ("canary", "field", "supplied"),
    [
        ("metadata-operation-canary", "operation", "metadata-operation-canary"),
        ("metadata-path-canary", "path", base64.b64encode(b"metadata-path-canary\xff").decode("ascii")),
    ],
)
def test_request_decoder_errors_discard_sensitive_values(canary: str, field: str, supplied: object) -> None:
    value = json.loads(encode_file_metadata_request(_request()))
    value[field] = supplied

    with pytest.raises(FileMetadataRequestError) as raised:
        decode_file_metadata_request(_json(value))

    assert canary not in _exception_details(raised.value)


def test_request_json_error_discards_decoder_document() -> None:
    canary = "metadata-json-doc-canary"
    with pytest.raises(FileMetadataRequestError) as raised:
        decode_file_metadata_request(b'{"operation":"metadata-json-doc-canary"')
    assert canary not in _exception_details(raised.value)


def test_request_encoder_discards_unicode_object() -> None:
    canary = "metadata-encoder-object-canary"
    request = _request()
    request = FileMetadataRequest(
        request.nonce,
        request.operation,
        request.root_path,
        "\ud800" + canary,
        request.uid,
        request.gid,
        request.mode,
        request.remaining_seconds,
        request.identity,
    )
    with pytest.raises(FileMetadataRequestError) as raised:
        encode_file_metadata_request(request)
    assert canary not in _exception_details(raised.value)


def test_request_rejects_huge_integer_without_retaining_json_error() -> None:
    value = json.loads(encode_file_metadata_request(_request()))
    encoded = _json(value)
    marker = b'"uid":'
    start = encoded.index(marker) + len(marker)
    end = encoded.index(b",", start)
    malformed = encoded[:start] + b"9" * 500 + encoded[end:]

    with pytest.raises(FileMetadataRequestError) as raised:
        decode_file_metadata_request(malformed)

    assert raised.value.failure is FileMetadataFailureCode.INVALID_REQUEST


@pytest.mark.parametrize("kind", list(FileMetadataResultKind))
def test_result_round_trips_changed_and_revision(kind: FileMetadataResultKind) -> None:
    result = FileMetadataResultControl(kind, _revision())
    assert parse_file_metadata_result(encode_file_metadata_result(result)) == result
    assert "revision" not in repr(result)


def test_result_rejects_content_revision_and_unsupported_object_kind() -> None:
    with pytest.raises(FileMetadataControlError):
        encode_file_metadata_result(
            FileMetadataResultControl(FileMetadataResultKind.CHANGED, FileRevision(_revision().stat, b"d" * 32))
        )
    value = json.loads(
        encode_file_metadata_result(FileMetadataResultControl(FileMetadataResultKind.CHANGED, _revision()))
    )
    value["revision"]["mode"] = stat.S_IFSOCK | 0o600
    with pytest.raises(FileMetadataControlError):
        parse_file_metadata_result(_json(value))


@pytest.mark.parametrize(
    ("failure", "effect"),
    [
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.CONFLICT,
                MetadataPhase.OBSERVATION,
            ),
            MetadataEffect.UNCHANGED,
        ),
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.IO,
                MetadataPhase.VERIFICATION,
                (MetadataStep.CREATION,),
            ),
            MetadataEffect.PARTIAL,
        ),
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.METADATA,
                MetadataPhase.MODE,
                (MetadataStep.OWNERSHIP,),
                MetadataStep.MODE,
            ),
            MetadataEffect.UNCERTAIN,
        ),
    ],
)
def test_metadata_failure_round_trips_and_derives_effect(
    failure: FileMetadataFailureControl, effect: MetadataEffect
) -> None:
    decoded = parse_file_metadata_failure(encode_file_metadata_failure(failure))
    assert decoded == failure
    assert decoded.effect is effect
    assert b"effect" not in encode_file_metadata_failure(failure)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(effect="unchanged"),
        lambda value: value.update(completed_steps=["mode", "ownership"]),
        lambda value: value.update(completed_steps=["mode", "mode"]),
        lambda value: value.update(phase="observation", completed_steps=["mode"]),
        lambda value: value.update(phase="creation", attempted_step="mode"),
        lambda value: value.update(phase="verification", attempted_step="mode"),
        lambda value: value.update(completed_steps=["mode"], attempted_step="mode"),
    ],
)
def test_response_rejects_contradictory_or_incoherent_effect_facts(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    failure = FileMetadataFailureControl(
        FileMetadataFailureCode.METADATA,
        MetadataFailureKind.IO,
        MetadataPhase.MODE,
        (MetadataStep.OWNERSHIP,),
        MetadataStep.MODE,
    )
    value = json.loads(encode_file_metadata_failure(failure))
    mutate(value)
    with pytest.raises(FileMetadataControlError):
        parse_file_metadata_failure(_json(value))


def _parse_failure_field(field: str, supplied: str) -> object:
    value: dict[str, object] = {
        "attempted_step": None,
        "code": FileMetadataFailureCode.METADATA.value,
        "completed_steps": [],
        "kind": MetadataFailureKind.CONFLICT.value,
        "phase": MetadataPhase.OBSERVATION.value,
    }
    value[field] = supplied
    return parse_file_metadata_failure(_json(value))


@pytest.mark.parametrize(
    ("canary", "invoke"),
    [
        (
            "metadata-result-canary",
            lambda: parse_file_metadata_result(_json({"result": "metadata-result-canary", "revision": {}})),
        ),
        ("metadata-code-canary", lambda: _parse_failure_field("code", "metadata-code-canary")),
        ("metadata-kind-canary", lambda: _parse_failure_field("kind", "metadata-kind-canary")),
        ("metadata-phase-canary", lambda: _parse_failure_field("phase", "metadata-phase-canary")),
        ("metadata-step-canary", lambda: _parse_failure_field("attempted_step", "metadata-step-canary")),
    ],
)
def test_response_decoder_errors_discard_sensitive_values(canary: str, invoke: Callable[[], object]) -> None:
    with pytest.raises(FileMetadataControlError) as raised:
        invoke()
    assert canary not in _exception_details(raised.value)


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
        request = decode_file_metadata_request(io.input.data)
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


def _records(request: FileMetadataRequest, body: bytes, kind: FileRecordKind = FileRecordKind.RESULT) -> bytes:
    return encode_file_record(request.nonce, FileRecord(0, kind, body)) + encode_file_record(
        request.nonce,
        FileRecord(1, FileRecordKind.FINISHED, empty_file_metadata_body()),
    )


def _set(carrier: object, plan: IdentityPlan) -> FileMetadataCandidateResult:
    return set_file_metadata(
        carrier,  # type: ignore[arg-type]
        trusted_root_path="/trusted",
        relative_path="leaf",
        uid=1001,
        gid=1002,
        mode=0o640,
        plan=plan,
        deadline=Deadline.after(1),
    )


def test_complete_result_is_authoritative_and_carrier_evidence_stays_separate(plan: IdentityPlan) -> None:
    carrier = TranscriptCarrier(
        lambda request: _records(
            request,
            encode_file_metadata_result(FileMetadataResultControl(FileMetadataResultKind.CHANGED, _revision())),
        )
    )

    result = _set(carrier, plan)

    assert carrier.calls == 1
    assert result.carrier_completion == ExitStatus(code=29)
    assert result.carrier_local_status == 29
    assert result.observation.state is FileMetadataObservationState.CHANGED
    assert result.observation.revision == _revision()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.CONFLICT,
                MetadataPhase.OBSERVATION,
            ),
            FileMetadataObservationState.REFUSED,
        ),
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.IO,
                MetadataPhase.VERIFICATION,
                (MetadataStep.CREATION,),
            ),
            FileMetadataObservationState.PARTIAL,
        ),
        (
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.METADATA,
                MetadataPhase.MODE,
                (),
                MetadataStep.MODE,
            ),
            FileMetadataObservationState.UNCERTAIN,
        ),
    ],
)
def test_complete_helper_failure_preserves_exact_effect(
    plan: IdentityPlan,
    failure: FileMetadataFailureControl,
    expected: FileMetadataObservationState,
) -> None:
    carrier = TranscriptCarrier(
        lambda request: _records(request, encode_file_metadata_failure(failure), FileRecordKind.FAILED)
    )

    result = ensure_file_directory(
        carrier,
        trusted_root_path="/trusted",
        relative_path="leaf",
        uid=1001,
        gid=1002,
        mode=0o2770,
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert result.observation.state is expected
    assert result.observation.failure == failure


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [
        (Dispatch.NOT_SENT, FileMetadataObservationState.INCOMPLETE),
        (Dispatch.SENT, FileMetadataObservationState.UNCERTAIN),
        (Dispatch.UNKNOWN, FileMetadataObservationState.UNCERTAIN),
    ],
)
def test_missing_acknowledgement_is_not_replayed_or_claimed_unchanged(
    plan: IdentityPlan,
    dispatch: Dispatch,
    expected: FileMetadataObservationState,
) -> None:
    carrier = TranscriptCarrier(lambda _request: b"", dispatch=dispatch)
    result = _set(carrier, plan)
    assert carrier.calls == 1
    assert result.observation.state is expected
    assert result.observation.error is FileMetadataObservationError.MISSING_TERMINAL


@pytest.mark.parametrize(
    ("build", "stderr", "error"),
    [
        (
            lambda request: _records(request, b"{}"),
            b"",
            FileMetadataObservationError.CONTROL,
        ),
        (
            lambda request: _records(request, b"{}", FileRecordKind.FAILED),
            b"",
            FileMetadataObservationError.CONTROL,
        ),
        (
            lambda request: (
                b"noise\n"
                + _records(
                    request,
                    encode_file_metadata_result(FileMetadataResultControl(FileMetadataResultKind.CHANGED, _revision())),
                )
            ),
            b"",
            FileWireError.MALFORMED,
        ),
        (
            lambda _request: encode_file_record("0" * 32, FileRecord(0, FileRecordKind.RESULT, b"{}")),
            b"",
            FileWireError.NONCE,
        ),
        (
            lambda request: _records(
                request,
                encode_file_metadata_result(FileMetadataResultControl(FileMetadataResultKind.CHANGED, _revision())),
            )[:-1],
            b"",
            FileWireError.TRUNCATED,
        ),
        (
            lambda request: _records(
                request,
                encode_file_metadata_result(FileMetadataResultControl(FileMetadataResultKind.CHANGED, _revision())),
            ),
            b"diagnostic-canary",
            FileMetadataObservationError.STDERR,
        ),
    ],
)
def test_malformed_or_incomplete_responses_are_uncertain(
    plan: IdentityPlan,
    build: Callable[[FileMetadataRequest], bytes],
    stderr: bytes,
    error: object,
) -> None:
    result = _set(TranscriptCarrier(build, stderr=stderr), plan)
    assert result.observation.state is FileMetadataObservationState.UNCERTAIN
    assert result.observation.error is error
    assert "diagnostic-canary" not in repr(result)


@pytest.mark.parametrize(
    "build",
    [
        lambda _request: b"x" * 8_193,
        lambda request: encode_file_metadata_request(request),
    ],
    ids=["oversized", "reflected-request"],
)
def test_oversized_and_reflected_output_is_bounded_uncertainty(
    plan: IdentityPlan,
    build: Callable[[FileMetadataRequest], bytes],
) -> None:
    result = _set(TranscriptCarrier(build), plan)
    assert result.observation.state is FileMetadataObservationState.UNCERTAIN
    assert result.observation.revision is None
    assert result.observation.failure is None
    assert "/trusted" not in repr(result)


def test_set_rejects_cross_operation_creation_failure(plan: IdentityPlan) -> None:
    failure = FileMetadataFailureControl(
        FileMetadataFailureCode.METADATA,
        MetadataFailureKind.IO,
        MetadataPhase.VERIFICATION,
        (MetadataStep.CREATION,),
    )
    result = _set(
        TranscriptCarrier(
            lambda request: _records(request, encode_file_metadata_failure(failure), FileRecordKind.FAILED)
        ),
        plan,
    )
    assert result.observation.state is FileMetadataObservationState.UNCERTAIN
    assert result.observation.error is FileMetadataObservationError.CONTROL


def test_host_path_validation_discards_unicode_error_chain(plan: IdentityPlan) -> None:
    canary = "metadata-host-path-canary"
    carrier = TranscriptCarrier(lambda _request: b"")
    with pytest.raises(ValidationError) as raised:
        set_file_metadata(
            carrier,
            trusted_root_path="/trusted",
            relative_path="\ud800" + canary,
            uid=1001,
            gid=1002,
            mode=0o640,
            plan=plan,
            deadline=Deadline.after(1),
        )
    assert canary not in _exception_details(raised.value)
    assert carrier.calls == 0


def test_host_request_conversion_discards_encoder_chain(
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "metadata-host-encoder-canary"

    def fail(_request: FileMetadataRequest) -> bytes:
        raw_error: UnicodeEncodeError | None = None
        try:
            ("\ud800" + canary).encode("utf-8")
        except UnicodeEncodeError as error:
            raw_error = error
        assert raw_error is not None
        wrapped = FileMetadataRequestError(FileMetadataFailureCode.INVALID_REQUEST)
        wrapped.__context__ = raw_error
        raise wrapped

    monkeypatch.setattr(exchange_module, "encode_file_metadata_request", fail)
    carrier = TranscriptCarrier(lambda _request: b"")
    with pytest.raises(ValidationError) as raised:
        _set(carrier, plan)
    assert canary not in _exception_details(raised.value)
    assert carrier.calls == 0


def test_control_interruption_propagates_with_safe_uncertainty(plan: IdentityPlan) -> None:
    class InterruptingCarrier:
        @property
        def features(self) -> ChannelFeatures:
            return ChannelFeatures()

        def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            del invocation, io, deadline
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        _set(InterruptingCarrier(), plan)
    assert isinstance(raised.value.__cause__, FileMetadataMutationUncertain)


def test_sink_fault_propagates_with_safe_uncertainty_and_no_replay(plan: IdentityPlan) -> None:
    class FaultingCarrier:
        calls = 0

        @property
        def features(self) -> ChannelFeatures:
            return ChannelFeatures()

        def execute(self, invocation: object, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            del invocation, deadline
            self.calls += 1
            assert isinstance(io.output, SinkOutput)
            io.output.stdout.try_write(memoryview(b"output-fault-canary\n"))
            raise RuntimeError("sink failed")

    carrier = FaultingCarrier()
    with pytest.raises(RuntimeError) as raised:
        _set(carrier, plan)
    assert carrier.calls == 1
    assert isinstance(raised.value.__cause__, FileMetadataMutationUncertain)
