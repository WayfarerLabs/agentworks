"""Closed wire for one identity-bound workload shell observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._helper_identity import IdentityExpectation, decode_identity

MAX_WORKLOAD_SHELL_MESSAGE_BYTES = 1_048_576
SUPPORTED_SHELLS = frozenset({"/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash"})
_HEX = frozenset("0123456789abcdef")


class WorkloadShellFailure(StrEnum):
    IDENTITY = "identity"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    LOOKUP = "lookup"
    RUNTIME = "runtime"
    INVALID_REQUEST = "invalid_request"


class WorkloadShellWireError(ValueError):
    """A bounded, canonical helper message violated its closed schema."""


@dataclass(frozen=True, slots=True, repr=False)
class WorkloadShellRequest:
    nonce: str
    identity: IdentityExpectation


@dataclass(frozen=True, slots=True)
class WorkloadShellResponse:
    shell: str | None = None
    failure: WorkloadShellFailure | None = None


def _nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(c in _HEX for c in value)


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _object(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > MAX_WORKLOAD_SHELL_MESSAGE_BYTES:
        raise WorkloadShellWireError
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique)
        if type(value) is not dict or _json(value) != data:
            raise ValueError
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise WorkloadShellWireError from None
    return value


def _identity(value: IdentityExpectation) -> dict[str, object]:
    record = {"euid": value.euid, "egid": value.egid, "groups": list(value.groups)}
    try:
        decode_identity(record)
    except ValueError:
        raise WorkloadShellWireError from None
    return record


def encode_workload_shell_request(request: WorkloadShellRequest) -> bytes:
    if type(request) is not WorkloadShellRequest or not _nonce(request.nonce):
        raise WorkloadShellWireError
    data = _json({"identity": _identity(request.identity), "nonce": request.nonce, "version": 1})
    if len(data) > MAX_WORKLOAD_SHELL_MESSAGE_BYTES:
        raise WorkloadShellWireError
    return data


def decode_workload_shell_request(data: bytes) -> WorkloadShellRequest:
    value = _object(data)
    if (
        set(value) != {"identity", "nonce", "version"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not _nonce(value["nonce"])
    ):
        raise WorkloadShellWireError
    try:
        identity = decode_identity(value["identity"])
    except ValueError:
        raise WorkloadShellWireError from None
    return WorkloadShellRequest(value["nonce"], identity)


def encode_workload_shell_response(nonce: str, response: WorkloadShellResponse) -> bytes:
    if not _nonce(nonce) or type(response) is not WorkloadShellResponse:
        raise WorkloadShellWireError
    if (
        response.shell is not None
        and response.failure is None
        and type(response.shell) is str
        and response.shell in SUPPORTED_SHELLS
    ):
        value = {"nonce": nonce, "shell": response.shell, "status": "shell", "version": 1}
    elif response.shell is None and type(response.failure) is WorkloadShellFailure:
        value = {"failure": response.failure.value, "nonce": nonce, "status": "refused", "version": 1}
    else:
        raise WorkloadShellWireError
    return _json(value)


def decode_workload_shell_response(data: bytes, nonce: str) -> WorkloadShellResponse:
    if not _nonce(nonce):
        raise WorkloadShellWireError
    value = _object(data)
    if value.get("nonce") != nonce or type(value.get("version")) is not int or value["version"] != 1:
        raise WorkloadShellWireError
    if (
        set(value) == {"nonce", "shell", "status", "version"}
        and value["status"] == "shell"
        and type(value["shell"]) is str
        and value["shell"] in SUPPORTED_SHELLS
    ):
        return WorkloadShellResponse(shell=value["shell"])
    if set(value) == {"failure", "nonce", "status", "version"} and value["status"] == "refused":
        try:
            return WorkloadShellResponse(failure=WorkloadShellFailure(value["failure"]))
        except (TypeError, ValueError):
            pass
    raise WorkloadShellWireError
