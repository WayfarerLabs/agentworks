"""Closed primitive envelope for one managed foreground relay."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from ._helper_identity import IdentityExpectation, decode_identity
from ._managed_job_request import ManagedLiveIO, RequestError, decode_request_launch
from ._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

LIVE_SOCKET_NAME = "live-io.sock"
HANDSHAKE_BYTES = 43
ENVELOPE_HEADER_BYTES = 13
MAX_REQUEST_BYTES = 16384
_MARKER = b"AGWR1"


class ManagedLiveError(ValueError):
    """Invalid relay binding, without retaining request or application bytes."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedLiveRequest:
    live_io: ManagedLiveIO
    identity: IdentityExpectation
    guest: VMGuestIdentity
    expected_launch: bytes


def encode_handshake(live_io: ManagedLiveIO) -> bytes:
    """Encode the protected directions and correlation for controller admission."""
    if type(live_io) is not ManagedLiveIO:
        raise ManagedLiveError("invalid live metadata")
    return f"AGWL1 {live_io.nonce} {int(live_io.input)} {int(live_io.output)}\n".encode("ascii")


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def checked_launch(request: ManagedLiveRequest) -> dict[str, object]:
    """Bind external relay evidence to one root helper and operation VM launch."""
    try:
        if (
            type(request) is not ManagedLiveRequest
            or type(request.live_io) is not ManagedLiveIO
            or type(request.identity) is not IdentityExpectation
            or type(request.guest) is not VMGuestIdentity
        ):
            raise ValueError
        identity = decode_identity(
            {"euid": request.identity.euid, "egid": request.identity.egid, "groups": list(request.identity.groups)}
        )
        launch = decode_request_launch(request.expected_launch)
        target = launch["target"]
        if (
            identity.euid != 0
            or launch["lifetime"] != "operation"
            or type(target) is not dict
            or target["kind"] != "vm"
            or target["boot_id"] != vm_guest_boot_id(request.guest)
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ManagedLiveError("invalid managed live binding") from None
    return launch


def encode_request(request: ManagedLiveRequest) -> bytes:
    checked_launch(request)
    data = _json(
        {
            "version": 1,
            "live_io": {
                "nonce": request.live_io.nonce,
                "input": request.live_io.input,
                "output": request.live_io.output,
            },
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
            "expected_launch": base64.b64encode(request.expected_launch).decode("ascii"),
        }
    )
    if len(data) > MAX_REQUEST_BYTES:
        raise ManagedLiveError("managed live envelope exceeds bound")
    return data


def decode_request(data: bytes) -> ManagedLiveRequest:
    """Validate canonical closed JSON received separately from the live suffix."""
    try:
        if type(data) is not bytes or len(data) > MAX_REQUEST_BYTES:
            raise ValueError
        value = json.loads(data.decode("ascii"))
        if (
            type(value) is not dict
            or _json(value) != data
            or set(value) != {"version", "live_io", "identity", "guest", "expected_launch"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError
        live = value["live_io"]
        guest = value["guest"]
        if type(live) is not dict or set(live) != {"nonce", "input", "output"}:
            raise ValueError
        if type(guest) is not dict or set(guest) != {"instance_marker", "boot_id", "init_start_ticks"}:
            raise ValueError
        encoded = value["expected_launch"]
        if type(encoded) is not str or len(encoded) > 5464:
            raise ValueError
        launch = base64.b64decode(encoded.encode("ascii"), validate=True)
        if base64.b64encode(launch).decode("ascii") != encoded:
            raise ValueError
        request = ManagedLiveRequest(
            ManagedLiveIO(live["nonce"], live["input"], live["output"]),
            decode_identity(value["identity"]),
            VMGuestIdentity(guest["instance_marker"], guest["boot_id"], guest["init_start_ticks"]),
            launch,
        )
        checked_launch(request)
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError, binascii.Error, RequestError):
        raise ManagedLiveError("invalid managed live envelope") from None
    return request


def encode_envelope(request: ManagedLiveRequest) -> bytes:
    body = encode_request(request)
    return _MARKER + f"{len(body):08x}".encode("ascii") + body


def decode_header(header: bytes) -> int:
    if (
        type(header) is not bytes
        or len(header) != ENVELOPE_HEADER_BYTES
        or not header.startswith(_MARKER)
        or any(byte not in b"0123456789abcdef" for byte in header[5:])
    ):
        raise ManagedLiveError("invalid managed live envelope header")
    length = int(header[5:], 16)
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise ManagedLiveError("managed live envelope exceeds bound")
    return length
