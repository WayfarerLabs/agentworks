"""Closed-schema checks for the private VM guest identity wire."""

from __future__ import annotations

import json

import pytest

from agentworks.execution._vm_guest_identity_protocol import (
    MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES,
    VM_BOOT_ID_PATH,
    VM_INSTANCE_MARKER_PATH,
    VMGuestIdentity,
    VMGuestIdentityFailure,
    VMGuestIdentityResponseError,
    decode_vm_guest_identity_response,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)

NONCE = "0123456789abcdef0123456789abcdef"
BOOT_ID = "12345678-1234-1234-1234-123456789abc"
MARKER = "a" * 32


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def test_fixed_paths_and_identity_shape_are_closed() -> None:
    assert VM_INSTANCE_MARKER_PATH == "/var/lib/agentworks/instance-id"
    assert VM_BOOT_ID_PATH == "/proc/sys/kernel/random/boot_id"
    identity = VMGuestIdentity(MARKER, BOOT_ID)
    assert identity.instance_marker == MARKER
    assert identity.boot_id == BOOT_ID


@pytest.mark.parametrize(
    "identity",
    [
        ("A" * 32, BOOT_ID),
        ("a" * 31, BOOT_ID),
        (MARKER, "12345678-1234-1234-1234-123456789ABc"),
        (MARKER, "123456781234-1234-1234-1234-123456789abc"),
    ],
)
def test_identity_rejects_noncanonical_values(identity: tuple[str, str]) -> None:
    with pytest.raises(ValueError):
        VMGuestIdentity(*identity)


def test_success_and_typed_refusal_round_trip() -> None:
    identity = VMGuestIdentity(MARKER, BOOT_ID)
    encoded = encode_vm_guest_identity_success(NONCE, identity)
    assert decode_vm_guest_identity_response(encoded, NONCE).identity == identity

    refusal = encode_vm_guest_identity_failure(NONCE, VMGuestIdentityFailure.MARKER_MISSING)
    decoded = decode_vm_guest_identity_response(refusal, NONCE)
    assert decoded.identity is None
    assert decoded.failure is VMGuestIdentityFailure.MARKER_MISSING


@pytest.mark.parametrize(
    "response",
    [
        b'{"boot_id":"12345678-1234-1234-1234-123456789abc",'
        b'"instance_marker":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"nonce":"0123456789abcdef0123456789abcdef",'
        b'"status":"identity","version":1,"version":1}',
        _json(
            {
                "boot_id": BOOT_ID,
                "instance_marker": MARKER,
                "nonce": NONCE,
                "status": "identity",
                "version": 1,
                "reflected": "private-byte-canary",
            }
        ),
        encode_vm_guest_identity_success(NONCE, VMGuestIdentity(MARKER, BOOT_ID)) + b"\n",
        encode_vm_guest_identity_success(NONCE, VMGuestIdentity(MARKER, BOOT_ID)).replace(NONCE.encode(), b"f" * 32),
        b"x" * (MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES + 1),
    ],
)
def test_response_rejects_duplicate_reflected_trailing_wrong_nonce_and_oversize(response: bytes) -> None:
    with pytest.raises(VMGuestIdentityResponseError):
        decode_vm_guest_identity_response(response, NONCE)
