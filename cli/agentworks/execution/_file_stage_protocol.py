"""Closed stdlib-only protocol for private stage transfer and recovery."""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_paths import normalized_relative_path, normalized_root
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._scratch import ScratchFailureKind, ScratchPhase, ScratchReference, _cleanup_debt
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
    decode_scratch_reference,
    encode_cleanup_debt,
    encode_scratch_reference,
)

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
MAX_STAGE_CHUNK_BYTES = 12 * 1_024
_MAX_OFFSET = (1 << 63) - 1
_COMMON_FIELDS = frozenset({"identity", "nonce", "operation", "path", "remaining_seconds", "root", "token", "version"})
_BEGIN_FIELDS = _COMMON_FIELDS | {"expected_length"}
_CHUNK_FIELDS = _COMMON_FIELDS | {"chunk_sha256", "data", "offset", "reference"}
_RECONCILE_FIELDS = _COMMON_FIELDS
_CLEANUP_FIELDS = _COMMON_FIELDS | {"cleanup"}


class FileStageOperation(StrEnum):
    BEGIN = "stage_begin"
    CHUNK = "stage_chunk"
    RECONCILE = "stage_reconcile"
    CLEANUP = "stage_cleanup"


class FileStageFailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERSIZED_REQUEST = "oversized_request"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    DEADLINE = "deadline"
    ROOT_REFUSED = "root_refused"
    PARENT_REFUSED = "parent_refused"
    SCRATCH = "scratch"


