"""Persisted prepared-helper identity is bounded cleanup metadata only."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._prepared_helper_obligation import (
    PreparedHelperObligation,
    PreparedHelperObligationError,
    decode_prepared_helper_obligation,
    encode_prepared_helper_obligation,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

GUEST = VMGuestIdentity("a" * 32, "00000000-0000-4000-8000-000000000001", 123)
ROOT = IdentityPlan(IdentityExpectation(0, 0, (0, 7)), IdentityMode.SUDO_ROOT)
TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "stage-vm", "v1:" + "b" * 64, vm_guest_boot_id(GUEST))
RECORD = PreparedHelperObligation(
    TARGET, _NumericGuestBootstrap(ROOT, GUEST), RuntimeSelection(RuntimeTargetOS.LINUX), bytes(range(16))
)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def test_roundtrip_and_payload_has_only_cleanup_identity() -> None:
    payload = encode_prepared_helper_obligation(RECORD)
    assert decode_prepared_helper_obligation(payload) == RECORD
    assert len(payload) <= MAX_LIFECYCLE_PAYLOAD_BYTES
    assert set(json.loads(payload)) == {"version", "target", "bootstrap", "runtime", "token"}
    assert RECORD.token.hex() not in repr(RECORD)


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_closed_version(version: object) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    value["version"] = version
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


@pytest.mark.parametrize("token", ["", "00" * 15, "00" * 17, "AB" * 16, "gg" * 16, 1, None])
def test_strict_token(token: object) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    value["token"] = token
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


@pytest.mark.parametrize("field", ["payload", "sha256", "source", "path", "reference", "command", "connection"])
def test_unexpected_activation_or_payload_fields_refused(field: str) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    value[field] = "secret-canary"
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        ("target", "kind", "container"),
        ("target", "boot_id", "00000000-0000-4000-8000-000000000002"),
        ("target", "incarnation", "unknown"),
        ("runtime", "target_os", "darwin"),
        ("runtime", "explicit_path", "/caller/python"),
    ],
)
def test_target_and_runtime_binding(section: str, field: str, replacement: object) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    value[section][field] = replacement
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("euid", 1), ("euid", True), ("groups", [7, 0]), ("groups", [7]), ("mode", "demote")],
)
def test_full_root_identity(field: str, replacement: object) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    value["bootstrap"]["root_entry"][field] = replacement
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
def test_full_guest_identity(field: str) -> None:
    value = json.loads(encode_prepared_helper_obligation(RECORD))
    del value["bootstrap"]["guest"][field]
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(_json(value))


def test_canonical_and_database_bounds() -> None:
    payload = encode_prepared_helper_obligation(RECORD)
    for malformed in (
        b"",
        payload[:-1],
        payload + b" ",
        b" " + payload,
        b"[1]",
        payload.replace(b'"version":1', b'"version":1,"version":1'),
    ):
        with pytest.raises(PreparedHelperObligationError):
            decode_prepared_helper_obligation(malformed)
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(b" " * (MAX_LIFECYCLE_PAYLOAD_BYTES + 1))
    huge_root = IdentityPlan(IdentityExpectation(0, 0, tuple(range(3000))), IdentityMode.DIRECT)
    with pytest.raises(PreparedHelperObligationError):
        encode_prepared_helper_obligation(replace(RECORD, bootstrap=_NumericGuestBootstrap(huge_root, GUEST)))


def test_constructor_record_cannot_bypass_boot_binding() -> None:
    with pytest.raises(PreparedHelperObligationError):
        encode_prepared_helper_obligation(replace(RECORD, token=b"short"))
    with pytest.raises(PreparedHelperObligationError):
        encode_prepared_helper_obligation(replace(RECORD, target=replace(TARGET, boot_id=GUEST.boot_id)))


@pytest.mark.parametrize("payload", [b"[" * 1100 + b"0" + b"]" * 1100, b'"\xff"', b'{"token":1e9999}'])
def test_malformed_bounded_json_errors_are_closed(payload: bytes) -> None:
    assert len(payload) <= MAX_LIFECYCLE_PAYLOAD_BYTES
    with pytest.raises(PreparedHelperObligationError):
        decode_prepared_helper_obligation(payload)
