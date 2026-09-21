"""Closed stdlib-only protocol for private scratch-backed publication."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ._file_paths import normalized_relative_path, normalized_root
from ._file_publication import (
    Create,
    CreateMetadata,
    Match,
    PublicationFailureKind,
    PublicationPhase,
    Replace,
)
from ._file_publication_wire import (
    BoundPublicationCleanupDebt,
    FilePublicationWireError,
    decode_publication_cleanup_debt,
    encode_publication_cleanup_debt,
)
from ._file_revision_wire import FileRevisionWireError, decode_file_revision, encode_file_revision
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._publication_receipt import _RECORD_MODE, PublicationReceiptFailureKind, PublicationStageCleanupDebt
from ._scratch import ScratchFailureKind, ScratchPhase, ScratchReference
from ._scratch_receipt import ScratchOperation, ScratchReceiptContext
from ._scratch_wire import ScratchWireError, decode_scratch_reference, encode_scratch_reference

if TYPE_CHECKING:
    from ._file_stat import FileRevision

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
_MAX_ID = 2**32 - 1
_COMMON_FIELDS = frozenset(
    {"identity", "nonce", "operation", "path", "reference", "remaining_seconds", "root", "token", "version"}
)
_PUBLISH_FIELDS = _COMMON_FIELDS | {"condition", "create_metadata", "sha256"}
_RECONCILE_FIELDS = _COMMON_FIELDS
_CLEANUP_FIELDS = _COMMON_FIELDS | {"cleanup"}
_LOWER_HEX = frozenset("0123456789abcdef")


class FilePublicationOperation(StrEnum):
    PUBLISH = "publish"
    RECONCILE = "publication_reconcile"
    CLEANUP = "publication_cleanup"


class FilePublicationFailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERSIZED_REQUEST = "oversized_request"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    DEADLINE = "deadline"
    ROOT_REFUSED = "root_refused"
    PARENT_REFUSED = "parent_refused"
    SCRATCH = "scratch"
    PUBLICATION = "publication"
    RECEIPT = "receipt"


class PublicationCleanupState(StrEnum):
    NONE = "none"
    EXACT = "exact"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"


class FilePublicationRequestError(ValueError):
    def __init__(self, failure: FilePublicationFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FilePublicationControlError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid file-publication control body")


@dataclass(frozen=True, slots=True, repr=False)
class FilePublishRequest:
    nonce: str
    token: bytes
    root_path: str
    relative_path: str
    reference: ScratchReference
    digest: bytes
    condition: Create | Replace | Match
    create_metadata: CreateMetadata
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FilePublicationOperation:
        return FilePublicationOperation.PUBLISH


@dataclass(frozen=True, slots=True, repr=False)
class FilePublicationReconcileRequest:
    nonce: str
    token: bytes
    root_path: str
    relative_path: str
    reference: ScratchReference
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FilePublicationOperation:
        return FilePublicationOperation.RECONCILE


@dataclass(frozen=True, slots=True, repr=False)
class FilePublicationCleanupRequest:
    nonce: str
    token: bytes
    root_path: str
    relative_path: str
    reference: ScratchReference
    cleanup_debt: BoundPublicationCleanupDebt
    identity: IdentityExpectation
    remaining_seconds: float | None

    @property
    def operation(self) -> FilePublicationOperation:
        return FilePublicationOperation.CLEANUP


FilePublicationRequest = FilePublishRequest | FilePublicationReconcileRequest | FilePublicationCleanupRequest


@dataclass(frozen=True, slots=True, repr=False)
class FilePublishResult:
    revision: FileRevision
    deadline_exceeded: bool


@dataclass(frozen=True, slots=True, repr=False)
class FilePublicationReconcileResult:
    cleanup_debt: BoundPublicationCleanupDebt | None
    deadline_exceeded: bool


@dataclass(frozen=True, slots=True, repr=False)
class FilePublicationCleanupResult:
    deadline_exceeded: bool


@dataclass(frozen=True, slots=True, repr=False)
class FilePublicationFailureControl:
    code: FilePublicationFailureCode
    scratch_kind: ScratchFailureKind | None = None
    scratch_phase: ScratchPhase | None = None
    publication_kind: PublicationFailureKind | None = None
    publication_phase: PublicationPhase | None = None
    receipt_kind: PublicationReceiptFailureKind | None = None
    cleanup_state: PublicationCleanupState = PublicationCleanupState.NONE
    cleanup_debt: BoundPublicationCleanupDebt | None = None


def publication_context(identity: IdentityExpectation) -> ScratchReceiptContext:
    return ScratchReceiptContext(ScratchOperation.STAGE, identity)


def _reconciled_cleanup_shape(debt: BoundPublicationCleanupDebt) -> bool:
    cleanup = debt._debt
    return (
        isinstance(cleanup, PublicationStageCleanupDebt)
        and not cleanup._stage_removed
        and cleanup._ownership._record_modes == (_RECORD_MODE,)
    )


def _invalid_request() -> FilePublicationRequestError:
    return FilePublicationRequestError(FilePublicationFailureCode.INVALID_REQUEST)


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
        raise FilePublicationControlError
    return value


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object, maximum: int) -> bytes:
    if type(value) is not str:
        raise _invalid_request()
    failed = False
    decoded = b""
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        failed = True
    if failed or len(decoded) > maximum or _encode_bytes(decoded) != value:
        raise _invalid_request()
    return decoded


def _decode_path(value: object, *, root: bool) -> str:
    raw = _decode_bytes(value, MAX_PATH_BYTES)
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid_request() from None
    if not (normalized_root(path) if root else normalized_relative_path(path)):
        raise _invalid_request()
    return path


def _identity(value: object) -> IdentityExpectation:
    try:
        return decode_identity(value)
    except ValueError:
        raise _invalid_request() from None


def _identity_value(identity: IdentityExpectation) -> dict[str, object]:
    return {"egid": identity.egid, "euid": identity.euid, "groups": list(identity.groups)}


def _remaining(value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise _invalid_request()
    return value


def _condition_value(condition: Create | Replace | Match) -> dict[str, object]:
    if isinstance(condition, Create):
        return {"kind": "create"}
    if isinstance(condition, Replace):
        return {"kind": "replace"}
    if isinstance(condition, Match):
        return {"kind": "match", "revision": encode_file_revision(condition.revision)}
    raise FilePublicationWireError


def _decode_condition(value: object) -> Create | Replace | Match:
    if type(value) is not dict:
        raise _invalid_request()
    kind = value.get("kind")
    if kind == "create" and set(value) == {"kind"}:
        return Create()
    if kind == "replace" and set(value) == {"kind"}:
        return Replace()
    if kind == "match" and set(value) == {"kind", "revision"}:
        try:
            revision = decode_file_revision(value["revision"])
        except FileRevisionWireError:
            raise _invalid_request() from None
        if not stat.S_ISREG(revision.stat.mode):
            raise _invalid_request()
        return Match(revision)
    raise _invalid_request()


def _metadata_value(metadata: CreateMetadata) -> dict[str, int]:
    return {"gid": metadata.gid, "mode": metadata.mode, "uid": metadata.uid}


def _decode_metadata(value: object) -> CreateMetadata:
    if type(value) is not dict or set(value) != {"gid", "mode", "uid"}:
        raise _invalid_request()
    uid, gid, mode = value["uid"], value["gid"], value["mode"]
    if type(uid) is not int or not 0 <= uid <= _MAX_ID or type(gid) is not int or not 0 <= gid <= _MAX_ID:
        raise _invalid_request()
    try:
        return CreateMetadata(uid, gid, mode)
    except ValueError:
        raise _invalid_request() from None


def encode_file_publication_request(request: FilePublicationRequest) -> bytes:
    """Encode trusted host values and enforce the complete guest schema."""
    try:
        ownership = request.reference._ownership
        if ownership._token != request.token or ownership._context != publication_context(request.identity):
            raise FilePublicationWireError
        value: dict[str, object] = {
            "identity": _identity_value(request.identity),
            "nonce": request.nonce,
            "operation": request.operation.value,
            "path": _encode_bytes(request.relative_path.encode("utf-8")),
            "reference": encode_scratch_reference(request.reference),
            "remaining_seconds": request.remaining_seconds,
            "root": _encode_bytes(request.root_path.encode("utf-8")),
            "token": request.token.hex(),
            "version": 1,
        }
        if isinstance(request, FilePublishRequest):
            value.update(
                {
                    "condition": _condition_value(request.condition),
                    "create_metadata": _metadata_value(request.create_metadata),
                    "sha256": request.digest.hex(),
                }
            )
        elif isinstance(request, FilePublicationCleanupRequest):
            if request.cleanup_debt._reference != request.reference:
                raise FilePublicationWireError
            value["cleanup"] = encode_publication_cleanup_debt(request.cleanup_debt)
        elif not isinstance(request, FilePublicationReconcileRequest):
            raise TypeError
        encoded = _json_bytes(value)
    except (AttributeError, FilePublicationWireError, ScratchWireError, TypeError, UnicodeEncodeError, ValueError):
        raise _invalid_request() from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FilePublicationRequestError(FilePublicationFailureCode.OVERSIZED_REQUEST)
    decode_file_publication_request(encoded)
    return encoded


def decode_file_publication_request(data: bytes) -> FilePublicationRequest:
    """Validate one canonical request before identity or filesystem access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FilePublicationRequestError(FilePublicationFailureCode.OVERSIZED_REQUEST)
    value = _load_json(data, request=True)
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        raise _invalid_request() from None
    if canonical != data:
        raise _invalid_request()
    operation_value = value.get("operation")
    try:
        operation = FilePublicationOperation(operation_value if type(operation_value) is str else "")
    except ValueError:
        raise _invalid_request() from None
    expected = {
        FilePublicationOperation.PUBLISH: _PUBLISH_FIELDS,
        FilePublicationOperation.RECONCILE: _RECONCILE_FIELDS,
        FilePublicationOperation.CLEANUP: _CLEANUP_FIELDS,
    }[operation]
    if set(value) != expected or value["version"] != 1 or type(value["version"]) is not int:
        raise _invalid_request()
    nonce = value["nonce"]
    token_value = value["token"]
    if type(nonce) is not str or not valid_nonce(nonce):
        raise _invalid_request()
    if type(token_value) is not str or len(token_value) != 32 or any(c not in _LOWER_HEX for c in token_value):
        raise _invalid_request()
    token = bytes.fromhex(token_value)
    identity = _identity(value["identity"])
    try:
        reference = decode_scratch_reference(value["reference"], token, publication_context(identity))
    except ScratchWireError:
        raise _invalid_request() from None
    common = (
        nonce,
        token,
        _decode_path(value["root"], root=True),
        _decode_path(value["path"], root=False),
        reference,
    )
    remaining = _remaining(value["remaining_seconds"])
    if operation is FilePublicationOperation.PUBLISH:
        digest = value["sha256"]
        if type(digest) is not str or len(digest) != 64 or any(c not in _LOWER_HEX for c in digest):
            raise _invalid_request()
        return FilePublishRequest(
            *common,
            bytes.fromhex(digest),
            _decode_condition(value["condition"]),
            _decode_metadata(value["create_metadata"]),
            identity,
            remaining,
        )
    if operation is FilePublicationOperation.RECONCILE:
        return FilePublicationReconcileRequest(*common, identity, remaining)
    try:
        cleanup = decode_publication_cleanup_debt(value["cleanup"], reference)
    except FilePublicationWireError:
        raise _invalid_request() from None
    return FilePublicationCleanupRequest(*common, cleanup, identity, remaining)


