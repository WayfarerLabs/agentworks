"""Strict schemas for private snapshot transfer and recovery."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agentworks.execution._file_revision_wire import (
    FileRevisionWireError,
    decode_file_revision,
    encode_file_revision,
)
from agentworks.execution._file_snapshot_protocol import (
    MAX_PATH_BYTES,
    MAX_REQUEST_BYTES,
    MAX_SNAPSHOT_CHUNK_BYTES,
    FileSnapshotBeginRequest,
    FileSnapshotChunkRequest,
    FileSnapshotChunkResult,
    FileSnapshotCleanupRequest,
    FileSnapshotControlError,
    FileSnapshotFailureCode,
    FileSnapshotFailureControl,
    FileSnapshotReconcileRequest,
    FileSnapshotRequestError,
    decode_file_snapshot_request,
    empty_file_snapshot_body,
    encode_file_snapshot_begin_result,
    encode_file_snapshot_chunk_result,
    encode_file_snapshot_cleanup_result,
    encode_file_snapshot_failure,
    encode_file_snapshot_reconcile_result,
    encode_file_snapshot_request,
    parse_empty_file_snapshot_body,
    parse_file_snapshot_begin_result,
    parse_file_snapshot_chunk_result,
    parse_file_snapshot_cleanup_result,
    parse_file_snapshot_failure,
    parse_file_snapshot_reconcile_result,
    snapshot_context,
)
from agentworks.execution._file_spool import SpoolSnapshot, SpoolSnapshotFailureKind
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchPhase,
    ScratchReference,
    _cleanup_debt,
)
from agentworks.execution._scratch_receipt import (
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnership,
    ScratchOwnershipUncertainty,
    ScratchReceiptContext,
    _Identity,
    scratch_name,
)
from agentworks.execution._scratch_wire import (
    ScratchWireError,
    decode_ready_scratch_reference,
    encode_ready_scratch_reference,
)

_NONCE = "0123456789abcdef0123456789abcdef"
_TOKEN = bytes(range(16))
_IDENTITY = IdentityExpectation(1001, 1002, (1002, 1003))
_CONTENT = b"snapshot payload"
_DIGEST = hashlib.sha256(_CONTENT).digest()


def _reference(
    *,
    length: int = len(_CONTENT),
    token: bytes = _TOKEN,
    context: ScratchReceiptContext | None = None,
) -> ScratchReference:
    return ScratchReference(
        ScratchOwnership(
            token,
            snapshot_context(_IDENTITY) if context is None else context,
            _Identity(1, 2),
            _Identity(1, 3),
            _Identity(1, 4),
            1002,
            length,
            _Identity(1, 5),
        )
    )


def _ready(*, length: int = len(_CONTENT), digest: bytes = _DIGEST) -> ReadyScratchReference:
    return ReadyScratchReference(_reference(length=length), digest, 7, 8)


def _revision(
    *,
    size: int = len(_CONTENT),
    digest: bytes | None = _DIGEST,
    mode: int = stat.S_IFREG | 0o600,
    links: int = 1,
) -> FileRevision:
    return FileRevision(FileStat(1, 9, mode, links, 1001, 1002, size, 7, 8), digest)


def _begin(**changes: object) -> FileSnapshotBeginRequest:
    values: dict[str, object] = {
        "nonce": _NONCE,
        "token": _TOKEN,
        "root_path": "/trusted/root",
        "relative_path": "nested/source",
        "max_bytes": 20_000,
        "identity": _IDENTITY,
        "remaining_seconds": 1.25,
    }
    values.update(changes)
    return FileSnapshotBeginRequest(**values)  # type: ignore[arg-type]


def _chunk(**changes: object) -> FileSnapshotChunkRequest:
    values: dict[str, object] = {
        "nonce": _NONCE,
        "token": _TOKEN,
        "ready": _ready(),
        "offset": 0,
        "length": len(_CONTENT),
        "identity": _IDENTITY,
        "remaining_seconds": 1.25,
    }
    values.update(changes)
    return FileSnapshotChunkRequest(**values)  # type: ignore[arg-type]


def _reconcile() -> FileSnapshotReconcileRequest:
    return FileSnapshotReconcileRequest(_NONCE, _TOKEN, _IDENTITY, 1.25)


def _cleanup() -> FileSnapshotCleanupRequest:
    return FileSnapshotCleanupRequest(_NONCE, _TOKEN, _cleanup_debt(_reference()), _IDENTITY, 1.25)


def _json(value: object, *, allow_nan: bool = False) -> bytes:
    return json.dumps(
        value,
        allow_nan=allow_nan,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


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


@pytest.mark.windows
@pytest.mark.parametrize("interpreter", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_protocol_import_is_safe_without_posix_account_modules(interpreter: Path) -> None:
    if not interpreter.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {interpreter}")
    script = r"""
