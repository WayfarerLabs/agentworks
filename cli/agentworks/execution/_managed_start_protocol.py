"""Portable bounded stdin and result controls for one managed start attempt."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, replace

from . import _managed_job_request as request_wire
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_store import FactName, RequestAsset
from ._managed_lease_wire import MAX_LEASE_BYTES, LeaseError, OperationLease, decode_lease, encode_lease
from ._vm_guest_identity_protocol import VMGuestIdentity

_ASSETS = tuple(RequestAsset)
_ASSET_LIMITS = (
    request_wire.MAX_LAUNCH_BYTES,
    request_wire.MAX_CONTROL_BYTES,
    request_wire.MAX_ENVIRONMENT_BYTES,
    request_wire.MAX_SOURCE_BYTES,
    request_wire.MAX_STDIN_BYTES,
)
_MAX_BODY_BYTES = 4096 + sum(4 * ((bound + 2) // 3) for bound in _ASSET_LIMITS)
MAX_REQUEST_BYTES = _MAX_BODY_BYTES + MAX_LEASE_BYTES
MAX_RESULT_BYTES = 128


class ManagedStartError(ValueError):
    """Invalid private start control without retaining its contents."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStartRequest:
    nonce: str
    identity: IdentityExpectation
    job: request_wire.ManagedJobRequest
    guest: VMGuestIdentity


@dataclass(frozen=True, slots=True)
class ManagedStartResult:
    """Observed systemd-run outcome and a fixed fact manifest."""

    exit_code: int | None
    signal: int | None
    facts: tuple[FactName, ...]


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _check_headroom(launch: bytes, control: bytes) -> None:
    if (
        request_wire.decode_request_launch(launch)["lifetime"] == "operation"
        and len(control) + request_wire.OPERATION_LEASE_CONTROL_HEADROOM > request_wire.MAX_CONTROL_BYTES
    ):
        raise ManagedStartError("operation control exceeds bound with lease headroom")


def encode_body(request: ManagedStartRequest) -> bytes:
    """Freeze validated preparation bytes without carrying dispatch authority."""
    if (
        type(request) is not ManagedStartRequest
        or not valid_nonce(request.nonce)
        or type(request.guest) is not VMGuestIdentity
    ):
        raise ManagedStartError("invalid managed start request")
    try:
        if type(request.job) is not request_wire.ManagedJobRequest or request.job.operation_lease is not None:
            raise ManagedStartError("managed start body must omit the lease")
        assets = request_wire._encode_request_assets(request.job, body_only=True)
        _check_headroom(assets["request-launch"], assets["request-control"])
        identity = decode_identity(
            {
                "euid": request.identity.euid,
                "egid": request.identity.egid,
                "groups": list(request.identity.groups),
            }
        )
        guest = VMGuestIdentity(
            request.guest.instance_marker,
            request.guest.boot_id,
            request.guest.init_start_ticks,
        )
    except (request_wire.RequestError, ValueError, TypeError, AttributeError):
        raise ManagedStartError("invalid managed start request") from None
    data = _json(
        {
            "version": 2,
            "nonce": request.nonce,
            "identity": {"euid": identity.euid, "egid": identity.egid, "groups": list(identity.groups)},
            "guest": {
                "instance_marker": guest.instance_marker,
                "boot_id": guest.boot_id,
                "init_start_ticks": guest.init_start_ticks,
            },
            "assets": [base64.b64encode(assets[name.value]).decode("ascii") for name in _ASSETS],
        }
    )
    if len(data) > _MAX_BODY_BYTES:
        raise ManagedStartError("managed start request exceeds bound")
    return data


def encode_tail(lease: OperationLease | None) -> bytes:
    """Encode one canonical lease or null in the existing fixed lease bound."""
    try:
        if lease is None:
            data = b"null"
        elif type(lease) is OperationLease:
            data = encode_lease(lease)
        else:
            raise ManagedStartError("invalid managed start lease tail")
    except (LeaseError, ValueError, TypeError, AttributeError):
        raise ManagedStartError("invalid managed start lease tail") from None
    return data.ljust(MAX_LEASE_BYTES, b" ")