def empty_file_publication_body() -> bytes:
    return b"{}"


def parse_empty_file_publication_body(body: bytes) -> None:
    if body != empty_file_publication_body():
        raise FilePublicationControlError


def encode_file_publish_result(result: FilePublishResult) -> bytes:
    return _json_bytes(
        {
            "deadline_exceeded": result.deadline_exceeded,
            "result": "published",
            "revision": encode_file_revision(result.revision),
        }
    )


def parse_file_publish_result(body: bytes, digest: bytes, expected_size: int) -> FilePublishResult:
    value = _canonical_control(body)
    if set(value) != {"deadline_exceeded", "result", "revision"} or value["result"] != "published":
        raise FilePublicationControlError
    if type(value["deadline_exceeded"]) is not bool:
        raise FilePublicationControlError
    try:
        revision = decode_file_revision(value["revision"])
    except FileRevisionWireError:
        raise FilePublicationControlError from None
    if (
        not stat.S_ISREG(revision.stat.mode)
        or revision.digest is None
        or revision.stat.size != expected_size
        or not hmac.compare_digest(revision.digest, digest)
    ):
        raise FilePublicationControlError
    return FilePublishResult(revision, value["deadline_exceeded"])


def encode_file_publication_reconcile_result(result: FilePublicationReconcileResult) -> bytes:
    if result.cleanup_debt is None:
        return _json_bytes({"deadline_exceeded": result.deadline_exceeded, "result": "ownership_uncertain"})
    return _json_bytes(
        {
            "cleanup": encode_publication_cleanup_debt(result.cleanup_debt),
            "deadline_exceeded": result.deadline_exceeded,
            "result": "recovered",
        }
    )


