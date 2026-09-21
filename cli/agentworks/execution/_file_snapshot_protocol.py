"""Closed stdlib-only protocol for private snapshot transfer and recovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_paths import normalized_relative_path, normalized_root
from ._file_revision_wire import FileRevisionWireError, decode_file_revision, encode_file_revision
from ._file_spool import SpoolSnapshot, SpoolSnapshotFailureKind
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._scratch import ReadyScratchReference, ScratchFailureKind, ScratchPhase, _cleanup_debt
from ._scratch_receipt import (
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnershipUncertainty,
    ScratchReceiptContext,
    scratch_name,
)
from ._scratch_wire import (
    ScratchWireError,
    decode_cleanup_debt,
    decode_ready_scratch_reference,
    encode_cleanup_debt,
    encode_ready_scratch_reference,
)

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
MAX_SNAPSHOT_CHUNK_BYTES = 12 * 1_024
_MAX_LENGTH = (1 << 63) - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_COMMON_FIELDS = frozenset({"identity", "nonce", "operation", "remaining_seconds", "token", "version"})
_BEGIN_FIELDS = _COMMON_FIELDS | {"max_bytes", "path", "root"}
_CHUNK_FIELDS = _COMMON_FIELDS | {"length", "offset", "ready"}
_RECONCILE_FIELDS = _COMMON_FIELDS
_CLEANUP_FIELDS = _COMMON_FIELDS | {"cleanup"}


class FileSnapshotOperation(StrEnum):
    BEGIN = "snapshot_begin"
    CHUNK = "snapshot_chunk"
    RECONCILE = "snapshot_reconcile"
    CLEANUP = "snapshot_cleanup"


class FileSnapshotFailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERSIZED_REQUEST = "oversized_request"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    ROOT_REFUSED = "root_refused"
    SCRATCH_ROOT_REFUSED = "scratch_root_refused"
    SPOOL = "spool"
    SCRATCH = "scratch"


class FileSnapshotRequestError(ValueError):
    """A snapshot request violated its closed schema without retaining it."""

    def __init__(self, failure: FileSnapshotFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileSnapshotControlError(ValueError):
    """A snapshot response body violated its closed schema."""

    def __init__(self) -> None:
        super().__init__("invalid file-snapshot control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotBeginRequest:
    nonce: str
    token: bytes
    root_path: str
    relative_path: str
    max_bytes: int
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileSnapshotOperation:
        return FileSnapshotOperation.BEGIN


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotChunkRequest:
    nonce: str
    token: bytes
    ready: ReadyScratchReference
    offset: int
    length: int
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileSnapshotOperation:
        return FileSnapshotOperation.CHUNK


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotReconcileRequest:
    nonce: str
    token: bytes
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileSnapshotOperation:
        return FileSnapshotOperation.RECONCILE


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotCleanupRequest:
    nonce: str
    token: bytes
    cleanup_debt: ScratchCleanupDebt
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileSnapshotOperation:
        return FileSnapshotOperation.CLEANUP


FileSnapshotRequest = (
    FileSnapshotBeginRequest | FileSnapshotChunkRequest | FileSnapshotReconcileRequest | FileSnapshotCleanupRequest
)


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotChunkResult:
    offset: int
    length: int
    data: bytes
    chunk_digest: bytes


@dataclass(frozen=True, slots=True, repr=False)
class FileSnapshotFailureControl:
    code: FileSnapshotFailureCode
    spool_kind: SpoolSnapshotFailureKind | None = None
    scratch_kind: ScratchFailureKind | None = None
    scratch_phase: ScratchPhase | None = None
    cleanup_debt: ScratchCleanupDebt | None = None


def snapshot_context(identity: IdentityExpectation) -> ScratchReceiptContext:
    """Build the sole scratch context accepted by this protocol family."""
    return ScratchReceiptContext(ScratchOperation.SNAPSHOT, identity)


def _historical_cleanup_shape(debt: ScratchCleanupDebt) -> bool:
    return (
        debt._parent is not None
        and debt._directory is not None
        and debt._object is not None
        and debt._receipt is not None
        and debt._receipt_modes == (_RECEIPT_MODE,)
    )


def _invalid_request() -> FileSnapshotRequestError:
    return FileSnapshotRequestError(FileSnapshotFailureCode.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _load_json(data: bytes, *, request: bool) -> dict[str, object]:
    failed = False
    value: Any = None
    maximum = MAX_REQUEST_BYTES if request else 4_096
    if type(data) is not bytes or len(data) > maximum:
        failed = True
    else:
        try:
            value = json.loads(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            failed = True
    if failed or type(value) is not dict:
        if request:
            raise _invalid_request()
        raise FileSnapshotControlError
    return value


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_request_bytes(value: object, maximum: int, *, exact: int | None = None) -> bytes:
    if type(value) is not str:
        raise _invalid_request()
    failed = False
    decoded = b""
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        failed = True
    if (
        failed
        or len(decoded) > maximum
        or (exact is not None and len(decoded) != exact)
        or _encode_bytes(decoded) != value
    ):
        raise _invalid_request()
    return decoded


def _decode_path(value: object, *, root: bool) -> str:
    decoded = _decode_request_bytes(value, MAX_PATH_BYTES)
    failed = False
    text = ""
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed or not (normalized_root(text) if root else normalized_relative_path(text)):
        raise _invalid_request()
    return text


def _encode_token(token: bytes) -> str:
    return token.hex()


def _decode_token(value: object) -> bytes:
    if type(value) is not str or len(value) != 32 or any(character not in _LOWER_HEX for character in value):
        raise _invalid_request()
    return bytes.fromhex(value)


def _identity(value: object) -> IdentityExpectation:
    failed = False
    identity: IdentityExpectation | None = None
    try:
        identity = decode_identity(value)
    except ValueError:
        failed = True
    if failed or identity is None:
        raise _invalid_request()
    return identity


def _identity_value(identity: IdentityExpectation) -> dict[str, object]:
    return {"egid": identity.egid, "euid": identity.euid, "groups": list(identity.groups)}


def _bounded_request_integer(value: object, maximum: int, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid_request()
    return value


def _remaining(value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise _invalid_request()
    return value


def encode_file_snapshot_request(request: FileSnapshotRequest) -> bytes:
    """Encode trusted host values and enforce the complete guest schema."""
    failed = False
    encoded = b""
    try:
        common: dict[str, object] = {
            "identity": _identity_value(request.identity),
            "nonce": request.nonce,
            "operation": request.operation.value,
            "remaining_seconds": request.remaining_seconds,
            "token": _encode_token(request.token),
            "version": 1,
        }
        if isinstance(request, FileSnapshotBeginRequest):
            common.update(
                {
                    "max_bytes": request.max_bytes,
                    "path": _encode_bytes(request.relative_path.encode("utf-8")),
                    "root": _encode_bytes(request.root_path.encode("utf-8")),
                }
            )
        elif isinstance(request, FileSnapshotChunkRequest):
            reference = request.ready._reference._ownership
            if reference._token != request.token or reference._context != snapshot_context(request.identity):
                raise ScratchWireError
            common.update(
                {
                    "length": request.length,
                    "offset": request.offset,
                    "ready": encode_ready_scratch_reference(request.ready),
                }
            )
        elif isinstance(request, FileSnapshotReconcileRequest):
            pass
        elif isinstance(request, FileSnapshotCleanupRequest):
            if (
                request.cleanup_debt._name != scratch_name(request.token)
                or request.cleanup_debt._uid != request.identity.euid
            ):
                raise ScratchWireError
            common["cleanup"] = encode_cleanup_debt(request.cleanup_debt)
        else:
            raise TypeError
        encoded = _json_bytes(common)
    except (AttributeError, ScratchWireError, TypeError, UnicodeEncodeError, ValueError):
        failed = True
    if failed:
        raise _invalid_request()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FileSnapshotRequestError(FileSnapshotFailureCode.OVERSIZED_REQUEST)
    decode_file_snapshot_request(encoded)
    return encoded


def decode_file_snapshot_request(data: bytes) -> FileSnapshotRequest:
    """Validate one untrusted canonical request before identity or path access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileSnapshotRequestError(FileSnapshotFailureCode.OVERSIZED_REQUEST)
    value = _load_json(data, request=True)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != data:
        raise _invalid_request()
    operation_value = value.get("operation")
    failed = False
    operation = FileSnapshotOperation.BEGIN
    try:
        operation = FileSnapshotOperation(operation_value if type(operation_value) is str else "")
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise _invalid_request()
    expected_fields = {
        FileSnapshotOperation.BEGIN: _BEGIN_FIELDS,
        FileSnapshotOperation.CHUNK: _CHUNK_FIELDS,
        FileSnapshotOperation.RECONCILE: _RECONCILE_FIELDS,
        FileSnapshotOperation.CLEANUP: _CLEANUP_FIELDS,
    }[operation]
    if set(value) != expected_fields or value["version"] != 1 or type(value["version"]) is not int:
        raise _invalid_request()
    nonce = value["nonce"]
    if type(nonce) is not str or not valid_nonce(nonce):
        raise _invalid_request()
    token = _decode_token(value["token"])
    identity = _identity(value["identity"])
    remaining = _remaining(value["remaining_seconds"])
    if operation is FileSnapshotOperation.BEGIN:
        return FileSnapshotBeginRequest(
            nonce,
            token,
            _decode_path(value["root"], root=True),
            _decode_path(value["path"], root=False),
            _bounded_request_integer(value["max_bytes"], _MAX_LENGTH, positive=True),
            identity,
            remaining,
        )
    context = snapshot_context(identity)
    if operation is FileSnapshotOperation.RECONCILE:
        return FileSnapshotReconcileRequest(nonce, token, identity, remaining)
    if operation is FileSnapshotOperation.CLEANUP:
        failed = False
        cleanup: ScratchCleanupDebt | None = None
        try:
            cleanup = decode_cleanup_debt(value["cleanup"], token, context)
        except ScratchWireError:
            failed = True
        if failed or cleanup is None:
            raise _invalid_request()
        return FileSnapshotCleanupRequest(nonce, token, cleanup, identity, remaining)
    failed = False
    ready: ReadyScratchReference | None = None
    try:
        ready = decode_ready_scratch_reference(value["ready"], token, context)
    except ScratchWireError:
        failed = True
    if failed or ready is None:
        raise _invalid_request()
    offset = _bounded_request_integer(value["offset"], _MAX_LENGTH)
    length = _bounded_request_integer(value["length"], MAX_SNAPSHOT_CHUNK_BYTES)
    declared_length = ready._reference._ownership._length
    if offset > declared_length or length > declared_length - offset:
        raise _invalid_request()
    return FileSnapshotChunkRequest(nonce, token, ready, offset, length, identity, remaining)