import sys
blocked = {"fcntl", "grp", "pwd"}
sys.modules.update(dict.fromkeys(blocked))
sys.path.insert(0, sys.argv[1])
import agentworks.execution._file_snapshot_protocol
assert all(sys.modules[name] is None for name in blocked)
"""
    cli_root = Path(__file__).parents[3]
    completed = subprocess.run([str(interpreter), "-I", "-c", script, str(cli_root)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


@pytest.mark.parametrize("snapshot_request", [_begin(), _chunk(), _reconcile(), _cleanup()])
def test_requests_round_trip_without_exposing_paths_or_private_values(snapshot_request) -> None:
    encoded = encode_file_snapshot_request(snapshot_request)
    decoded = decode_file_snapshot_request(encoded)

    assert decoded == snapshot_request
    assert snapshot_request.token.hex() not in repr(decoded)
    if isinstance(snapshot_request, FileSnapshotBeginRequest):
        assert snapshot_request.root_path not in repr(decoded)
        assert snapshot_request.relative_path not in repr(decoded)


def test_only_begin_request_carries_source_selectors_and_token_is_hex() -> None:
    begin = json.loads(encode_file_snapshot_request(_begin()))
    assert set(begin) == {
        "identity",
        "max_bytes",
        "nonce",
        "operation",
        "path",
        "remaining_seconds",
        "root",
        "token",
        "version",
    }
    assert begin["token"] == _TOKEN.hex()

    for request in (_chunk(), _reconcile(), _cleanup()):
        value = json.loads(encode_file_snapshot_request(request))
        assert "root" not in value and "path" not in value


def test_begin_paths_have_independent_utf8_byte_bounds() -> None:
    bounded = "x" * MAX_PATH_BYTES
    root_bounded = decode_file_snapshot_request(encode_file_snapshot_request(_begin(root_path="/" + "x" * 4095)))
    relative_bounded = decode_file_snapshot_request(encode_file_snapshot_request(_begin(relative_path=bounded)))
    assert isinstance(root_bounded, FileSnapshotBeginRequest) and root_bounded.root_path
    assert isinstance(relative_bounded, FileSnapshotBeginRequest) and relative_bounded.relative_path

    with pytest.raises(FileSnapshotRequestError):
        encode_file_snapshot_request(_begin(relative_path="x" * (MAX_PATH_BYTES + 1)))


@pytest.mark.parametrize("path", ["", ".", "..", "/leaf", "nested//leaf", "nested/../leaf"])
def test_begin_refuses_empty_or_nonnormal_relative_source(path: str) -> None:
    with pytest.raises(FileSnapshotRequestError):
        encode_file_snapshot_request(_begin(relative_path=path))


def test_chunk_accepts_empty_terminal_range_and_enforces_declared_length_and_cap() -> None:
    empty = _chunk(ready=_ready(length=0, digest=hashlib.sha256(b"").digest()), offset=0, length=0)
    assert decode_file_snapshot_request(encode_file_snapshot_request(empty)) == empty

    with pytest.raises(FileSnapshotRequestError):
        encode_file_snapshot_request(_chunk(offset=len(_CONTENT), length=1))
    with pytest.raises(FileSnapshotRequestError):
        encode_file_snapshot_request(
            _chunk(
                ready=_ready(length=MAX_SNAPSHOT_CHUNK_BYTES + 1),
                length=MAX_SNAPSHOT_CHUNK_BYTES + 1,
            )
        )


def test_chunk_request_refuses_ready_reference_outside_snapshot_core_binding() -> None:
    wrong_token = ReadyScratchReference(_reference(token=b"z" * 16), _DIGEST, 7, 8)
    wrong_operation = ReadyScratchReference(
        _reference(context=ScratchReceiptContext(ScratchOperation.STAGE, _IDENTITY)),
        _DIGEST,
        7,
        8,
    )

    for ready in (wrong_token, wrong_operation):
        with pytest.raises(FileSnapshotRequestError):
            encode_file_snapshot_request(_chunk(ready=ready))


def test_cleanup_request_refuses_debt_outside_original_token_and_identity() -> None:
    wrong_token = FileSnapshotCleanupRequest(_NONCE, b"z" * 16, _cleanup_debt(_reference()), _IDENTITY, 1.25)
    debt = _cleanup_debt(_reference())
    wrong_identity = FileSnapshotCleanupRequest(
        _NONCE,
        _TOKEN,
        ScratchCleanupDebt(
            debt._name,
            debt._parent,
            debt._directory,
            debt._object,
            debt._receipt,
            debt._receipt_modes,
            _IDENTITY.euid + 1,
            debt._gid,
        ),
        _IDENTITY,
        1.25,
    )

    for snapshot_request in (wrong_token, wrong_identity):
        with pytest.raises(FileSnapshotRequestError):
            encode_file_snapshot_request(snapshot_request)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=1),
        lambda value: value.update(version=True),
        lambda value: value.update(token="A" * 32),
        lambda value: value.update(token="0" * 30),
        lambda value: value.update(remaining_seconds=1),
        lambda value: value.update(remaining_seconds=True),
        lambda value: value.update(remaining_seconds=float("nan")),
        lambda value: value.update(max_bytes=0),
        lambda value: value.update(max_bytes=True),
    ],
)
def test_request_refuses_extra_noncanonical_wrong_typed_and_nonfinite_fields(mutate) -> None:
    value = json.loads(encode_file_snapshot_request(_begin()))
    mutate(value)
    with pytest.raises(FileSnapshotRequestError):
        decode_file_snapshot_request(_json(value, allow_nan=True))


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":1}',
        b'{ "version":1}',
        b"[]",
        b"\xff",
        b"{" + b'"x":' + b'"a"' * 40_000 + b"}",
    ],
    ids=["duplicate-key", "noncanonical-space", "wrong-top-level", "non-ascii", "oversized"],
)
def test_request_refuses_duplicate_noncanonical_invalid_and_oversized_json(data: bytes) -> None:
    with pytest.raises(FileSnapshotRequestError) as raised:
        decode_file_snapshot_request(data)
    assert "a" * 100 not in _exception_details(raised.value)


def test_request_error_chain_does_not_retain_path_or_document() -> None:
    canary = "snapshot-request-canary"
    value = json.loads(encode_file_snapshot_request(_begin()))
    value["path"] = base64.b64encode((canary + "\ud800").encode("utf-8", errors="surrogatepass")).decode("ascii")

    with pytest.raises(FileSnapshotRequestError) as raised:
        decode_file_snapshot_request(_json(value))
    assert canary not in _exception_details(raised.value)

    with pytest.raises(FileSnapshotRequestError) as raised:
        decode_file_snapshot_request(b'{"operation":"snapshot-request-canary"')
    assert canary not in _exception_details(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":"invalid_request","code":"invalid_request"}',
        b'{ "code":"invalid_request"}',
        b'{"code":NaN}',
    ],
    ids=["duplicate-key", "noncanonical", "nonfinite"],
)
def test_response_refuses_duplicate_noncanonical_and_nonfinite_json(body: bytes) -> None:
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_failure(body, _TOKEN, _IDENTITY)


def test_ready_fragment_round_trips_active_reference_and_content_facts() -> None:
    ready = _ready()
    encoded = encode_ready_scratch_reference(ready)

    assert set(encoded) == {"changed_ns", "digest", "modified_ns", "reference"}
    assert decode_ready_scratch_reference(encoded, _TOKEN, snapshot_context(_IDENTITY)) == ready

    historical_shape = dict(encoded)
    historical_shape["reference"] = {"receipt_state": "final"}
    with pytest.raises(ScratchWireError):
        decode_ready_scratch_reference(historical_shape, _TOKEN, snapshot_context(_IDENTITY))


@pytest.mark.parametrize("field", ["modified_ns", "changed_ns"])
def test_ready_fragment_accepts_signed_time_bounds_but_not_booleans(field: str) -> None:
    value = encode_ready_scratch_reference(_ready())
    value[field] = -(2**63)
    decode_ready_scratch_reference(value, _TOKEN, snapshot_context(_IDENTITY))

    value[field] = True
    with pytest.raises(ScratchWireError):
        decode_ready_scratch_reference(value, _TOKEN, snapshot_context(_IDENTITY))


def test_revision_codec_preserves_object_schema_and_closed_validation() -> None:
    revision = _revision()
    encoded = encode_file_revision(revision)

    assert set(encoded) == {
        "changed_ns",
        "device",
        "digest",
        "gid",
        "inode",
        "link_count",
        "mode",
        "modified_ns",
        "size",
        "uid",
        "version",
    }
    assert decode_file_revision(encoded) == revision

    encoded["link_count"] = 2
    with pytest.raises(FileRevisionWireError):
        decode_file_revision(encoded)


@pytest.mark.parametrize(
    "revision",
    [
        _revision(mode=stat.S_IFDIR | 0o700, digest=_DIGEST),
        _revision(mode=stat.S_IFIFO | 0o600, digest=None),
    ],
)
def test_revision_codec_refuses_digest_kind_and_unsupported_kind_combinations(revision: FileRevision) -> None:
    with pytest.raises(FileRevisionWireError):
        decode_file_revision(encode_file_revision(revision))


def test_begin_result_round_trips_absence_or_matching_ready_source() -> None:
    assert (
        parse_file_snapshot_begin_result(
            encode_file_snapshot_begin_result(None),
            _TOKEN,
            _IDENTITY,
            20_000,
        )
        is None
    )

    result = SpoolSnapshot(_ready(), _revision())
    assert (
        parse_file_snapshot_begin_result(
            encode_file_snapshot_begin_result(result),
            _TOKEN,
            _IDENTITY,
            20_000,
        )
        == result
    )


@pytest.mark.parametrize(
    "result",
    [
        SpoolSnapshot(_ready(length=len(_CONTENT) + 1), _revision()),
        SpoolSnapshot(_ready(digest=b"x" * 32), _revision()),
        SpoolSnapshot(_ready(), _revision(digest=None)),
        SpoolSnapshot(_ready(), _revision(mode=stat.S_IFDIR | 0o700, digest=None)),
        SpoolSnapshot(_ready(), _revision(links=2)),
    ],
)
def test_begin_result_refuses_mismatched_or_nonregular_source(result: SpoolSnapshot) -> None:
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_begin_result(
            encode_file_snapshot_begin_result(result),
            _TOKEN,
            _IDENTITY,
            20_000,
        )


def test_begin_result_refuses_source_beyond_requested_bound() -> None:
    body = encode_file_snapshot_begin_result(SpoolSnapshot(_ready(), _revision()))
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_begin_result(body, _TOKEN, _IDENTITY, len(_CONTENT) - 1)


def test_chunk_result_binds_range_length_content_and_digest() -> None:
    result = FileSnapshotChunkResult(4, len(_CONTENT), _CONTENT, _DIGEST)
    decoded = parse_file_snapshot_chunk_result(
        encode_file_snapshot_chunk_result(result),
        result.offset,
        result.length,
        result.data,
    )

    assert decoded == result
    assert repr(_CONTENT) not in repr(decoded)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(offset=5),
        lambda value: value.update(offset=True),
        lambda value: value.update(length=len(_CONTENT) - 1),
        lambda value: value.update(chunk_sha256=(b"x" * 32).hex()),
        lambda value: value.update(extra=1),
    ],
)
def test_chunk_result_refuses_range_length_digest_and_schema_mismatches(mutate) -> None:
    value = json.loads(encode_file_snapshot_chunk_result(FileSnapshotChunkResult(4, len(_CONTENT), _CONTENT, _DIGEST)))
    mutate(value)
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_chunk_result(_json(value), 4, len(_CONTENT), _CONTENT)


@pytest.mark.parametrize("data", [_CONTENT[:-1], _CONTENT + b"x", b"x" * len(_CONTENT)])
def test_chunk_result_refuses_truncated_oversized_or_tampered_data(data: bytes) -> None:
    result = FileSnapshotChunkResult(4, len(_CONTENT), _CONTENT, _DIGEST)
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_chunk_result(
            encode_file_snapshot_chunk_result(result),
            result.offset,
            result.length,
            data,
        )


def test_empty_chunk_result_is_a_verified_typed_payload() -> None:
    digest = hashlib.sha256(b"").digest()
    result = FileSnapshotChunkResult(0, 0, b"", digest)
    assert parse_file_snapshot_chunk_result(encode_file_snapshot_chunk_result(result), 0, 0, b"") == result


def test_reconcile_result_exposes_only_complete_historical_debt_or_uncertainty() -> None:
    historical = ScratchHistoricalOwnership(_reference()._ownership)
    recovered = parse_file_snapshot_reconcile_result(
        encode_file_snapshot_reconcile_result(historical),
        _TOKEN,
        _IDENTITY,
    )
    uncertain = parse_file_snapshot_reconcile_result(
        encode_file_snapshot_reconcile_result(ScratchOwnershipUncertainty()),
        _TOKEN,
        _IDENTITY,
    )

    assert recovered == _cleanup_debt(_reference())
    assert uncertain is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("parent", None),
        ("directory", None),
        ("data", None),
        ("receipt", None),
        ("receipt_state", "creating_or_final"),
    ],
)
def test_reconcile_result_refuses_incomplete_or_nonfinal_cleanup(field: str, replacement: object) -> None:
    historical = ScratchHistoricalOwnership(_reference()._ownership)
    value = json.loads(encode_file_snapshot_reconcile_result(historical))
    value["cleanup"][field] = replacement
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_reconcile_result(_json(value), _TOKEN, _IDENTITY)


def test_cleanup_result_is_one_empty_fixed_success() -> None:
    parse_file_snapshot_cleanup_result(encode_file_snapshot_cleanup_result())
    with pytest.raises(FileSnapshotControlError):
        parse_file_snapshot_cleanup_result(b'{"result":"cleaned"}')


def test_finished_body_is_one_empty_fixed_shape() -> None:
    parse_empty_file_snapshot_body(empty_file_snapshot_body())
    with pytest.raises(FileSnapshotControlError):
        parse_empty_file_snapshot_body(b'{"extra":null}')


def _debt() -> ScratchCleanupDebt:
    return ScratchCleanupDebt(
        scratch_name(_TOKEN),
        _Identity(1, 2),
        _Identity(1, 3),
        _Identity(1, 4),
        _Identity(1, 5),
        (_RECEIPT_MODE,),
        _IDENTITY.euid,
        1002,
    )


@pytest.mark.parametrize("kind", list(SpoolSnapshotFailureKind))
def test_spool_failure_round_trips_closed_kind_and_optional_debt(kind: SpoolSnapshotFailureKind) -> None:
    failure = FileSnapshotFailureControl(FileSnapshotFailureCode.SPOOL, spool_kind=kind, cleanup_debt=_debt())
    assert parse_file_snapshot_failure(encode_file_snapshot_failure(failure), _TOKEN, _IDENTITY) == failure


@pytest.mark.parametrize("kind", list(ScratchFailureKind))
@pytest.mark.parametrize("phase", list(ScratchPhase))
def test_scratch_failure_round_trips_closed_kind_phase_and_optional_debt(
    kind: ScratchFailureKind,
    phase: ScratchPhase,
) -> None:
    failure = FileSnapshotFailureControl(
        FileSnapshotFailureCode.SCRATCH,
        scratch_kind=kind,
        scratch_phase=phase,
        cleanup_debt=_debt(),
    )
    assert parse_file_snapshot_failure(encode_file_snapshot_failure(failure), _TOKEN, _IDENTITY) == failure


@pytest.mark.parametrize(
    "code",
    [
        code
        for code in FileSnapshotFailureCode
        if code not in {FileSnapshotFailureCode.SPOOL, FileSnapshotFailureCode.SCRATCH}
    ],
)
def test_fixed_failure_round_trips_without_arbitrary_diagnostics(code: FileSnapshotFailureCode) -> None:
    failure = FileSnapshotFailureControl(code)
    body = encode_file_snapshot_failure(failure)
    assert set(json.loads(body)) == {"code"}
    assert parse_file_snapshot_failure(body, _TOKEN, _IDENTITY) == failure


def _parse_failure_field(field: str, supplied: object) -> object:
    value: dict[str, object] = {
        "cleanup": None,
        "code": FileSnapshotFailureCode.SCRATCH.value,
        "kind": ScratchFailureKind.CONFLICT.value,
        "phase": ScratchPhase.READ.value,
    }
    value[field] = supplied
    return parse_file_snapshot_failure(_json(value), _TOKEN, _IDENTITY)


@pytest.mark.parametrize(
    ("canary", "invoke"),
    [
        ("snapshot-code-canary", lambda: _parse_failure_field("code", "snapshot-code-canary")),
        ("snapshot-kind-canary", lambda: _parse_failure_field("kind", "snapshot-kind-canary")),
        ("snapshot-phase-canary", lambda: _parse_failure_field("phase", "snapshot-phase-canary")),
    ],
)
def test_response_decoder_does_not_retain_invalid_control_fields(
    canary: str,
    invoke: Callable[[], object],
) -> None:
    with pytest.raises(FileSnapshotControlError) as raised:
        invoke()
    assert canary not in _exception_details(raised.value)


def test_maximum_chunk_request_stays_within_request_cap() -> None:
    request = _chunk(ready=_ready(length=MAX_SNAPSHOT_CHUNK_BYTES), length=MAX_SNAPSHOT_CHUNK_BYTES)
    encoded = encode_file_snapshot_request(request)
    assert len(encoded) <= MAX_REQUEST_BYTES
