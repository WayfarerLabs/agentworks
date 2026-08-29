"""VM applied-state codecs, comparison facts, and policy boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.db import AppliedStateKey, AppliedStateSlice, Database, VersionedPayload
from agentworks.errors import ConfigError, StateError
from agentworks.ssh_identity import UnverifiableSSHIdentity, VerifiedSSHIdentity
from agentworks.vms import applied_state
from agentworks.vms.applied_state import (
    HardwareProvenance,
    UnverifiableSSHAppliedState,
    VerifiedSSHAppliedState,
    VMSSHIdentityComparison,
    VMSSHIdentityState,
)
from agentworks.vms.initializer.ssh_keys import AuthorizedKeysApplied

_FINGERPRINT = f"SHA256:{'A' * 43}"
_OTHER_FINGERPRINT = f"SHA256:{'E' * 43}"


def _slice(key: AppliedStateKey, payload: VersionedPayload) -> AppliedStateSlice:
    return AppliedStateSlice("vm", "alpha", key, payload, "vm-create", "2026-08-28T12:00:00Z")


def test_hardware_marker_codec_is_strict_and_empty() -> None:
    payload = applied_state.encode_hardware_provenance(HardwareProvenance())
    assert payload == VersionedPayload(1, {})
    assert applied_state.decode_hardware_provenance(_slice(AppliedStateKey.HARDWARE_PROVENANCE, payload)) == (
        HardwareProvenance()
    )

    with pytest.raises(StateError):
        applied_state.decode_hardware_provenance(
            _slice(AppliedStateKey.HARDWARE_PROVENANCE, VersionedPayload(1, {"unexpected": True}))
        )


def test_hardware_marker_codec_refuses_an_unsupported_version_as_version_skew() -> None:
    record = _slice(AppliedStateKey.HARDWARE_PROVENANCE, VersionedPayload(2, {}))

    with pytest.raises(StateError) as caught:
        applied_state.decode_hardware_provenance(record)

    assert type(caught.value) is StateError
    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "alpha"
    assert caught.value.hint is not None


@pytest.mark.parametrize(
    ("identity", "expected_value", "expected_decoded"),
    [
        (
            VerifiedSSHIdentity(_FINGERPRINT),
            {"fingerprint": _FINGERPRINT, "private_key_ref": "/keys/id", "status": "verified"},
            VerifiedSSHAppliedState("/keys/id", _FINGERPRINT),
        ),
        (
            UnverifiableSSHIdentity(),
            {"private_key_ref": "/keys/id", "status": "unverifiable"},
            UnverifiableSSHAppliedState("/keys/id"),
        ),
    ],
)
def test_ssh_payload_codec_round_trips_closed_arms(
    identity: VerifiedSSHIdentity | UnverifiableSSHIdentity,
    expected_value: dict[str, str],
    expected_decoded: VerifiedSSHAppliedState | UnverifiableSSHAppliedState,
) -> None:
    payload = applied_state.encode_ssh_identity("/keys/id", identity)
    assert payload == VersionedPayload(1, expected_value)
    assert applied_state.decode_ssh_identity(_slice(AppliedStateKey.SSH_IDENTITY, payload)) == expected_decoded


@pytest.mark.parametrize(
    "value",
    [
        {"private_key_ref": "/keys/id"},
        {"private_key_ref": "/keys/id", "status": "other"},
        {"private_key_ref": "/keys/id", "status": "verified"},
        {"fingerprint": _FINGERPRINT, "private_key_ref": "/keys/id", "status": "unverifiable"},
        {"private_key_ref": "/keys/id", "status": "unverifiable", "reason": "legacy"},
        {"fingerprint": "SHA256:short", "private_key_ref": "/keys/id", "status": "verified"},
        {"fingerprint": f"SHA256:{'A' * 42}B", "private_key_ref": "/keys/id", "status": "verified"},
        {"fingerprint": _FINGERPRINT, "private_key_ref": "", "status": "verified"},
    ],
)
def test_ssh_payload_decoder_rejects_malformed_or_extra_fields(value: dict[str, str]) -> None:
    with pytest.raises(StateError):
        applied_state.decode_ssh_identity(_slice(AppliedStateKey.SSH_IDENTITY, VersionedPayload(1, value)))


def test_ssh_payload_codec_refuses_an_unsupported_version_before_decoding_value() -> None:
    sensitive_value = "do-not-render-this-forward-payload"
    record = _slice(
        AppliedStateKey.SSH_IDENTITY,
        VersionedPayload(2, {"future_field": sensitive_value}),
    )

    with pytest.raises(StateError) as caught:
        applied_state.decode_ssh_identity(record)

    assert type(caught.value) is StateError
    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "alpha"
    assert caught.value.hint is not None
    assert sensitive_value not in str(caught.value)


def test_prepare_configured_identity_retains_public_text_and_refuses_verified_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_path = Path("/keys/id.pub")
    private_path = Path("/keys/id")
    monkeypatch.setattr(applied_state, "_read_public_key_text", lambda path: "ssh-ed25519 AAAA comment")
    monkeypatch.setattr(applied_state, "parse_public_ssh_identity", lambda text: VerifiedSSHIdentity(_FINGERPRINT))
    monkeypatch.setattr(applied_state, "read_private_ssh_identity", lambda path: VerifiedSSHIdentity(_FINGERPRINT))

    prepared = applied_state.prepare_configured_ssh_identity(public_path, private_path)

    assert prepared.public_text == "ssh-ed25519 AAAA comment"
    assert prepared.private_key_ref == "/keys/id"
    assert prepared.identity == VerifiedSSHIdentity(_FINGERPRINT)

    monkeypatch.setattr(
        applied_state,
        "read_private_ssh_identity",
        lambda path: VerifiedSSHIdentity(_OTHER_FINGERPRINT),
    )
    with pytest.raises(ConfigError):
        applied_state.prepare_configured_ssh_identity(public_path, private_path)


def test_initialization_slice_builder_uses_post_write_private_key_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_paths: list[Path] = []

    def read_identity(path: Path) -> VerifiedSSHIdentity:
        read_paths.append(path)
        return VerifiedSSHIdentity(_FINGERPRINT)

    monkeypatch.setattr(applied_state, "read_private_ssh_identity", read_identity)

    slices = applied_state.build_vm_initialization_slices(
        AuthorizedKeysApplied(VerifiedSSHIdentity(_FINGERPRINT), "/proof/key"),
        include_hardware=True,
    )

    assert read_paths == [Path("/proof/key")]
    assert slices == {
        AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {}),
        AppliedStateKey.SSH_IDENTITY: VersionedPayload(
            1,
            {"fingerprint": _FINGERPRINT, "private_key_ref": "/proof/key", "status": "verified"},
        ),
    }


@pytest.mark.parametrize(
    ("persisted", "current", "expected"),
    [
        (None, VerifiedSSHIdentity(_FINGERPRINT), VMSSHIdentityState.NOT_RECORDED),
        (VerifiedSSHAppliedState("/old", _FINGERPRINT), VerifiedSSHIdentity(_FINGERPRINT), VMSSHIdentityState.MATCH),
        (
            VerifiedSSHAppliedState("/old", _FINGERPRINT),
            VerifiedSSHIdentity(_OTHER_FINGERPRINT),
            VMSSHIdentityState.DRIFT,
        ),
        (VerifiedSSHAppliedState("/old", _FINGERPRINT), UnverifiableSSHIdentity(), VMSSHIdentityState.UNVERIFIABLE),
        (UnverifiableSSHAppliedState("/old"), VerifiedSSHIdentity(_FINGERPRINT), VMSSHIdentityState.UNVERIFIABLE),
    ],
)
def test_compare_vm_ssh_identity_returns_structural_fact(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    persisted: VerifiedSSHAppliedState | UnverifiableSSHAppliedState | None,
    current: VerifiedSSHIdentity | UnverifiableSSHIdentity,
    expected: VMSSHIdentityState,
) -> None:
    if persisted is not None:
        identity = (
            VerifiedSSHIdentity(persisted.fingerprint)
            if isinstance(persisted, VerifiedSSHAppliedState)
            else UnverifiableSSHIdentity()
        )
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {AppliedStateKey.SSH_IDENTITY: applied_state.encode_ssh_identity(persisted.private_key_ref, identity)},
        )
    monkeypatch.setattr(applied_state, "read_private_ssh_identity", lambda path: current)

    comparison = applied_state.compare_vm_ssh_identity(db, "alpha", Path("/new"))

    assert comparison.state is expected


def test_require_vm_ssh_identity_distinguishes_ordinary_and_establishment_policy() -> None:
    absent = VMSSHIdentityComparison(VMSSHIdentityState.NOT_RECORDED, "alpha")
    applied_state.require_vm_ssh_identity(absent, allow_not_recorded=True)
    with pytest.raises(StateError):
        applied_state.require_vm_ssh_identity(absent)

    drift = VMSSHIdentityComparison(VMSSHIdentityState.DRIFT, "alpha")
    with pytest.raises(StateError):
        applied_state.require_vm_ssh_identity(drift, allow_not_recorded=True)

    applied_state.require_vm_ssh_identity(VMSSHIdentityComparison(VMSSHIdentityState.UNVERIFIABLE, "alpha"))