class FileStageRequestError(ValueError):
    """A stage request violated its closed schema without retaining it."""

    def __init__(self, failure: FileStageFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileStageControlError(ValueError):
    """A stage response body violated its closed schema."""

    def __init__(self) -> None:
        super().__init__("invalid file-stage control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileStageBeginRequest:
    nonce: str
    root_path: str
    relative_path: str
    token: bytes
    expected_length: int
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileStageOperation:
        return FileStageOperation.BEGIN


@dataclass(frozen=True, slots=True, repr=False)
class FileStageChunkRequest:
    nonce: str
    root_path: str
    relative_path: str
    token: bytes
    reference: ScratchReference
    offset: int
    data: bytes
    chunk_digest: bytes
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileStageOperation:
        return FileStageOperation.CHUNK


@dataclass(frozen=True, slots=True, repr=False)
class FileStageReconcileRequest:
    nonce: str
    root_path: str
    relative_path: str
    token: bytes
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileStageOperation:
        return FileStageOperation.RECONCILE


@dataclass(frozen=True, slots=True, repr=False)
class FileStageCleanupRequest:
    nonce: str
    root_path: str
    relative_path: str
    token: bytes
    cleanup_debt: ScratchCleanupDebt
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FileStageOperation:
        return FileStageOperation.CLEANUP


FileStageRequest = FileStageBeginRequest | FileStageChunkRequest | FileStageReconcileRequest | FileStageCleanupRequest


@dataclass(frozen=True, slots=True, repr=False)
class FileStageFailureControl:
    code: FileStageFailureCode
    kind: ScratchFailureKind | None = None
    phase: ScratchPhase | None = None
    cleanup_debt: ScratchCleanupDebt | None = None


def stage_context(identity: IdentityExpectation) -> ScratchReceiptContext:
    """Build the sole scratch context accepted by this protocol family."""
    return ScratchReceiptContext(ScratchOperation.STAGE, identity)


def _historical_cleanup_shape(debt: ScratchCleanupDebt) -> bool:
    return (
        debt._parent is not None
        and debt._directory is not None
        and debt._object is not None
        and debt._receipt is not None
        and debt._receipt_modes == (_RECEIPT_MODE,)
    )


def _invalid_request() -> FileStageRequestError:
    return FileStageRequestError(FileStageFailureCode.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _load_json(data: bytes, *, request: bool) -> dict[str, object]:
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed:
        if request:
            raise _invalid_request()
        raise FileStageControlError
    if type(value) is not dict:
        if request:
            raise _invalid_request()
        raise FileStageControlError
    return value


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object, maximum: int, *, exact: int | None = None) -> bytes:
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
    decoded = _decode_bytes(value, MAX_PATH_BYTES)
    failed = False
    text = ""
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed:
        raise _invalid_request()
    valid = normalized_root(text) if root else normalized_relative_path(text)
    if not valid:
        raise _invalid_request()
    return text


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


def _bounded_integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise _invalid_request()
    return value


def _remaining(value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise _invalid_request()
    return value


def encode_file_stage_request(request: FileStageRequest) -> bytes:
    """Encode trusted host values and enforce the complete guest schema."""
    failed = False
    encoded = b""
    try:
        common: dict[str, object] = {
            "identity": _identity_value(request.identity),
            "nonce": request.nonce,
            "operation": request.operation.value,
            "path": _encode_bytes(request.relative_path.encode("utf-8")),
            "remaining_seconds": request.remaining_seconds,
            "root": _encode_bytes(request.root_path.encode("utf-8")),
            "token": _encode_bytes(request.token),
            "version": 1,
        }
        if isinstance(request, FileStageBeginRequest):
            common["expected_length"] = request.expected_length
        elif isinstance(request, FileStageChunkRequest):
            ownership = request.reference._ownership
            if ownership._token != request.token or ownership._context != stage_context(request.identity):
                raise ScratchWireError
            common.update(
                {
                    "chunk_sha256": _encode_bytes(request.chunk_digest),
                    "data": _encode_bytes(request.data),
                    "offset": request.offset,
                    "reference": encode_scratch_reference(request.reference),
                }
            )
        elif isinstance(request, FileStageReconcileRequest):
            pass
        elif isinstance(request, FileStageCleanupRequest):
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
        raise FileStageRequestError(FileStageFailureCode.OVERSIZED_REQUEST)
    decode_file_stage_request(encoded)
    return encoded


def decode_file_stage_request(data: bytes) -> FileStageRequest:
    """Validate one untrusted canonical request before identity or path access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileStageRequestError(FileStageFailureCode.OVERSIZED_REQUEST)
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
    operation = FileStageOperation.BEGIN
    try:
        operation = FileStageOperation(operation_value if type(operation_value) is str else "")
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise _invalid_request()
    expected_fields = {
        FileStageOperation.BEGIN: _BEGIN_FIELDS,
        FileStageOperation.CHUNK: _CHUNK_FIELDS,
        FileStageOperation.RECONCILE: _RECONCILE_FIELDS,
        FileStageOperation.CLEANUP: _CLEANUP_FIELDS,
    }[operation]
    if set(value) != expected_fields or value["version"] != 1 or type(value["version"]) is not int:
        raise _invalid_request()
    nonce = value["nonce"]
    if type(nonce) is not str or not valid_nonce(nonce):
        raise _invalid_request()
    root_path = _decode_path(value["root"], root=True)
    relative_path = _decode_path(value["path"], root=False)
    token = _decode_bytes(value["token"], 16, exact=16)
    identity = _identity(value["identity"])
    remaining = _remaining(value["remaining_seconds"])
    if operation is FileStageOperation.BEGIN:
        return FileStageBeginRequest(
            nonce,
            root_path,
            relative_path,
            token,
            _bounded_integer(value["expected_length"], _MAX_OFFSET),
            identity,
            remaining,
        )
    context = stage_context(identity)
    if operation is FileStageOperation.RECONCILE:
        return FileStageReconcileRequest(nonce, root_path, relative_path, token, identity, remaining)
    if operation is FileStageOperation.CLEANUP:
        failed = False
        cleanup: ScratchCleanupDebt | None = None
        try:
            cleanup = decode_cleanup_debt(value["cleanup"], token, context)
        except ScratchWireError:
            failed = True
        if failed or cleanup is None:
            raise _invalid_request()
        return FileStageCleanupRequest(nonce, root_path, relative_path, token, cleanup, identity, remaining)
    failed = False
    reference: ScratchReference | None = None
    try:
        reference = decode_scratch_reference(value["reference"], token, context)
    except ScratchWireError:
        failed = True
    if failed or reference is None:
        raise _invalid_request()
    data_value = _decode_bytes(value["data"], MAX_STAGE_CHUNK_BYTES)
    if not data_value:
        raise _invalid_request()
    return FileStageChunkRequest(
        nonce,
        root_path,
        relative_path,
        token,
        reference,
        _bounded_integer(value["offset"], _MAX_OFFSET),
        data_value,
        _decode_bytes(value["chunk_sha256"], 32, exact=32),
        identity,
        remaining,
    )


def empty_file_stage_body() -> bytes:
    return b"{}"


def parse_empty_file_stage_body(body: bytes) -> None:
    if body != empty_file_stage_body():
        raise FileStageControlError


def encode_file_stage_begin_result(
    reference: ScratchReference,
) -> bytes:
    failed = False
    body = b""
    try:
        body = _json_bytes({"reference": encode_scratch_reference(reference)})
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed:
        raise FileStageControlError
    return body


def parse_file_stage_begin_result(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> ScratchReference:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    reference: ScratchReference | None = None
    try:
        canonical = _json_bytes(value)
        reference = decode_scratch_reference(value.get("reference"), token, stage_context(identity))
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed or reference is None or set(value) != {"reference"} or canonical != body:
        raise FileStageControlError
    return reference


def encode_file_stage_chunk_result() -> bytes:
    return b'{"result":"accepted"}'


def parse_file_stage_chunk_result(body: bytes) -> None:
    if body != encode_file_stage_chunk_result():
        raise FileStageControlError


def encode_file_stage_reconcile_result(
    result: ScratchHistoricalOwnership | ScratchOwnershipUncertainty,
) -> bytes:
    if isinstance(result, ScratchOwnershipUncertainty):
        return b'{"result":"ownership_uncertain"}'
    return _json_bytes({"cleanup": encode_cleanup_debt(_cleanup_debt(result)), "result": "recovered"})


def parse_file_stage_reconcile_result(
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
            cleanup = decode_cleanup_debt(value["cleanup"], token, stage_context(identity))
            if not _historical_cleanup_shape(cleanup):
                failed = True
        elif result != "ownership_uncertain" or set(value) != {"result"}:
            failed = True
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileStageControlError
    return cleanup


def encode_file_stage_cleanup_result() -> bytes:
    return b'{"result":"cleaned"}'


def parse_file_stage_cleanup_result(body: bytes) -> None:
    if body != encode_file_stage_cleanup_result():
        raise FileStageControlError


def encode_file_stage_failure(
    failure: FileStageFailureControl,
) -> bytes:
    value: dict[str, object] = {"code": failure.code.value}
    if failure.code is FileStageFailureCode.SCRATCH:
        if failure.kind is None or failure.phase is None:
            raise FileStageControlError
        failed = False
        cleanup: dict[str, object] | None = None
        try:
            cleanup = None if failure.cleanup_debt is None else encode_cleanup_debt(failure.cleanup_debt)
        except ScratchWireError:
            failed = True
        if failed:
            raise FileStageControlError
        value.update({"cleanup": cleanup, "kind": failure.kind.value, "phase": failure.phase.value})
    elif failure.kind is not None or failure.phase is not None or failure.cleanup_debt is not None:
        raise FileStageControlError
    return _json_bytes(value)


def parse_file_stage_failure(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> FileStageFailureControl:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    code = FileStageFailureCode.INVALID_REQUEST
    try:
        canonical = _json_bytes(value)
        code_value = value.get("code")
        code = FileStageFailureCode(code_value if type(code_value) is str else "")
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileStageControlError
    if code is not FileStageFailureCode.SCRATCH:
        if set(value) != {"code"}:
            raise FileStageControlError
        return FileStageFailureControl(code)
    if set(value) != {"cleanup", "code", "kind", "phase"}:
        raise FileStageControlError
    failed = False
    kind = ScratchFailureKind.UNSUPPORTED
    phase = ScratchPhase.BEGIN
    cleanup: ScratchCleanupDebt | None = None
    try:
        kind = ScratchFailureKind(value["kind"])
        phase = ScratchPhase(value["phase"])
        cleanup = (
            None if value["cleanup"] is None else decode_cleanup_debt(value["cleanup"], token, stage_context(identity))
        )
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed:
        raise FileStageControlError
    return FileStageFailureControl(code, kind, phase, cleanup)
