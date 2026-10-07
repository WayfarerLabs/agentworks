"""Finite closed clock-observe and exact operation-lease publication controls."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import cast

from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_lease_wire import LeaseError, OperationLease, checked_clock, checked_lease, decode_lease, encode_lease
from ._managed_observation_protocol import checked_vm_launch
from ._vm_guest_identity_protocol import VMGuestIdentity

MAX_REQUEST_BYTES = 8192
MAX_RESULT_BYTES = 128


@dataclass(frozen=True, slots=True, repr=False)
class LeaseRequest:
    nonce: str
    identity: IdentityExpectation
    guest: VMGuestIdentity
    expected_launch: bytes | None = None
    lease: OperationLease | None = None


@dataclass(frozen=True, slots=True)
class ClockObservation:
    sampled_ns: int


@dataclass(frozen=True, slots=True)
class LeasePublication:
    accepted_expiry_ns: int


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _load(data: bytes, bound: int) -> dict[str, object]:
    if type(data) is not bytes or len(data) > bound:
        raise LeaseError("invalid lease control size")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or _json(value) != data:
            raise ValueError
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise LeaseError("invalid lease control") from None


def encode_request(request: LeaseRequest) -> bytes:
    data = _json(
        {
            "version": 1,
            "nonce": request.nonce,
            "identity": {
                "euid": request.identity.euid,
                "egid": request.identity.egid,
                "groups": list(request.identity.groups),
            },
            "guest": {
                "instance_marker": request.guest.instance_marker,
                "boot_id": request.guest.boot_id,
                "init_start_ticks": request.guest.init_start_ticks,
            },
            "expected_launch": base64.b64encode(request.expected_launch).decode("ascii")
            if request.expected_launch is not None
            else None,
            "lease": json.loads(encode_lease(request.lease)) if request.lease is not None else None,
        }
    )
    decode_request(data)
    return data


def decode_request(data: bytes) -> LeaseRequest:
    value = _load(data, MAX_REQUEST_BYTES)
    try:
        if set(value) != {"version", "nonce", "identity", "guest", "expected_launch", "lease"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1 or not valid_nonce(value["nonce"]):
            raise ValueError
        identity = decode_identity(value["identity"])
        if identity.euid != 0:
            raise ValueError
        guest_data = value["guest"]
        if type(guest_data) is not dict or set(guest_data) != {"instance_marker", "boot_id", "init_start_ticks"}:
            raise ValueError
        guest = VMGuestIdentity(**guest_data)
        encoded, proposed = value["expected_launch"], value["lease"]
        if encoded is None and proposed is None:
            return LeaseRequest(cast("str", value["nonce"]), identity, guest)
        if type(encoded) is not str or proposed is None:
            raise ValueError
        launch = base64.b64decode(encoded.encode("ascii"), validate=True)
        if base64.b64encode(launch).decode("ascii") != encoded:
            raise ValueError
        checked_vm_launch(launch, guest)
        lease = checked_lease(decode_lease(_json(proposed)), launch)
        return LeaseRequest(cast("str", value["nonce"]), identity, guest, launch, lease)
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise LeaseError("invalid lease request") from None


def encode_result(result: ClockObservation | LeasePublication) -> bytes:
    if isinstance(result, ClockObservation):
        return _json({"version": 1, "sampled_ns": checked_clock(result.sampled_ns)})
    return _json({"version": 1, "accepted_expiry_ns": checked_clock(result.accepted_expiry_ns)})


def decode_result(data: bytes) -> ClockObservation | LeasePublication:
    value = _load(data, MAX_RESULT_BYTES)
    if type(value.get("version")) is not int or value["version"] != 1:
        raise LeaseError("invalid lease result version")
    if set(value) == {"version", "sampled_ns"}:
        return ClockObservation(checked_clock(value["sampled_ns"]))
    if set(value) == {"version", "accepted_expiry_ns"}:
        return LeasePublication(checked_clock(value["accepted_expiry_ns"]))
    raise LeaseError("invalid lease result fields")
