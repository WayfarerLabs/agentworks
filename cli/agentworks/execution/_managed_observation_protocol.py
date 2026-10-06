"""Portable closed controls for independent managed-job observation."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from . import _managed_job_wire as wire
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_store import FactName, Stream
from ._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

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


class ControllerState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ControllerObservation:
    """Native controller state, independent of workload and dispatch debt."""

    state: ControllerState
    unit: str
    boot_id: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class ManagedObservationRequest:
    nonce: str
    operation: ManagedOperation
    expected_launch: bytes
    identity: IdentityExpectation
    guest: VMGuestIdentity
    stream: Stream | None = None


@dataclass(frozen=True, slots=True)
class ManagedResultControl:
    facts: tuple[FactName, ...]
    controller: ControllerObservation | None = None


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


def checked_vm_launch(data: bytes, guest: VMGuestIdentity) -> dict[str, object]:
    """Bind caller-supplied guest evidence to the exact VM launch before dispatch."""
    launch = checked_launch(data)
    if type(guest) is not VMGuestIdentity:
        raise ManagedObservationError("invalid managed guest identity")
    target = launch["target"]
    if type(target) is not dict or target["kind"] != "vm" or target["boot_id"] != vm_guest_boot_id(guest):
        raise ManagedObservationError("managed target boot mismatch")
    return launch


def encode_request(request: ManagedObservationRequest) -> bytes:
    if type(request) is not ManagedObservationRequest:
        raise ManagedObservationError("invalid managed observation request")
    checked_vm_launch(request.expected_launch, request.guest)
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
            "guest": {
                "instance_marker": request.guest.instance_marker,
                "boot_id": request.guest.boot_id,
                "init_start_ticks": request.guest.init_start_ticks,
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
    fields = {"version", "nonce", "operation", "expected_launch", "identity", "guest", "stream"}
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
        guest_data = value["guest"]
        if type(guest_data) is not dict or set(guest_data) != {"instance_marker", "boot_id", "init_start_ticks"}:
            raise ValueError
        guest = VMGuestIdentity(guest_data["instance_marker"], guest_data["boot_id"], guest_data["init_start_ticks"])
        checked_vm_launch(launch_bytes, guest)
        identity = decode_identity(value["identity"])
        selected = value["stream"]
        if selected is not None and type(selected) is not str:
            raise ValueError
        stream = None if selected is None else Stream(selected)
    except (ValueError, TypeError, UnicodeError, KeyError, binascii.Error):
        raise ManagedObservationError("invalid managed observation request") from None
    if (
        not valid_nonce(nonce)
        or (operation is ManagedOperation.OBSERVE and stream is not None)
        or (operation is ManagedOperation.READ_OUTPUT and stream is None)
    ):
        raise ManagedObservationError("managed observation binding mismatch")
    return ManagedObservationRequest(nonce, operation, launch_bytes, identity, guest, stream)


def encode_result(control: ManagedResultControl) -> bytes:
    value: dict[str, object] = {"version": 1, "facts": [name.value for name in control.facts]}
    if control.controller is not None:
        native = control.controller
        value["controller"] = {
            "state": native.state.value,
            "unit": native.unit,
            "boot_id": native.boot_id,
            "receipt_sha256": native.receipt_sha256,
        }
    return _json(value)


def decode_result(data: bytes) -> ManagedResultControl:
    value = _load(data, MAX_CONTROL_BYTES)
    if set(value) not in ({"version", "facts"}, {"version", "facts", "controller"}) or (
        type(value["version"]) is not int or value["version"] != 1
    ):
        raise ManagedObservationError("invalid managed result")
    try:
        names = value["facts"]
        if type(names) is not list:
            raise ValueError
        if any(type(name) is not str for name in names):
            raise ValueError
        facts = tuple(FactName(name) for name in names)
    except (ValueError, TypeError):
        raise ManagedObservationError("invalid managed result") from None
    if not facts or facts[0] is not FactName.LAUNCH or facts != tuple(name for name in FACT_ORDER if name in facts):
        raise ManagedObservationError("invalid managed result")
    controller = None
    if "controller" in value:
        native = value["controller"]
        if type(native) is not dict or set(native) != {"state", "unit", "boot_id", "receipt_sha256"}:
            raise ManagedObservationError("invalid controller observation")
        try:
            if any(type(item) is not str for item in native.values()):
                raise ValueError
            state = ControllerState(native["state"])
            unit = native["unit"]
            boot = native["boot_id"]
            digest = native["receipt_sha256"]
            if (
                len(unit) != 52
                or not unit.startswith("agw-managed-")
                or not unit.endswith(".service")
                or not valid_nonce(unit[12:-8])
                or str(UUID(boot)) != boot
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ManagedObservationError("invalid controller observation") from None
        controller = ControllerObservation(state, unit, boot, digest)
    return ManagedResultControl(facts, controller)


def checked_controller(controller: ControllerObservation, expected_launch: bytes) -> None:
    """Correlate external native evidence with the complete exact launch."""
    launch = checked_launch(expected_launch)
    target = launch["target"]
    if (
        type(target) is not dict
        or controller.unit != launch["unit"]
        or controller.boot_id != target["boot_id"]
        or controller.receipt_sha256 != wire.launch_sha256(launch)
    ):
        raise ManagedObservationError("controller observation binding mismatch")


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
