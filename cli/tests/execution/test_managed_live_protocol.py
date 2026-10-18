"""Closed relay requests bind root, operation, guest boot and live directions."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_request import ManagedLiveIO
from agentworks.execution._managed_live_protocol import (
    ENVELOPE_HEADER_BYTES,
    HANDSHAKE_BYTES,
    MAX_REQUEST_BYTES,
    ManagedLiveError,
    ManagedLiveRequest,
    decode_header,
    decode_request,
    encode_envelope,
    encode_handshake,
    encode_request,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

NONCE = "b" * 32
RUN = "a" * 32
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)


def _request(*, input: bool = True, output: bool = True) -> ManagedLiveRequest:
    launch = wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {
                "kind": "vm",
                "name": "vm-one",
                "incarnation": "v1:" + "c" * 64,
                "boot_id": vm_guest_boot_id(GUEST),
            },
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "operation", "owner_id": "e" * 32},
            "lifetime": "operation",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )
    return ManagedLiveRequest(
        ManagedLiveIO(NONCE, input, output),
        IdentityExpectation(0, 0, (0,)),
        GUEST,
        launch,
        remaining_ns=30_000_000_000,
    )


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@pytest.mark.parametrize(("input", "output"), [(True, True), (True, False), (False, True)])
def test_canonical_request_envelope_and_controller_handshake(input: bool, output: bool) -> None:
    request = _request(input=input, output=output)
    body = encode_request(request)
    assert decode_request(body) == request
    envelope = encode_envelope(request)
    assert decode_header(envelope[:ENVELOPE_HEADER_BYTES]) == len(body)
    assert envelope[ENVELOPE_HEADER_BYTES:] == body
    handshake = encode_handshake(request.live_io)
    assert len(handshake) == HANDSHAKE_BYTES
    assert handshake == f"AGWL1 {NONCE} {int(input)} {int(output)}\n".encode()
    assert NONCE not in repr(request) and RUN not in repr(request)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"remaining_ns": True},
        {"remaining_ns": 0},
        {"remaining_ns": -1},
        {"remaining_ns": 2**63},
        {"remaining_ns": 1.5},
        {"remaining_ns": "1000000000"},
        {"remaining_ns": None},
        {"nonce": NONCE},
        {"live_io": {"nonce": NONCE, "input": 1, "output": True}},
        {"live_io": {"nonce": NONCE, "input": False, "output": False}},
        {"live_io": {"nonce": NONCE.upper(), "input": True, "output": True}},
        {"live_io": {"nonce": NONCE, "input": True, "output": True, "path": "/tmp/socket"}},
        {"identity": {"euid": 1, "egid": 0, "groups": [0]}},
        {"identity": {"euid": 0, "egid": 0, "groups": [0, 0]}},
        {"guest": {"instance_marker": "d" * 32, "boot_id": GUEST.boot_id, "init_start_ticks": True}},
        {"expected_launch": "?"},
        {"expected_launch": base64.b64encode(b"secret-invalid-launch").decode()},
        {"expected_launch": base64.b64encode(b"x" * 4097).decode()},
    ],
)
def test_closed_exact_types_and_bounds(change: dict[str, object]) -> None:
    value = json.loads(encode_request(_request()))
    value.update(change)
    with pytest.raises(ManagedLiveError) as caught:
        decode_request(_json(value))
    assert "secret-invalid-launch" not in str(caught.value)


@pytest.mark.parametrize("variant", ["independent", "host", "boot", "interactive"])
def test_launch_must_be_operation_vm_and_exact_guest_boot(variant: str) -> None:
    request = _request()
    launch = wire.decode_fact(request.expected_launch)
    if variant == "independent":
        launch.update(lifetime="independent", owner={"kind": "resource", "owner_id": "session"})
    elif variant == "host":
        launch["target"]["kind"] = "platform-host"  # type: ignore[index]
    elif variant == "boot":
        launch["target"]["boot_id"] = "00000000-0000-4000-8000-000000000002"  # type: ignore[index]
    else:
        launch["shell"] = {"requested": "sh", "resolved_executable": "/bin/sh", "login": False, "interactive": True}
    with pytest.raises(ManagedLiveError):
        encode_request(replace(request, expected_launch=wire.encode_fact(launch)))


def test_noncanonical_truncated_duplicate_and_oversized_json_refuse() -> None:
    data = encode_request(_request())
    for invalid in (data + b" ", data[:-1], b'{"version":1,' + data[1:], b"\xff", b"x" * (MAX_REQUEST_BYTES + 1)):
        with pytest.raises(ManagedLiveError):
            decode_request(invalid)


def test_observation_budget_is_required_and_bounded_at_construction_and_wire() -> None:
    request = _request()
    for duration in (1, 2**63 - 1):
        bounded = replace(request, remaining_ns=duration)
        assert decode_request(encode_request(bounded)) == bounded
    for invalid in (True, 0, -1, 2**63, 1.5, "1000000000", None):
        with pytest.raises(ManagedLiveError):
            replace(request, remaining_ns=invalid)
    value = json.loads(encode_request(request))
    del value["remaining_ns"]
    with pytest.raises(ManagedLiveError):
        decode_request(_json(value))


@pytest.mark.parametrize("header", [b"", b"AGWR10000000A", b"AGWR100000000", b"AGWR100004001", b"AGWR200000001"])
def test_envelope_header_is_exact_and_bounded(header: bytes) -> None:
    with pytest.raises(ManagedLiveError):
        decode_header(header)