def empty_file_snapshot_body() -> bytes:
    return b"{}"


def parse_empty_file_snapshot_body(body: bytes) -> None:
    if body != empty_file_snapshot_body():
        raise FileSnapshotControlError


def encode_file_snapshot_begin_result(result: SpoolSnapshot | None) -> bytes:
    if result is None:
        return b'{"result":"absent"}'
    return _json_bytes(
        {
            "ready": encode_ready_scratch_reference(result.ready),
            "result": "ready",
            "source": encode_file_revision(result.source),
        }
    )


def parse_file_snapshot_begin_result(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
    max_bytes: int,
) -> SpoolSnapshot | None:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileSnapshotControlError
    result = value.get("result")
    if result == "absent" and set(value) == {"result"}:
        return None
    if result != "ready" or set(value) != {"ready", "result", "source"}:
        raise FileSnapshotControlError
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_LENGTH:
        raise FileSnapshotControlError
    failed = False
    ready: ReadyScratchReference | None = None
    source = None
    try:
        ready = decode_ready_scratch_reference(value["ready"], token, snapshot_context(identity))
        source = decode_file_revision(value["source"])
    except (FileRevisionWireError, ScratchWireError):
        failed = True
    if failed or ready is None or source is None:
        raise FileSnapshotControlError
    source_digest = source.digest
    length = ready._reference._ownership._length
    if (
        source_digest is None
        or not stat.S_ISREG(source.stat.mode)
        or source.stat.link_count != 1
        or source.stat.size > max_bytes
        or length != source.stat.size
        or not hmac.compare_digest(ready._digest, source_digest)
    ):
        raise FileSnapshotControlError
    return SpoolSnapshot(ready, source)


