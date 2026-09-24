"""Portable closed controls for independent managed-job observation."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum

from . import _managed_job_wire as wire
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_store import FactName, Stream

MAX_REQUEST_BYTES = 8192
MAX_CONTROL_BYTES = 512
FACT_ORDER = (
    FactName.LAUNCH,
    FactName.WAIT,
    FactName.STDOUT_END,
    FactName.STDERR_END,
    FactName.BOUNDARY_EMPTY,
)


class ManagedObservationError(ValueError):
    """Invalid control or fact bytes, without retaining their contents."""


class ManagedOperation(StrEnum):
    OBSERVE = "observe"
    READ_OUTPUT = "read_output"


class ManagedResultStatus(StrEnum):
    OBSERVED = "observed"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, repr=False)
class ManagedObservationRequest:
    nonce: str
    operation: ManagedOperation
    expected_launch: bytes
    identity: IdentityExpectation
    stream: Stream | None = None


@dataclass(frozen=True, slots=True)
class ManagedResultControl:
    status: ManagedResultStatus
    facts: tuple[FactName, ...]
    output_bytes: int = 0


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _load(data: bytes, limit: int) -> dict[str, object]:
    if type(data) is not bytes or len(data) > limit:
        raise ManagedObservationError("invalid managed observation control")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or _json(value) != data:
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ManagedObservationError("invalid managed observation control") from None
    return value


def checked_launch(data: bytes) -> dict[str, object]:
    """Require one canonical exact-run launch, including target and boot."""
    try:
        launch = wire.decode_fact(data)
    except wire.ManagedJobWireError:
        raise ManagedObservationError("invalid expected launch") from None
    if launch["kind"] != "launch":
        raise ManagedObservationError("invalid expected launch")
    return launch


def encode_request(request: ManagedObservationRequest) -> bytes:
    if type(request) is not ManagedObservationRequest:
        raise ManagedObservationError("invalid managed observation request")
    checked_launch(request.expected_launch)
    if not valid_nonce(request.nonce) or type(request.operation) is not ManagedOperation:
        raise ManagedObservationError("invalid managed observation request")
    if (request.operation is ManagedOperation.OBSERVE and request.stream is not None) or (
        request.operation is ManagedOperation.READ_OUTPUT and type(request.stream) is not Stream
    ):
        raise ManagedObservationError("invalid managed observation selector")
    data = _json(
        {
            "version": 1,
            "nonce": request.nonce,
            "operation": request.operation.value,
            "expected_launch": base64.b64encode(request.expected_launch).decode("ascii"),
            "identity": {
                "euid": request.identity.euid,
                "egid": request.identity.egid,
                "groups": list(request.identity.groups),
            },
            "stream": request.stream.value if request.stream is not None else None,
        }
    )
    if len(data) > MAX_REQUEST_BYTES:
        raise ManagedObservationError("managed observation request exceeds bound")
    decode_request(data)
    return data


def decode_request(data: bytes) -> ManagedObservationRequest:
    value = _load(data, MAX_REQUEST_BYTES)
    fields = {"version", "nonce", "operation", "expected_launch", "identity", "stream"}
    if set(value) != fields or type(value["version"]) is not int or value["version"] != 1:
        raise ManagedObservationError("invalid managed observation request")
    try:
        nonce = value["nonce"]
        operation_value = value["operation"]
        if type(nonce) is not str or type(operation_value) is not str:
            raise ValueError
        operation = ManagedOperation(operation_value)
        encoded = value["expected_launch"]
        if type(encoded) is not str:
            raise ValueError
        launch_bytes = base64.b64decode(encoded.encode("ascii"), validate=True)
        if base64.b64encode(launch_bytes).decode("ascii") != encoded:
            raise ValueError
        checked_launch(launch_bytes)
        identity = decode_identity(value["identity"])
        selected = value["stream"]
        if selected is not None and type(selected) is not str:
            raise ValueError
        stream = None if selected is None else Stream(selected)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ManagedObservationError("invalid managed observation request") from None
    if (
        not valid_nonce(nonce)
        or (operation is ManagedOperation.OBSERVE and stream is not None)
        or (operation is ManagedOperation.READ_OUTPUT and stream is None)
    ):
        raise ManagedObservationError("managed observation binding mismatch")
    return ManagedObservationRequest(nonce, operation, launch_bytes, identity, stream)


def encode_result(control: ManagedResultControl) -> bytes:
    if type(control) is not ManagedResultControl:
        raise ManagedObservationError("invalid managed result")
    data = _json(
        {
            "version": 1,
            "status": control.status.value,
            "facts": [name.value for name in control.facts],
            "output_bytes": control.output_bytes,
        }
    )
    decode_result(data)
    return data


def decode_result(data: bytes) -> ManagedResultControl:
    value = _load(data, MAX_CONTROL_BYTES)
    if (
        set(value) != {"version", "status", "facts", "output_bytes"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ManagedObservationError("invalid managed result")
    try:
        status_value = value["status"]
        if type(status_value) is not str:
            raise ValueError
        status = ManagedResultStatus(status_value)
        names = value["facts"]
        if type(names) is not list:
            raise ValueError
        if any(type(name) is not str for name in names):
            raise ValueError
        facts = tuple(FactName(name) for name in names)
    except (ValueError, TypeError):
        raise ManagedObservationError("invalid managed result") from None
    length = value["output_bytes"]
    if (
        type(length) is not int
        or not 0 <= length <= wire.MAX_CAPTURE_PREFIX_BYTES_V1
        or not facts
        or facts[0] is not FactName.LAUNCH
        or facts != tuple(name for name in FACT_ORDER if name in facts)
        or (status is not ManagedResultStatus.AVAILABLE and length != 0)
    ):
        raise ManagedObservationError("invalid managed result")
    return ManagedResultControl(status, facts, length)


def checked_fact(name: FactName, data: bytes, expected_launch: bytes) -> dict[str, object]:
    """Revalidate a returned fact against exact run, unit, target and boot."""
    launch = checked_launch(expected_launch)
    try:
        fact = wire.decode_fact(data)
    except wire.ManagedJobWireError:
        raise ManagedObservationError("invalid managed fact") from None
    if name is FactName.LAUNCH:
        if data != expected_launch:
            raise ManagedObservationError("managed launch mismatch")
    elif (
        fact["kind"] != ("stream-end" if name in (FactName.STDOUT_END, FactName.STDERR_END) else name.value)
        or fact["run_id"] != launch["run_id"]
        or fact["unit"] != launch["unit"]
        or fact["receipt_sha256"] != wire.launch_sha256(launch)
        or (name in (FactName.STDOUT_END, FactName.STDERR_END) and fact["stream"] != name.value[:-4])
    ):
        raise ManagedObservationError("managed fact binding mismatch")
    return fact