def _decode_tail(data: bytes) -> OperationLease | None:
    unpadded = data.rstrip(b" ")
    lease = None if unpadded == b"null" else decode_lease(unpadded)
    if encode_tail(lease) != data:
        raise ManagedStartError("noncanonical managed start lease tail")
    return lease


def encode_request(request: ManagedStartRequest) -> bytes:
    """Validate a dispatchable request before freezing its body and late tail."""
    try:
        if type(request) is not ManagedStartRequest:
            raise ValueError
        request_wire.encode_request(request.job)
        body = encode_body(replace(request, job=replace(request.job, operation_lease=None)))
        return body + encode_tail(request.job.operation_lease)
    except (request_wire.RequestError, ValueError, TypeError, AttributeError):
        raise ManagedStartError("invalid managed start request") from None


def decode_request(data: bytes) -> ManagedStartRequest:
    if type(data) is not bytes or not MAX_LEASE_BYTES < len(data) <= MAX_REQUEST_BYTES:
        raise ManagedStartError("invalid managed start request")
    try:
        body, tail = data[:-MAX_LEASE_BYTES], data[-MAX_LEASE_BYTES:]
        lease = _decode_tail(tail)
        header = json.loads(body.decode("ascii"))
        if type(header) is not dict or _json(header) != body:
            raise ValueError
        if (
            set(header) != {"version", "nonce", "identity", "guest", "assets"}
            or type(header["version"]) is not int
            or header["version"] != 2
            or not valid_nonce(header["nonce"])
        ):
            raise ValueError
        identity = decode_identity(header["identity"])
        guest_data = header["guest"]
        if type(guest_data) is not dict or set(guest_data) != {"instance_marker", "boot_id", "init_start_ticks"}:
            raise ValueError
        guest = VMGuestIdentity(guest_data["instance_marker"], guest_data["boot_id"], guest_data["init_start_ticks"])
        encoded = header["assets"]
        if type(encoded) is not list or len(encoded) != len(_ASSETS) or any(type(part) is not str for part in encoded):
            raise ValueError
        assets: dict[str, bytes] = {}
        for name, part, bound in zip(_ASSETS, encoded, _ASSET_LIMITS, strict=True):
            if len(part) > 4 * ((bound + 2) // 3):
                raise ValueError
            payload = base64.b64decode(part.encode("ascii"), validate=True)
            if len(payload) > bound or base64.b64encode(payload).decode("ascii") != part:
                raise ValueError
            assets[name.value] = payload
        control = request_wire.decode_control(assets["request-control"])
        if "operation_lease" in control:
            raise ManagedStartError("managed start body already carries a lease")
        _check_headroom(assets["request-launch"], assets["request-control"])
        if lease is not None:
            control["operation_lease"] = json.loads(encode_lease(lease))
            assets["request-control"] = _json(control)
        job = request_wire.decode_request(assets)
    except (
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        binascii.Error,
        request_wire.RequestError,
        LeaseError,
    ):
        raise ManagedStartError("invalid managed start request") from None
    return ManagedStartRequest(header["nonce"], identity, job, guest)


def encode_result(result: ManagedStartResult) -> bytes:
    if type(result) is not ManagedStartResult:
        raise ManagedStartError("invalid managed start result")
    data = _json(
        {
            "version": 1,
            "exit_code": result.exit_code,
            "signal": result.signal,
            "facts": [name.value for name in result.facts],
        }
    )
    decode_result(data)
    return data


def decode_result(data: bytes) -> ManagedStartResult:
    if type(data) is not bytes or len(data) > MAX_RESULT_BYTES:
        raise ManagedStartError("invalid managed start result")
    try:
        value = json.loads(data.decode("ascii"))
        if (
            type(value) is not dict
            or _json(value) != data
            or set(value)
            != {
                "version",
                "exit_code",
                "signal",
                "facts",
            }
        ):
            raise ValueError
        code = value["exit_code"]
        signal = value["signal"]
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or (code is not None and (type(code) is not int or not 0 <= code <= 255))
            or (signal is not None and (type(signal) is not int or not 1 <= signal <= 255))
            or (code is not None and signal is not None)
            or value["facts"] not in ([], ["launch"])
        ):
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise ManagedStartError("invalid managed start result") from None
    return ManagedStartResult(code, signal, tuple(FactName(name) for name in value["facts"]))