def encode_file_snapshot_chunk_result(result: FileSnapshotChunkResult) -> bytes:
    return _json_bytes(
        {
            "chunk_sha256": result.chunk_digest.hex(),
            "length": result.length,
            "offset": result.offset,
        }
    )


def parse_file_snapshot_chunk_result(
    body: bytes,
    requested_offset: int,
    requested_length: int,
    data: bytes,
) -> FileSnapshotChunkResult:
    if (
        type(requested_offset) is not int
        or not 0 <= requested_offset <= _MAX_LENGTH
        or type(requested_length) is not int
        or not 0 <= requested_length <= MAX_SNAPSHOT_CHUNK_BYTES
    ):
        raise FileSnapshotControlError
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body or set(value) != {"chunk_sha256", "length", "offset"}:
        raise FileSnapshotControlError
    if value["offset"] != requested_offset or type(value["offset"]) is not int:
        raise FileSnapshotControlError
    if value["length"] != requested_length or type(value["length"]) is not int:
        raise FileSnapshotControlError
    digest_value = value["chunk_sha256"]
    if (
        type(digest_value) is not str
        or len(digest_value) != 64
        or any(character not in _LOWER_HEX for character in digest_value)
    ):
        raise FileSnapshotControlError
    if type(data) is not bytes or len(data) != requested_length:
        raise FileSnapshotControlError
    digest = bytes.fromhex(digest_value)
    if not hmac.compare_digest(hashlib.sha256(data).digest(), digest):
        raise FileSnapshotControlError
    return FileSnapshotChunkResult(requested_offset, requested_length, data, digest)