def parse_file_publication_reconcile_result(
    body: bytes,
    reference: ScratchReference,
) -> FilePublicationReconcileResult:
    value = _canonical_control(body)
    deadline = value.get("deadline_exceeded")
    if type(deadline) is not bool:
        raise FilePublicationControlError
    if value.get("result") == "ownership_uncertain" and set(value) == {"deadline_exceeded", "result"}:
        return FilePublicationReconcileResult(None, deadline)
    if value.get("result") != "recovered" or set(value) != {"cleanup", "deadline_exceeded", "result"}:
        raise FilePublicationControlError
    try:
        debt = decode_publication_cleanup_debt(value["cleanup"], reference)
    except FilePublicationWireError:
        raise FilePublicationControlError from None
    if not _reconciled_cleanup_shape(debt):
        raise FilePublicationControlError
    return FilePublicationReconcileResult(debt, deadline)


def encode_file_publication_cleanup_result(result: FilePublicationCleanupResult) -> bytes:
    return _json_bytes({"deadline_exceeded": result.deadline_exceeded, "result": "cleaned"})


def parse_file_publication_cleanup_result(body: bytes) -> FilePublicationCleanupResult:
    value = _canonical_control(body)
    if set(value) != {"deadline_exceeded", "result"} or value["result"] != "cleaned":
        raise FilePublicationControlError
    if type(value["deadline_exceeded"]) is not bool:
        raise FilePublicationControlError
    return FilePublicationCleanupResult(value["deadline_exceeded"])


