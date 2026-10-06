"""Host request encoding and response parsing for private snapshot exchanges."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
from typing import TYPE_CHECKING, Any

from . import _file_snapshot_protocol as guest
from ._file_effect_gate import FileEffectGateError, encode_file_effect_gate
from ._file_revision_wire import FileRevisionWireError, decode_file_revision
from ._file_spool import SpoolSnapshot, SpoolSnapshotFailureKind
from ._scratch import ReadyScratchReference, ScratchFailureKind, ScratchPhase
from ._scratch_receipt import (
    _RECEIPT_MODE,
    ScratchCleanupDebt,
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

if TYPE_CHECKING:
    from ._helper_identity import IdentityExpectation


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _identity_value(identity: IdentityExpectation) -> dict[str, object]:
    return {"egid": identity.egid, "euid": identity.euid, "groups": list(identity.groups)}


def encode_file_snapshot_request(request: guest.FileSnapshotRequest) -> bytes:
    """Encode trusted host values and enforce the complete guest schema."""
    failed = False
    encoded = b""
    try:
        common: dict[str, object] = {
            "identity": _identity_value(request.identity),
            "nonce": request.nonce,
            "operation": request.operation.value,
            "remaining_seconds": request.remaining_seconds,
            "token": request.token.hex(),
            "version": 1,
        }
        if request.effect_gate is not None:
            if request.effect_gate.proposed_generation is not None or request.effect_gate.euid != request.identity.euid:
                raise FileEffectGateError("invalid snapshot gate binding")
            common["effect_gate"] = encode_file_effect_gate(request.effect_gate)
        if isinstance(request, guest.FileSnapshotBeginRequest):
            common.update(
                {
                    "max_bytes": request.max_bytes,
                    "path": _encode_bytes(request.relative_path.encode("utf-8")),
                    "root": _encode_bytes(request.root_path.encode("utf-8")),
                }
            )
        elif isinstance(request, guest.FileSnapshotChunkRequest):
            reference = request.ready._reference._ownership
            if reference._token != request.token or reference._context != guest.snapshot_context(request.identity):
                raise ScratchWireError
            common.update(
                {
                    "length": request.length,
                    "offset": request.offset,
                    "ready": encode_ready_scratch_reference(request.ready),
                }
            )
        elif isinstance(request, guest.FileSnapshotStreamRequest):
            reference = request.ready._reference._ownership
            if reference._token != request.token or reference._context != guest.snapshot_context(request.identity):
                raise ScratchWireError
            common["ready"] = encode_ready_scratch_reference(request.ready)
        elif isinstance(request, guest.FileSnapshotReconcileRequest):
            pass
        elif isinstance(request, guest.FileSnapshotCleanupRequest):
            if (
                request.cleanup_debt._name != scratch_name(request.token)
                or request.cleanup_debt._uid != request.identity.euid
            ):
                raise ScratchWireError
            common["cleanup"] = encode_cleanup_debt(request.cleanup_debt)
        else:
            raise TypeError
        encoded = guest._json_bytes(common)
    except (AttributeError, FileEffectGateError, ScratchWireError, TypeError, UnicodeEncodeError, ValueError):
        failed = True
    if failed:
        raise guest._invalid_request()
    if len(encoded) > guest.MAX_REQUEST_BYTES:
        raise guest.FileSnapshotRequestError(guest.FileSnapshotFailureCode.OVERSIZED_REQUEST)
    guest.decode_file_snapshot_request(encoded)
    return encoded


def _historical_cleanup_shape(debt: ScratchCleanupDebt) -> bool:
    return (
        debt._parent is not None
        and debt._directory is not None
        and debt._object is not None
        and debt._receipt is not None
        and debt._receipt_modes == (_RECEIPT_MODE,)
    )


def _load_json(data: bytes) -> dict[str, object]:
    failed = False
    value: Any = None
    if type(data) is not bytes or len(data) > 4_096:
        failed = True
    else:
        try:
            value = json.loads(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            failed = True
    if failed or type(value) is not dict:
        raise guest.FileSnapshotControlError
    return value


def parse_empty_file_snapshot_body(body: bytes) -> None:
    if body != guest.empty_file_snapshot_body():
        raise guest.FileSnapshotControlError


def parse_file_snapshot_begin_result(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
    max_bytes: int,
) -> SpoolSnapshot | None:
    value = _load_json(body)
    failed = False
    canonical = b""
    try:
        canonical = guest._json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise guest.FileSnapshotControlError
    result = value.get("result")
    if result == "absent" and set(value) == {"result"}:
        return None
    if result != "ready" or set(value) != {"ready", "result", "source"}:
        raise guest.FileSnapshotControlError
    failed = False
    ready: ReadyScratchReference | None = None
    source = None
    try:
        ready = decode_ready_scratch_reference(value["ready"], token, guest.snapshot_context(identity))
        source = decode_file_revision(value["source"])
    except (FileRevisionWireError, ScratchWireError):
        failed = True
    if failed or ready is None or source is None:
        raise guest.FileSnapshotControlError
    source_digest = source.digest
    length = ready._reference._ownership._length
    if (
        source_digest is None
        or not stat.S_ISREG(source.stat.mode)
        or source.stat.size > max_bytes
        or length != source.stat.size
        or not hmac.compare_digest(ready._digest, source_digest)
    ):
        raise guest.FileSnapshotControlError
    return SpoolSnapshot(ready, source)


def parse_file_snapshot_chunk_result(
    body: bytes,
    requested_offset: int,
    requested_length: int,
    data: bytes,
) -> guest.FileSnapshotChunkResult:
    value = _load_json(body)
    failed = False
    canonical = b""
    try:
        canonical = guest._json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body or set(value) != {"chunk_sha256", "length", "offset"}:
        raise guest.FileSnapshotControlError
    if value["offset"] != requested_offset or type(value["offset"]) is not int:
        raise guest.FileSnapshotControlError
    if value["length"] != requested_length or type(value["length"]) is not int:
        raise guest.FileSnapshotControlError
    digest_value = value["chunk_sha256"]
    if (
        type(digest_value) is not str
        or len(digest_value) != 64
        or any(character not in guest._LOWER_HEX for character in digest_value)
    ):
        raise guest.FileSnapshotControlError
    if len(data) != requested_length:
        raise guest.FileSnapshotControlError
    digest = bytes.fromhex(digest_value)
    if not hmac.compare_digest(hashlib.sha256(data).digest(), digest):
        raise guest.FileSnapshotControlError
    return guest.FileSnapshotChunkResult(requested_offset, requested_length, data, digest)


def parse_file_snapshot_stream_result(
    body: bytes,
    expected_length: int,
    expected_digest: bytes,
    observed_length: int,
    observed_digest: bytes,
) -> None:
    value = _load_json(body)
    try:
        canonical = guest._json_bytes(value)
    except (TypeError, ValueError):
        raise guest.FileSnapshotControlError from None
    digest = value.get("sha256")
    if (
        canonical != body
        or set(value) != {"length", "sha256"}
        or type(value["length"]) is not int
        or value["length"] != expected_length
        or value["length"] != observed_length
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in guest._LOWER_HEX for character in digest)
    ):
        raise guest.FileSnapshotControlError
    decoded = bytes.fromhex(digest)
    if not hmac.compare_digest(decoded, expected_digest) or not hmac.compare_digest(decoded, observed_digest):
        raise guest.FileSnapshotControlError


def parse_file_snapshot_reconcile_result(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> ScratchCleanupDebt | None:
    value = _load_json(body)
    failed = False
    canonical = b""
    cleanup: ScratchCleanupDebt | None = None
    try:
        canonical = guest._json_bytes(value)
        result = value.get("result")
        if result == "recovered" and set(value) == {"cleanup", "result"}:
            cleanup = decode_cleanup_debt(value["cleanup"], token, guest.snapshot_context(identity))
            if not _historical_cleanup_shape(cleanup):
                failed = True
        elif result != "ownership_uncertain" or set(value) != {"result"}:
            failed = True
    except (ScratchWireError, TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise guest.FileSnapshotControlError
    return cleanup


def parse_file_snapshot_cleanup_result(body: bytes) -> None:
    if body != guest.encode_file_snapshot_cleanup_result():
        raise guest.FileSnapshotControlError


def parse_file_snapshot_failure(
    body: bytes,
    token: bytes,
    identity: IdentityExpectation,
) -> guest.FileSnapshotFailureControl:
    value = _load_json(body)
    failed = False
    canonical = b""
    code = guest.FileSnapshotFailureCode.INVALID_REQUEST
    try:
        canonical = guest._json_bytes(value)
        code_value = value.get("code")
        code = guest.FileSnapshotFailureCode(code_value if type(code_value) is str else "")
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise guest.FileSnapshotControlError
    if code not in {guest.FileSnapshotFailureCode.SPOOL, guest.FileSnapshotFailureCode.SCRATCH}:
        if set(value) != {"code"}:
            raise guest.FileSnapshotControlError
        return guest.FileSnapshotFailureControl(code)
    context: ScratchReceiptContext = guest.snapshot_context(identity)
    cleanup: ScratchCleanupDebt | None = None
    if code is guest.FileSnapshotFailureCode.SPOOL:
        if set(value) != {"cleanup", "code", "kind"}:
            raise guest.FileSnapshotControlError
        failed = False
        kind = SpoolSnapshotFailureKind.IO
        try:
            kind = SpoolSnapshotFailureKind(value["kind"])
            cleanup = None if value["cleanup"] is None else decode_cleanup_debt(value["cleanup"], token, context)
        except (ScratchWireError, TypeError, ValueError):
            failed = True
        if failed:
            raise guest.FileSnapshotControlError
        return guest.FileSnapshotFailureControl(code, spool_kind=kind, cleanup_debt=cleanup)
    if set(value) != {"cleanup", "code", "kind", "phase"}:
        raise guest.FileSnapshotControlError
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
        raise guest.FileSnapshotControlError
    return guest.FileSnapshotFailureControl(code, scratch_kind=scratch_kind, scratch_phase=phase, cleanup_debt=cleanup)
