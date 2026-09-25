"""Portable bounded stdin and result controls for one managed start attempt."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from . import _managed_job_request as request_wire
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_store import FactName, RequestAsset

_ASSETS = tuple(RequestAsset)
_ASSET_LIMITS = (
    request_wire.MAX_LAUNCH_BYTES,
    request_wire.MAX_CONTROL_BYTES,
    request_wire.MAX_ENVIRONMENT_BYTES,
    request_wire.MAX_SOURCE_BYTES,
    request_wire.MAX_STDIN_BYTES,
)
MAX_REQUEST_BYTES = 4096 + sum(4 * ((bound + 2) // 3) for bound in _ASSET_LIMITS)
MAX_RESULT_BYTES = 128


class ManagedStartError(ValueError):
    """Invalid private start control without retaining its contents."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStartRequest:
    nonce: str
    identity: IdentityExpectation
    job: request_wire.ManagedJobRequest


@dataclass(frozen=True, slots=True)
class ManagedStartResult:
    """Observed systemd-run outcome and a fixed fact manifest."""

    exit_code: int | None
    signal: int | None
    facts: tuple[FactName, ...]


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def encode_request(request: ManagedStartRequest) -> bytes:
    if type(request) is not ManagedStartRequest or not valid_nonce(request.nonce):
        raise ManagedStartError("invalid managed start request")
    try:
        assets = request_wire.encode_request(request.job)
        identity = decode_identity(
            {
                "euid": request.identity.euid,
                "egid": request.identity.egid,
                "groups": list(request.identity.groups),
            }
        )
    except (request_wire.RequestError, ValueError, TypeError, AttributeError):
        raise ManagedStartError("invalid managed start request") from None
    data = _json(
        {
            "version": 1,
            "nonce": request.nonce,
            "identity": {"euid": identity.euid, "egid": identity.egid, "groups": list(identity.groups)},
            "assets": [base64.b64encode(assets[name.value]).decode("ascii") for name in _ASSETS],
        }
    )
    if len(data) > MAX_REQUEST_BYTES:
        raise ManagedStartError("managed start request exceeds bound")
    return data


def decode_request(data: bytes) -> ManagedStartRequest:
    if type(data) is not bytes or len(data) > MAX_REQUEST_BYTES:
        raise ManagedStartError("invalid managed start request")
    try:
        header = json.loads(data.decode("ascii"))
        if type(header) is not dict or _json(header) != data:
            raise ValueError
        if (
            set(header) != {"version", "nonce", "identity", "assets"}
            or type(header["version"]) is not int
            or header["version"] != 1
            or not valid_nonce(header["nonce"])
        ):
            raise ValueError
        identity = decode_identity(header["identity"])
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
    ):
        raise ManagedStartError("invalid managed start request") from None
    return ManagedStartRequest(header["nonce"], identity, job)


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