def _canonical_control(body: bytes) -> dict[str, object]:
    value = _load_json(body, request=False)
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        raise FilePublicationControlError from None
    if canonical != body:
        raise FilePublicationControlError
    return value


def _cleanup_value(failure: FilePublicationFailureControl) -> object:
    if failure.cleanup_state is PublicationCleanupState.NONE and failure.cleanup_debt is None:
        return {"state": "none"}
    if failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN and failure.cleanup_debt is None:
        return {"state": "ownership_uncertain"}
    if failure.cleanup_state is PublicationCleanupState.EXACT and failure.cleanup_debt is not None:
        return {"debt": encode_publication_cleanup_debt(failure.cleanup_debt), "state": "exact"}
    raise FilePublicationControlError


def _parse_cleanup(
    value: object, reference: ScratchReference
) -> tuple[PublicationCleanupState, BoundPublicationCleanupDebt | None]:
    if type(value) is not dict:
        raise FilePublicationControlError
    if value == {"state": "none"}:
        return PublicationCleanupState.NONE, None
    if value == {"state": "ownership_uncertain"}:
        return PublicationCleanupState.OWNERSHIP_UNCERTAIN, None
    if set(value) == {"debt", "state"} and value["state"] == "exact":
        try:
            return PublicationCleanupState.EXACT, decode_publication_cleanup_debt(value["debt"], reference)
        except FilePublicationWireError:
            raise FilePublicationControlError from None
    raise FilePublicationControlError