def encode_file_snapshot_reconcile_result(
    result: ScratchHistoricalOwnership | ScratchOwnershipUncertainty,
) -> bytes:
    if isinstance(result, ScratchOwnershipUncertainty):
        return b'{"result":"ownership_uncertain"}'
    return _json_bytes({"cleanup": encode_cleanup_debt(_cleanup_debt(result)), "result": "recovered"})


def parse_file_snapshot_reconcile_result(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> ScratchCleanupDebt | None:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    cleanup: ScratchCleanupDebt | None = None
    try:
        canonical = _json_bytes(value)
        result = value.get("result")
        if result == "recovered" and set(value) == {"cleanup", "result"}:
            cleanup = decode_cleanup_debt(value["cleanup"], token, snapshot_context(identity))
            if not _historical_cleanup_shape(cleanup):
                failed = True
        elif result != "ownership_uncertain" or set(value) != {"result"}:
            failed = True
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileSnapshotControlError
    return cleanup


def encode_file_snapshot_cleanup_result() -> bytes:
    return b"{}"


def parse_file_snapshot_cleanup_result(body: bytes) -> None:
    if body != encode_file_snapshot_cleanup_result():
        raise FileSnapshotControlError


def encode_file_snapshot_failure(failure: FileSnapshotFailureControl) -> bytes:
    try:
        value: dict[str, object] = {"code": failure.code.value}
        details = (failure.spool_kind, failure.scratch_kind, failure.scratch_phase, failure.cleanup_debt)
        if failure.code is FileSnapshotFailureCode.SPOOL:
            if failure.spool_kind is None or failure.scratch_kind is not None or failure.scratch_phase is not None:
                raise FileSnapshotControlError
            value.update(
                {
                    "cleanup": None if failure.cleanup_debt is None else encode_cleanup_debt(failure.cleanup_debt),
                    "kind": failure.spool_kind.value,
                }
            )
        elif failure.code is FileSnapshotFailureCode.SCRATCH:
            if failure.spool_kind is not None or failure.scratch_kind is None or failure.scratch_phase is None:
                raise FileSnapshotControlError
            value.update(
                {
                    "cleanup": None if failure.cleanup_debt is None else encode_cleanup_debt(failure.cleanup_debt),
                    "kind": failure.scratch_kind.value,
                    "phase": failure.scratch_phase.value,
                }
            )
        elif any(detail is not None for detail in details):
            raise FileSnapshotControlError
        return _json_bytes(value)
    except (AttributeError, ScratchWireError, TypeError, ValueError):
        raise FileSnapshotControlError from None


def parse_file_snapshot_failure(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> FileSnapshotFailureControl:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    code = FileSnapshotFailureCode.INVALID_REQUEST
    try:
        canonical = _json_bytes(value)
        code_value = value.get("code")
        code = FileSnapshotFailureCode(code_value if type(code_value) is str else "")
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileSnapshotControlError
    if code not in {FileSnapshotFailureCode.SPOOL, FileSnapshotFailureCode.SCRATCH}:
        if set(value) != {"code"}:
            raise FileSnapshotControlError
        return FileSnapshotFailureControl(code)
    context = snapshot_context(identity)
    cleanup: ScratchCleanupDebt | None = None
    if code is FileSnapshotFailureCode.SPOOL:
        if set(value) != {"cleanup", "code", "kind"}:
            raise FileSnapshotControlError
        failed = False
        kind = SpoolSnapshotFailureKind.IO
        try:
            kind = SpoolSnapshotFailureKind(value["kind"])
            cleanup = None if value["cleanup"] is None else decode_cleanup_debt(value["cleanup"], token, context)
        except (ScratchWireError, TypeError, ValueError):
            failed = True
        if failed:
            raise FileSnapshotControlError
        return FileSnapshotFailureControl(code, spool_kind=kind, cleanup_debt=cleanup)
    if set(value) != {"cleanup", "code", "kind", "phase"}:
        raise FileSnapshotControlError
    failed = False
    scratch_kind = ScratchFailureKind.IO
    phase = ScratchPhase.BEGIN
    try:
        scratch_kind = ScratchFailureKind(value["kind"])
        phase = ScratchPhase(value["phase"])
        cleanup = None if value["cleanup"] is None else decode_cleanup_debt(value["cleanup"], token, context)
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed:
        raise FileSnapshotControlError
    return FileSnapshotFailureControl(code, scratch_kind=scratch_kind, scratch_phase=phase, cleanup_debt=cleanup)
