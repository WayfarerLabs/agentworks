"""Closed request and result wire for private guest file-gate control."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_effect_gate import (
    FileEffectGateBinding,
    FileEffectGateError,
    _valid_path,
    _valid_scope_name,
    decode_file_effect_gate,
    encode_file_effect_gate,
)
from ._file_wire import MAX_RECORD_BODY_BYTES, valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._vm_guest_identity_protocol import VMGuestIdentity

MAX_REQUEST_BYTES = 32_768
_VERSION = 1
_COMMON_FIELDS = frozenset(
    {"euid", "guest", "identity", "nonce", "operation", "path", "remaining_seconds", "scope_name", "version"}
)


class GateControlOperation(StrEnum):
    SETUP = "setup"
    INSPECT = "inspect"
    ADVANCE = "advance"


class GateControlFailure(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERSIZED_REQUEST = "oversized_request"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    GUEST_MISMATCH = "guest_mismatch"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    DEADLINE = "deadline"
    GATE = "gate"


class GateControlProtocolError(ValueError):
    """A control value violated its closed schema without retaining its content."""


@dataclass(frozen=True, slots=True, repr=False)
class GateControlRequest:
    nonce: str
    operation: GateControlOperation
    path: str
    guest: VMGuestIdentity
    euid: int
    scope_name: str
    identity: IdentityExpectation
    remaining_seconds: float | None
    binding: FileEffectGateBinding | None = None


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_object(data: bytes, *, maximum_bytes: int) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > maximum_bytes:
        raise GateControlProtocolError("invalid gate-control body")
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object, parse_constant=lambda _: _reject())
        canonical = _json_bytes(value)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise GateControlProtocolError("invalid gate-control body") from None
    if type(value) is not dict or canonical != data:
        raise GateControlProtocolError("invalid gate-control body")
    return value


def _reject() -> None:
    raise ValueError


def _guest(value: object) -> VMGuestIdentity:
    if type(value) is not dict or set(value) != {"instance_marker", "boot_id", "init_start_ticks"}:
        raise GateControlProtocolError("invalid gate-control guest")
    try:
        return VMGuestIdentity(value["instance_marker"], value["boot_id"], value["init_start_ticks"])
    except (TypeError, ValueError):
        raise GateControlProtocolError("invalid gate-control guest") from None


def _guest_value(guest: VMGuestIdentity) -> dict[str, object]:
    return {
        "instance_marker": guest.instance_marker,
        "boot_id": guest.boot_id,
        "init_start_ticks": guest.init_start_ticks,
    }


def _identity_value(identity: IdentityExpectation) -> dict[str, object]:
    return {"egid": identity.egid, "euid": identity.euid, "groups": list(identity.groups)}


def encode_gate_control_request(request: GateControlRequest) -> bytes:
    """Encode trusted host values, then enforce the complete guest schema."""
    try:
        value: dict[str, object] = {
            "euid": request.euid,
            "guest": _guest_value(request.guest),
            "identity": _identity_value(request.identity),
            "nonce": request.nonce,
            "operation": request.operation.value,
            "path": request.path,
            "remaining_seconds": request.remaining_seconds,
            "scope_name": request.scope_name,
            "version": _VERSION,
        }
        if request.operation is GateControlOperation.ADVANCE:
            value["binding"] = encode_file_effect_gate(request.binding)  # type: ignore[arg-type]
        encoded = _json_bytes(value)
        decode_gate_control_request(encoded)
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError):
        raise GateControlProtocolError("invalid gate-control request") from None
    return encoded


def decode_gate_control_request(data: bytes) -> GateControlRequest:
    """Validate one canonical untrusted manifest before guest effects."""
    value = _decode_object(data, maximum_bytes=MAX_REQUEST_BYTES)
    try:
        operation = GateControlOperation(value["operation"])
        fields = _COMMON_FIELDS | ({"binding"} if operation is GateControlOperation.ADVANCE else set())
        if set(value) != fields or type(value["version"]) is not int or value["version"] != _VERSION:
            raise ValueError
        nonce = value["nonce"]
        guest = _guest(value["guest"])
        identity = decode_identity(value["identity"])
        path = value["path"]
        euid = value["euid"]
        scope_name = value["scope_name"]
        remaining = value["remaining_seconds"]
        if (
            not valid_nonce(nonce)
            or not _valid_path(path)
            or type(euid) is not int
            or euid != identity.euid
            or not _valid_scope_name(scope_name)
            or remaining is not None
            and (type(remaining) is not float or not math.isfinite(remaining) or remaining < 0)
        ):
            raise ValueError
        binding = decode_file_effect_gate(value["binding"]) if operation is GateControlOperation.ADVANCE else None
        if binding is not None and (
            binding.path != path
            or binding.guest != guest
            or binding.euid != euid
            or binding.scope_name != scope_name
            or binding.proposed_generation is None
        ):
            raise ValueError
        return GateControlRequest(nonce, operation, path, guest, euid, scope_name, identity, remaining, binding)
    except (KeyError, TypeError, ValueError, FileEffectGateError):
        raise GateControlProtocolError("invalid gate-control request") from None


def encode_gate_control_result(binding: FileEffectGateBinding) -> bytes:
    if type(binding) is not FileEffectGateBinding or binding.proposed_generation is not None:
        raise GateControlProtocolError("invalid gate-control result")
    return _json_bytes(
        {
            "device": binding.device,
            "generation": binding.generation.hex(),
            "inode": binding.inode,
            "instance": binding.instance.hex(),
        }
    )


def parse_gate_control_result(body: bytes, request: GateControlRequest) -> FileEffectGateBinding:
    value = _decode_object(body, maximum_bytes=MAX_RECORD_BODY_BYTES)
    if set(value) != {"device", "generation", "inode", "instance"}:
        raise GateControlProtocolError("invalid gate-control result")
    try:
        binding = decode_file_effect_gate(
            {
                "path": request.path,
                "guest": _guest_value(request.guest),
                "euid": request.euid,
                "scope_name": request.scope_name,
                **value,
            }
        )
    except FileEffectGateError:
        raise GateControlProtocolError("invalid gate-control result") from None
    if binding.proposed_generation is not None:
        raise GateControlProtocolError("invalid gate-control result")
    return binding


def encode_gate_control_failure(failure: GateControlFailure) -> bytes:
    return _json_bytes({"code": failure.value})


def parse_gate_control_failure(body: bytes) -> GateControlFailure:
    value = _decode_object(body, maximum_bytes=MAX_RECORD_BODY_BYTES)
    if set(value) != {"code"}:
        raise GateControlProtocolError("invalid gate-control failure")
    try:
        return GateControlFailure(value["code"])
    except (TypeError, ValueError):
        raise GateControlProtocolError("invalid gate-control failure") from None


def parse_gate_control_finished(body: bytes) -> None:
    if body != b"{}":
        raise GateControlProtocolError("invalid gate-control terminal")