def encode_file_publication_failure(failure: FilePublicationFailureControl) -> bytes:
    value: dict[str, object] = {"code": failure.code.value}
    details = (
        failure.scratch_kind,
        failure.scratch_phase,
        failure.publication_kind,
        failure.publication_phase,
        failure.receipt_kind,
    )
    if failure.code is FilePublicationFailureCode.SCRATCH:
        if failure.scratch_kind is None or failure.scratch_phase is None or any(x is not None for x in details[2:]):
            raise FilePublicationControlError
        if failure.cleanup_state is not PublicationCleanupState.NONE or failure.cleanup_debt is not None:
            raise FilePublicationControlError
        value.update({"kind": failure.scratch_kind.value, "phase": failure.scratch_phase.value})
    elif failure.code is FilePublicationFailureCode.PUBLICATION:
        if (
            failure.publication_kind is None
            or failure.publication_phase is None
            or any(x is not None for x in details[:2] + details[4:])
        ):
            raise FilePublicationControlError
        value.update(
            {
                "cleanup": _cleanup_value(failure),
                "kind": failure.publication_kind.value,
                "phase": failure.publication_phase.value,
            }
        )
    elif failure.code is FilePublicationFailureCode.RECEIPT:
        if failure.receipt_kind is None or any(x is not None for x in details[:4]):
            raise FilePublicationControlError
        value.update({"cleanup": _cleanup_value(failure), "kind": failure.receipt_kind.value})
    elif (
        any(x is not None for x in details)
        or failure.cleanup_state is not PublicationCleanupState.NONE
        or failure.cleanup_debt is not None
    ):
        raise FilePublicationControlError
    return _json_bytes(value)


def parse_file_publication_failure(
    body: bytes,
    reference: ScratchReference,
) -> FilePublicationFailureControl:
    value = _canonical_control(body)
    code_value = value.get("code")
    try:
        code = FilePublicationFailureCode(code_value if type(code_value) is str else "")
    except ValueError:
        raise FilePublicationControlError from None
    if code is FilePublicationFailureCode.SCRATCH:
        if set(value) != {"code", "kind", "phase"}:
            raise FilePublicationControlError
        try:
            return FilePublicationFailureControl(
                code,
                scratch_kind=ScratchFailureKind(value["kind"]),
                scratch_phase=ScratchPhase(value["phase"]),
            )
        except (TypeError, ValueError):
            raise FilePublicationControlError from None
    if code is FilePublicationFailureCode.PUBLICATION:
        if set(value) != {"cleanup", "code", "kind", "phase"}:
            raise FilePublicationControlError
        try:
            state, debt = _parse_cleanup(value["cleanup"], reference)
            return FilePublicationFailureControl(
                code,
                publication_kind=PublicationFailureKind(value["kind"]),
                publication_phase=PublicationPhase(value["phase"]),
                cleanup_state=state,
                cleanup_debt=debt,
            )
        except (TypeError, ValueError):
            raise FilePublicationControlError from None
    if code is FilePublicationFailureCode.RECEIPT:
        if set(value) != {"cleanup", "code", "kind"}:
            raise FilePublicationControlError
        try:
            state, debt = _parse_cleanup(value["cleanup"], reference)
            return FilePublicationFailureControl(
                code,
                receipt_kind=PublicationReceiptFailureKind(value["kind"]),
                cleanup_state=state,
                cleanup_debt=debt,
            )
        except (TypeError, ValueError):
            raise FilePublicationControlError from None
    if set(value) != {"code"}:
        raise FilePublicationControlError
    return FilePublicationFailureControl(code)
