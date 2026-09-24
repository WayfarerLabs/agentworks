"""Fixed bounded controls for one exact managed stop request."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_store import FactName
from ._managed_observation_protocol import ManagedObservationError, checked_launch

MAX_REQUEST_BYTES = 8192
MAX_RESULT_BYTES = 96
MAX_OBSERVATION_MS = 15000


class ManagedStopError(ValueError):
    """Invalid stop control without retaining its contents."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStopRequest:
    nonce: str
    expected_launch: bytes
    identity: IdentityExpectation
    observation_ms: int


@dataclass(frozen=True, slots=True)
class ManagedStopResult:
    facts: tuple[FactName, ...]


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _load(data: bytes, bound: int) -> dict[str, object]:
    if type(data) is not bytes or len(data) > bound:
        raise ManagedStopError("invalid managed stop control")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or _json(value) != data:
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ManagedStopError("invalid managed stop control") from None
    return value


def encode_request(request: ManagedStopRequest) -> bytes:
    if type(request) is not ManagedStopRequest:
        raise ManagedStopError("invalid managed stop request")
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
            "observation_ms": request.observation_ms,
        }
    )
    decode_request(data)
    return data


def decode_request(data: bytes) -> ManagedStopRequest:
    value = _load(data, MAX_REQUEST_BYTES)
    if (
        set(value) != {"version", "nonce", "expected_launch", "identity", "observation_ms"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ManagedStopError("invalid managed stop request")
    try:
        nonce = value["nonce"]
        encoded = value["expected_launch"]
        budget = value["observation_ms"]
        if (
            type(nonce) is not str
            or not valid_nonce(nonce)
            or type(encoded) is not str
            or type(budget) is not int
            or not 1 <= budget <= MAX_OBSERVATION_MS
        ):
            raise ValueError
        launch = base64.b64decode(encoded.encode("ascii"), validate=True)
        if base64.b64encode(launch).decode("ascii") != encoded:
            raise ValueError
        checked_launch(launch)
        identity = decode_identity(value["identity"])
        if identity.euid != 0:
            raise ValueError
    except (ManagedObservationError, ValueError, TypeError, UnicodeError, binascii.Error):
        raise ManagedStopError("invalid managed stop request") from None
    return ManagedStopRequest(nonce, launch, identity, budget)


def encode_result(result: ManagedStopResult) -> bytes:
    if type(result) is not ManagedStopResult:
        raise ManagedStopError("invalid managed stop result")
    data = _json({"version": 1, "facts": [name.value for name in result.facts]})
    decode_result(data)
    return data


def decode_result(data: bytes) -> ManagedStopResult:
    value = _load(data, MAX_RESULT_BYTES)
    if (
        set(value) != {"version", "facts"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["facts"] not in (["launch"], ["launch", "boundary-empty"])
    ):
        raise ManagedStopError("invalid managed stop result")
    return ManagedStopResult(tuple(FactName(name) for name in value["facts"]))
