"""Closed stdlib-only protocol for the private VM guest identity probe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

VM_INSTANCE_MARKER_PATH = "/var/lib/agentworks/instance-id"
VM_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
VM_INIT_STAT_PATH = "/proc/1/stat"

# This wire is deliberately much smaller than the carrier's default capture.
MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES = 512

_LOWER_HEX = frozenset("0123456789abcdef")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SUCCESS_FIELDS = frozenset({"boot_id", "init_start_ticks", "instance_marker", "nonce", "status", "version"})
_REFUSAL_FIELDS = frozenset({"failure", "nonce", "status", "version"})
_VERSION = 2
_MAX_START_TICKS = 2**64 - 1


class VMGuestIdentityFailure(StrEnum):
    """Closed guest refusals that disclose no path, ownership, or file bytes."""

    RUNTIME = "runtime"
    MARKER_MISSING = "marker_missing"
    MARKER_UNSAFE = "marker_unsafe"
    MARKER_UNREADABLE = "marker_unreadable"
    BOOT_ID_UNREADABLE = "boot_id_unreadable"
    INIT_START_UNREADABLE = "init_start_unreadable"
    INVALID_IDENTITY = "invalid_identity"


class VMGuestIdentityResponseError(ValueError):
    """A response violated the canonical nonce-bound wire schema."""


@dataclass(frozen=True, slots=True)
class VMGuestIdentity:
    """Fixed-path guest facts used to identify one VM and guest boot."""

    instance_marker: str
    boot_id: str
    init_start_ticks: int

    def __post_init__(self) -> None:
        if not _valid_instance_marker(self.instance_marker):
            raise ValueError("invalid VM instance marker")
        if not _valid_boot_id(self.boot_id):
            raise ValueError("invalid VM boot ID")
        if type(self.init_start_ticks) is not int or not 0 <= self.init_start_ticks <= _MAX_START_TICKS:
            raise ValueError("invalid VM init start time")


@dataclass(frozen=True, slots=True)
class VMGuestIdentityResponse:
    identity: VMGuestIdentity | None = None
    failure: VMGuestIdentityFailure | None = None


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _decode_object(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES:
        raise VMGuestIdentityResponseError
    try:
        value = json.loads(
            data.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise VMGuestIdentityResponseError from None
    if type(value) is not dict:
        raise VMGuestIdentityResponseError
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError, RecursionError):
        raise VMGuestIdentityResponseError from None
    if canonical != data:
        raise VMGuestIdentityResponseError
    return value


def _valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


def _valid_instance_marker(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


def _valid_boot_id(value: object) -> bool:
    return type(value) is str and _UUID_RE.fullmatch(value) is not None


def encode_vm_guest_identity_success(nonce: str, identity: VMGuestIdentity) -> bytes:
    """Encode one canonical identity response."""
    if not _valid_nonce(nonce) or type(identity) is not VMGuestIdentity:
        raise VMGuestIdentityResponseError
    try:
        value = {
            "boot_id": identity.boot_id,
            "init_start_ticks": identity.init_start_ticks,
            "instance_marker": identity.instance_marker,
            "nonce": nonce,
            "status": "identity",
            "version": _VERSION,
        }
        encoded = _json_bytes(value)
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError):
        raise VMGuestIdentityResponseError from None
    if len(encoded) > MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES:
        raise VMGuestIdentityResponseError
    return encoded


def encode_vm_guest_identity_failure(nonce: str, failure: VMGuestIdentityFailure) -> bytes:
    """Encode one canonical closed refusal response."""
    if not _valid_nonce(nonce) or type(failure) is not VMGuestIdentityFailure:
        raise VMGuestIdentityResponseError
    return _json_bytes({"failure": failure.value, "nonce": nonce, "status": "refused", "version": _VERSION})


def decode_vm_guest_identity_response(data: bytes, nonce: str) -> VMGuestIdentityResponse:
    """Validate one untrusted, canonical, nonce-bound helper response."""
    if not _valid_nonce(nonce):
        raise VMGuestIdentityResponseError
    value = _decode_object(data)
    if (
        type(value.get("version")) is not int
        or value["version"] != _VERSION
        or value.get("nonce") != nonce
        or not _valid_nonce(value.get("nonce"))
    ):
        raise VMGuestIdentityResponseError
    if set(value) == _SUCCESS_FIELDS and value.get("status") == "identity":
        try:
            identity = VMGuestIdentity(value["instance_marker"], value["boot_id"], value["init_start_ticks"])
        except (TypeError, ValueError):
            raise VMGuestIdentityResponseError from None
        return VMGuestIdentityResponse(identity=identity)
    if set(value) == _REFUSAL_FIELDS and value.get("status") == "refused":
        try:
            failure = VMGuestIdentityFailure(value["failure"])
        except (TypeError, ValueError):
            raise VMGuestIdentityResponseError from None
        return VMGuestIdentityResponse(failure=failure)
    raise VMGuestIdentityResponseError
