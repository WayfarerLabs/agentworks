"""Bounded controls for private exact managed-run disposal."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum

from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_observation_protocol import ManagedObservationError, checked_launch

MAX_REQUEST_BYTES = 8192


class DisposalError(ValueError):
    """Invalid disposal control."""


class DisposalResult(StrEnum):
    DISPOSED = "disposed"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True, repr=False)
class DisposalRequest:
    nonce: str
    expected_launch: bytes
    identity: IdentityExpectation


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def encode_request(request: DisposalRequest) -> bytes:
    if type(request) is not DisposalRequest:
        raise DisposalError("invalid disposal request")
    data = _json(
        {
            "version": 1,
            "nonce": request.nonce,
            "expected_launch": base64.b64encode(request.expected_launch).decode("ascii"),
            "identity": {
                "euid": request.identity.euid,
                "egid": request.identity.egid,
                "groups": list(request.identity.groups),
            },
        }
    )
    decode_request(data)
    return data


def decode_request(data: bytes) -> DisposalRequest:
    if type(data) is not bytes or len(data) > MAX_REQUEST_BYTES:
        raise DisposalError("invalid disposal request")
    try:
        value = json.loads(data.decode("ascii"))
        if (
            type(value) is not dict
            or set(value) != {"version", "nonce", "expected_launch", "identity"}
            or _json(value) != data
        ):
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1 or not valid_nonce(value["nonce"]):
            raise ValueError
        encoded = value["expected_launch"]
        if type(encoded) is not str:
            raise ValueError
        launch = base64.b64decode(encoded.encode("ascii"), validate=True)
        if base64.b64encode(launch).decode("ascii") != encoded:
            raise ValueError
        checked_launch(launch)
        identity = decode_identity(value["identity"])
        if identity.euid != 0:
            raise ValueError
    except (ManagedObservationError, ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise DisposalError("invalid disposal request") from None
    return DisposalRequest(value["nonce"], launch, identity)


def encode_result(result: DisposalResult) -> bytes:
    if type(result) is not DisposalResult:
        raise DisposalError("invalid disposal result")
    return _json({"version": 1, "result": result.value})


def decode_result(data: bytes) -> DisposalResult:
    if type(data) is not bytes or len(data) > 64:
        raise DisposalError("invalid disposal result")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or set(value) != {"version", "result"} or _json(value) != data:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1 or type(value["result"]) is not str:
            raise ValueError
        return DisposalResult(value["result"])
    except (ValueError, TypeError, UnicodeError):
        raise DisposalError("invalid disposal result") from None
