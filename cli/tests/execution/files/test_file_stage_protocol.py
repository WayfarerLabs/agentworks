"""Strict schemas for private stage creation and chunk exchange."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import pytest

from agentworks.execution._file_stage_protocol import (
    MAX_REQUEST_BYTES,
    MAX_STAGE_CHUNK_BYTES,
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageControlError,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageRequestError,
    decode_file_stage_request,
    encode_file_stage_begin_result,
    encode_file_stage_failure,
    encode_file_stage_request,
    parse_file_stage_begin_result,
    parse_file_stage_failure,
    stage_context,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import ScratchFailureKind, ScratchPhase, ScratchReference
from agentworks.execution._scratch_receipt import (
    _RECEIPT_BUILD_MODE,
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchOperation,
    ScratchOwnership,
    ScratchReceiptContext,
    _Identity,
    scratch_name,
)
from agentworks.execution._scratch_wire import (
    decode_cleanup_debt,
    decode_scratch_reference,
    encode_cleanup_debt,
    encode_scratch_reference,
)

_NONCE = "0123456789abcdef0123456789abcdef"
_TOKEN = bytes(range(16))
_IDENTITY = IdentityExpectation(1001, 1002, (1002, 1003))


def _reference(
    *,
    token: bytes = _TOKEN,
    context: ScratchReceiptContext | None = None,
) -> ScratchReference:
    return ScratchReference(
        ScratchOwnership(
            token,
            stage_context(_IDENTITY) if context is None else context,
            _Identity(1, 2),
            _Identity(1, 3),
            _Identity(1, 4),
            1002,
            20_000,
            _Identity(1, 5),
        )
    )


def _begin() -> FileStageBeginRequest:
    return FileStageBeginRequest(_NONCE, "/trusted/root", "nested/target", _TOKEN, 20_000, _IDENTITY, 1.25)


def _chunk(*, data: bytes = b"chunk") -> FileStageChunkRequest:
    import hashlib

    return FileStageChunkRequest(
        _NONCE,
        "/trusted/root",
        "nested/target",
        _TOKEN,
        _reference(),
        0,
        data,
        hashlib.sha256(data).digest(),
        _IDENTITY,
        1.25,
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
        details.append(getattr(current, "doc", None))
        details.append(getattr(current, "object", None))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return repr(details)


@pytest.mark.parametrize("stage_request", [_begin(), _chunk()])
def test_requests_round_trip_without_exposing_selectors_or_payload(stage_request) -> None:
    decoded = decode_file_stage_request(encode_file_stage_request(stage_request))

    assert decoded == stage_request
    assert stage_request.root_path not in repr(decoded)
    assert stage_request.relative_path not in repr(decoded)
    if isinstance(stage_request, FileStageChunkRequest):
        assert repr(stage_request.data) not in repr(decoded)


def test_chunk_uses_the_conservative_raw_bound_and_final_manifest_bound() -> None:
    request = _chunk(data=b"x" * MAX_STAGE_CHUNK_BYTES)
    encoded = encode_file_stage_request(request)

    assert len(encoded) <= MAX_REQUEST_BYTES
    assert decode_file_stage_request(encoded) == request

    with pytest.raises(FileStageRequestError):
        encode_file_stage_request(_chunk(data=b"x" * (MAX_STAGE_CHUNK_BYTES + 1)))


@pytest.mark.parametrize("path", ["", ".", "..", "/target", "nested//target", "nested/../target"])
def test_request_refuses_empty_self_or_nonnormal_relative_destination(path: str) -> None:
    request = _begin()
    malformed = FileStageBeginRequest(
        request.nonce,
        request.root_path,
        path,
        request.token,
        request.expected_length,
        request.identity,
        request.remaining_seconds,
    )

    with pytest.raises(FileStageRequestError):
        encode_file_stage_request(malformed)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=1),
        lambda value: value.update(version=True),
        lambda value: value.update(operation="unknown"),
        lambda value: value.update(remaining_seconds=1),
        lambda value: value.update(remaining_seconds=float("nan")),
        lambda value: value.update(token=base64.b64encode(b"short").decode("ascii")),
        lambda value: value.update(path=base64.b64encode(b".").decode("ascii")),
    ],
)
def test_request_refuses_extra_wrong_typed_nonfinite_and_invalid_fields(mutate) -> None:
    value = json.loads(encode_file_stage_request(_begin()))
    mutate(value)

    with pytest.raises(FileStageRequestError):
        decode_file_stage_request(_json(value))


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":1}',
        b'{ "version":1}',
        b"[]",
        b"\xff",
        b"{" + b'"x":' + b'"a"' * 40_000 + b"}",
    ],
)
def test_request_refuses_duplicate_noncanonical_invalid_and_oversized_json(data: bytes) -> None:
    with pytest.raises(FileStageRequestError):
        decode_file_stage_request(data)


def test_request_decoder_does_not_retain_sensitive_manifest_fields() -> None:
    canary = "stage-manifest-canary"
    value = json.loads(encode_file_stage_request(_begin()))
    value["path"] = base64.b64encode((canary + "\ud800").encode("utf-8", errors="surrogatepass")).decode("ascii")

    with pytest.raises(FileStageRequestError) as raised:
        decode_file_stage_request(_json(value))

    assert canary not in _exception_details(raised.value)


def test_active_reference_fragment_is_pathless_and_decoder_context_bound() -> None:
    reference = _reference()
    context = stage_context(_IDENTITY)
    encoded = encode_scratch_reference(reference)

    assert set(encoded) == {"artifact_gid", "data", "directory", "length", "parent", "receipt"}
    assert decode_scratch_reference(encoded, _TOKEN, context) == reference

    syntactic = decode_scratch_reference(encoded, b"z" * 16, context)
    assert syntactic._ownership._token == b"z" * 16


@pytest.mark.parametrize(
    "reference",
    [
        _reference(token=b"z" * 16),
        _reference(context=ScratchReceiptContext(ScratchOperation.SNAPSHOT, _IDENTITY)),
    ],
)
def test_chunk_request_entry_refuses_reference_outside_its_core_context(reference: ScratchReference) -> None:
    request = _chunk()
    mismatched = FileStageChunkRequest(
        request.nonce,
        request.root_path,
        request.relative_path,
        request.token,
        reference,
        request.offset,
        request.data,
        request.chunk_digest,
        request.identity,
        request.remaining_seconds,
    )

    with pytest.raises(FileStageRequestError):
        encode_file_stage_request(mismatched)


@pytest.mark.parametrize(
    ("modes", "state"),
    [
        ((_RECEIPT_MODE,), "final"),
        ((_RECEIPT_BUILD_MODE, _RECEIPT_MODE), "creating_or_final"),
    ],
)
def test_cleanup_debt_round_trips_without_exporting_its_derived_name(
    modes: tuple[int, ...],
    state: str,
) -> None:
    debt = ScratchCleanupDebt(
        scratch_name(_TOKEN),
        _Identity(1, 2),
        _Identity(1, 3),
        None,
        None,
        modes,
        _IDENTITY.euid,
        1002,
    )
    context = stage_context(_IDENTITY)
    encoded = encode_cleanup_debt(debt)

    assert encoded["receipt_state"] == state
    assert "name" not in encoded and "token" not in encoded
    assert decode_cleanup_debt(encoded, _TOKEN, context) == debt


def test_begin_result_round_trips_only_an_active_reference() -> None:
    reference = _reference()
    body = encode_file_stage_begin_result(reference)

    assert parse_file_stage_begin_result(body, _TOKEN, _IDENTITY) == reference
    assert set(json.loads(body)) == {"reference"}


@pytest.mark.parametrize("kind", list(ScratchFailureKind))
@pytest.mark.parametrize("phase", [ScratchPhase.BEGIN, ScratchPhase.WRITE])
def test_scratch_failure_round_trips_closed_fact_and_optional_debt(
    kind: ScratchFailureKind,
    phase: ScratchPhase,
) -> None:
    debt = ScratchCleanupDebt(
        scratch_name(_TOKEN),
        _Identity(1, 2),
        _Identity(1, 3),
        _Identity(1, 4),
        _Identity(1, 5),
        (_RECEIPT_MODE,),
        _IDENTITY.euid,
        1002,
    )
    failure = FileStageFailureControl(FileStageFailureCode.SCRATCH, kind, phase, debt)

    assert (
        parse_file_stage_failure(
            encode_file_stage_failure(failure),
            _TOKEN,
            _IDENTITY,
        )
        == failure
    )


def _parse_failure_field(field: str, supplied: object) -> object:
    value: dict[str, object] = {
        "cleanup": None,
        "code": FileStageFailureCode.SCRATCH.value,
        "kind": ScratchFailureKind.CONFLICT.value,
        "phase": ScratchPhase.BEGIN.value,
    }
    value[field] = supplied
    return parse_file_stage_failure(_json(value), _TOKEN, _IDENTITY)


@pytest.mark.parametrize(
    ("canary", "invoke"),
    [
        ("stage-code-canary", lambda: _parse_failure_field("code", "stage-code-canary")),
        ("stage-kind-canary", lambda: _parse_failure_field("kind", "stage-kind-canary")),
        ("stage-phase-canary", lambda: _parse_failure_field("phase", "stage-phase-canary")),
    ],
)
def test_response_decoder_does_not_retain_invalid_control_fields(
    canary: str,
    invoke: Callable[[], object],
) -> None:
    with pytest.raises(FileStageControlError) as raised:
        invoke()

    assert canary not in _exception_details(raised.value)
