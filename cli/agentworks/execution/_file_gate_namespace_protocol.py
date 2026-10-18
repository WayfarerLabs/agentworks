"""Closed wire for one root-fenced managed UID namespace preparation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from ._file_effect_gate import FileEffectGateBinding, decode_file_effect_gate, encode_file_effect_gate
from ._file_effect_gate_protocol import GateControlProtocolError, _decode_object, _identity_value, _json_bytes
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity

MAX_REQUEST_BYTES = 32_768


class NamespaceStatus(StrEnum):
    PREPARED = "prepared"
    REFUSED = "refused"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True, repr=False)
class NamespaceRequest:
    nonce: str
    uid: int
    gid: int
    identity: IdentityExpectation
    binding: FileEffectGateBinding
    remaining_seconds: float


def encode_namespace_request(request: NamespaceRequest) -> bytes:
    data = _json_bytes(
        {
            "version": 1,
            "nonce": request.nonce,
            "uid": request.uid,
            "gid": request.gid,
            "identity": _identity_value(request.identity),
            "binding": encode_file_effect_gate(request.binding),
            "remaining_seconds": request.remaining_seconds,
        }
    )
    decode_namespace_request(data)
    return data


def decode_namespace_request(data: bytes) -> NamespaceRequest:
    """Validate canonical untrusted input before any namespace filesystem work."""
    value = _decode_object(data, maximum_bytes=MAX_REQUEST_BYTES)
    try:
        if set(value) != {"version", "nonce", "uid", "gid", "identity", "binding", "remaining_seconds"}:
            raise ValueError
        identity = decode_identity(value["identity"])
        binding = decode_file_effect_gate(value["binding"])
        remaining = value["remaining_seconds"]
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not valid_nonce(value["nonce"])
            or type(value["uid"]) is not int
            or not 0 < value["uid"] < 2**32
            or type(value["gid"]) is not int
            or not 0 <= value["gid"] < 2**32
            or identity.euid != 0
            or binding.euid != 0
            or binding.proposed_generation is not None
            or type(remaining) is not float
            or not math.isfinite(remaining)
            or remaining < 0
        ):
            raise ValueError
        return NamespaceRequest(value["nonce"], value["uid"], value["gid"], identity, binding, remaining)
    except (KeyError, TypeError, ValueError):
        raise GateControlProtocolError("invalid namespace request") from None


def encode_namespace_status(status: NamespaceStatus) -> bytes:
    return _json_bytes({"status": status.value})


def decode_namespace_status(data: bytes) -> NamespaceStatus:
    value = _decode_object(data, maximum_bytes=256)
    try:
        if set(value) != {"status"}:
            raise ValueError
        return NamespaceStatus(value["status"])
    except (KeyError, TypeError, ValueError):
        raise GateControlProtocolError("invalid namespace result") from None
