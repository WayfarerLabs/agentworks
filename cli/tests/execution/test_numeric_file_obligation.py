"""Durable numeric bootstrap identity and unchanged recovery growth bounds."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_obligation import (
    _FILE_CALL_RECOVERY_HEADROOM_BYTES,
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    NUMERIC_FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    FileCallObligationCodecError,
    decode_file_call_obligation,
    encode_file_call_admission,
    encode_file_call_obligation,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from tests.execution.test_file_obligation import _MAXIMUM, _maximum_recovery_obligation, _obligation

_GUEST = VMGuestIdentity("f" * 32, "123e4567-e89b-12d3-a456-426614174000", _MAXIMUM)
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0, 12)), IdentityMode.SUDO_ROOT)
_BOOTSTRAP = _NumericGuestBootstrap(_ROOT, _GUEST)
_GATED_FAMILIES = (FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD)


def _numeric(family: FileCallFamily = FileCallFamily.STAT) -> FileCallObligation:
    initial = _obligation(family)
    return replace(initial, target=replace(initial.target, boot_id=vm_guest_boot_id(_GUEST)), bootstrap=_BOOTSTRAP)


def _payload(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


@pytest.mark.parametrize("family", list(FileCallFamily))
def test_v1_bytes_remain_exact_and_only_bound_records_select_v2(family: FileCallFamily) -> None:
    legacy = _obligation(family)
    old_bytes = encode_file_call_obligation(legacy)
    numeric = _numeric(family)
    encoded = encode_file_call_obligation(numeric)
    value = json.loads(encoded)

    assert FILE_CALL_OBLIGATION_PAYLOAD_VERSION == legacy.payload_version == 1
    assert NUMERIC_FILE_CALL_OBLIGATION_PAYLOAD_VERSION == numeric.payload_version == value["version"] == 2
    assert decode_file_call_obligation(old_bytes).bootstrap is None
    assert encode_file_call_obligation(decode_file_call_obligation(old_bytes)) == old_bytes
    assert decode_file_call_obligation(encoded) == numeric
    assert encode_file_call_obligation(decode_file_call_obligation(encoded)) == encoded
    assert value["bootstrap"] == {
        "root_entry": {"egid": 0, "euid": 0, "groups": [0, 12], "mode": "sudo_root"},
        "guest": {
            "boot_id": _GUEST.boot_id,
            "instance_marker": _GUEST.instance_marker,
            "init_start_ticks": _GUEST.init_start_ticks,
        },
    }
    del value["bootstrap"]
    value["version"] = 1
    assert _payload(value) == encode_file_call_obligation(replace(numeric, bootstrap=None))


@pytest.mark.parametrize("root_mode", (IdentityMode.DIRECT, IdentityMode.SUDO_ROOT))
@pytest.mark.parametrize("body_uid", (0, 1001))
@pytest.mark.parametrize("python_path", (None, "/usr/bin/python3"))
def test_root_entry_is_independent_of_root_or_nonroot_body(
    root_mode: IdentityMode, body_uid: int, python_path: str | None
) -> None:
    body = IdentityPlan(IdentityExpectation(body_uid, 1002, (1002, 1003)), IdentityMode.DIRECT)
    bootstrap = replace(_BOOTSTRAP, root_entry=replace(_ROOT, mode=root_mode))
    call = replace(
        _numeric(),
        identity_plan=body,
        bootstrap=bootstrap,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, python_path),
    )
    decoded = decode_file_call_obligation(encode_file_call_obligation(call))
    assert decoded.identity_plan == body
    assert decoded.bootstrap == bootstrap


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("version",), True),
        (("version",), 2.0),
        (("version",), "2"),
        (("version",), 0),
        (("version",), 3),
        (("version",), 1),
        (("bootstrap",), None),
        (("bootstrap",), []),
        (("bootstrap", "root_entry"), None),
        (("bootstrap", "root_entry", "euid"), 1001),
        (("bootstrap", "root_entry", "euid"), True),
        (("bootstrap", "root_entry", "egid"), -1),
        (("bootstrap", "root_entry", "groups"), [12, 0]),
        (("bootstrap", "root_entry", "groups"), [0, True]),
        (("bootstrap", "root_entry", "mode"), "demote"),
        (("bootstrap", "root_entry", "mode"), "future"),
        (("bootstrap", "guest"), None),
        (("bootstrap", "guest", "instance_marker"), "invalid"),
        (("bootstrap", "guest", "boot_id"), []),
        (("bootstrap", "guest", "init_start_ticks"), True),
        (("bootstrap", "guest", "init_start_ticks"), _MAXIMUM + 1),
        (("bootstrap", "guest", "init_start_ticks"), 1),
        (("target", "boot_id"), _GUEST.boot_id),
        (("target", "kind"), "platform-host"),
        (("runtime", "target_os"), "darwin"),
        (("runtime", "explicit_path"), "/opt/python3"),
    ],
)
def test_decoder_refuses_malformed_or_mismatched_bootstrap(path: tuple[str, ...], replacement: object) -> None:
    value = json.loads(encode_file_call_obligation(_numeric()))
    nested = value
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = replacement
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(_payload(value))


@pytest.mark.parametrize("path", ((), ("bootstrap",), ("bootstrap", "root_entry"), ("bootstrap", "guest")))
def test_bootstrap_nested_objects_have_closed_required_fields(path: tuple[str, ...]) -> None:
    for remove_field in (False, True):
        value = json.loads(encode_file_call_obligation(_numeric()))
        nested = value
        for key in path:
            nested = nested[key]
        if remove_field:
            del nested[next(iter(nested))]
        else:
            nested["future"] = True
        with pytest.raises(FileCallObligationCodecError):
            decode_file_call_obligation(_payload(value))


def test_v2_requires_bootstrap_and_canonical_encoding() -> None:
    value = json.loads(encode_file_call_obligation(_numeric()))
    del value["bootstrap"]
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(_payload(value))
    canonical = encode_file_call_obligation(_numeric())
    for malformed in (canonical.replace(b"{", b"{ ", 1), canonical.replace(b'"version":2', b'"version":2,"version":2')):
        with pytest.raises(FileCallObligationCodecError):
            decode_file_call_obligation(malformed)


@pytest.mark.parametrize(
    "changes",
    [
        {"bootstrap": object()},
        {"runtime_selection": RuntimeSelection(RuntimeTargetOS.DARWIN)},
        {"runtime_selection": RuntimeSelection(RuntimeTargetOS.LINUX, "/opt/python3")},
        {"target": replace(_numeric().target, kind=ManagedTargetKind.PLATFORM_HOST)},
        {"target": replace(_numeric().target, boot_id=_GUEST.boot_id)},
    ],
)
def test_invalid_typed_numeric_target_and_runtime_raise_codec_error(changes: dict[str, Any]) -> None:
    with pytest.raises(FileCallObligationCodecError):
        replace(_numeric(), **changes)


def _gated(family: FileCallFamily, state: str, body_uid: int = 1001) -> FileCallObligation:
    initial = _numeric(family)
    if body_uid != initial.identity_plan.expected.euid:
        plan = IdentityPlan(IdentityExpectation(body_uid, body_uid, (body_uid,)), IdentityMode.DIRECT)
        initial = replace(initial, identity_plan=plan)
    if state == "none":
        return initial
    setup = FileEffectGateSetup.for_target(initial.target, initial.identity_plan.expected.euid, _GUEST)
    if state == "setup":
        return replace(initial, gate_setup=setup, batch_index=0 if family is FileCallFamily.PACKAGE_UPLOAD else None)
    gate = FileEffectGateBinding(
        setup.path,
        b"i" * 16,
        b"g" * 16,
        _GUEST,
        initial.identity_plan.expected.euid,
        initial.target.name,
        _MAXIMUM,
        _MAXIMUM,
        b"p" * 16 if state == "proposed" else None,
    )
    return replace(initial, effect_gate=gate)


@pytest.mark.parametrize("family", _GATED_FAMILIES)
@pytest.mark.parametrize("state", ("setup", "bound", "proposed"))
def test_bootstrap_requires_full_gate_guest_not_only_derived_boot(family: FileCallFamily, state: str) -> None:
    initial = _gated(family, state)
    other_guest = replace(_GUEST, instance_marker="e" * 32)
    assert vm_guest_boot_id(other_guest) == initial.target.boot_id
    with pytest.raises(FileCallObligationCodecError):
        replace(initial, bootstrap=replace(_BOOTSTRAP, guest=other_guest))
    value = json.loads(encode_file_call_obligation(initial))
    value["bootstrap"]["guest"]["instance_marker"] = other_guest.instance_marker
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(_payload(value))


def _maximum_recovery(initial: FileCallObligation) -> FileCallObligation:
    if initial.gate_setup is not None:
        gate = _gated(initial.family, "proposed", initial.identity_plan.expected.euid).effect_gate
        initial = replace(initial, gate_setup=None, effect_gate=gate)
    elif initial.effect_gate is not None:
        initial = replace(initial, effect_gate=replace(initial.effect_gate, proposed_generation=b"p" * 16))
    return _maximum_recovery_obligation(initial.family, initial=initial)


def _with_length(initial: FileCallObligation, length: int) -> FileCallObligation:
    baseline = replace(initial, root="/", relative_path="a")
    padding = length - len(encode_file_call_obligation(baseline))
    relative_padding = min(padding, 4095)
    root_padding = padding - relative_padding
    assert 0 <= root_padding <= 4095
    return replace(baseline, root="/" + "a" * root_padding, relative_path="a" * (relative_padding + 1))


@pytest.mark.parametrize(
    ("family", "state"),
    [(family, "none") for family in FileCallFamily]
    + [(family, state) for family in _GATED_FAMILIES for state in ("setup", "bound", "proposed")],
)
@pytest.mark.parametrize("body_uid", (0, 1001, (1 << 32) - 1))
def test_v2_exact_admission_and_maximum_retained_envelope(family: FileCallFamily, state: str, body_uid: int) -> None:
    initial = _gated(family, state, body_uid)
    maximum = _maximum_recovery(initial)
    legacy = replace(initial, bootstrap=None)
    legacy_maximum = replace(maximum, bootstrap=None)
    growth = len(encode_file_call_obligation(maximum)) - len(encode_file_call_obligation(initial))
    assert growth == len(encode_file_call_obligation(legacy_maximum)) - len(encode_file_call_obligation(legacy))
    reserve = _FILE_CALL_RECOVERY_HEADROOM_BYTES[family]
    proposal_size = len(',"proposed_generation":"' + "0" * 32 + '"')
    if state == "setup":
        reserve = growth
    elif state in ("bound", "proposed"):
        reserve += proposal_size
    else:
        assert growth == reserve
    # Already-proposed rows retain the existing conservative extra proposal reserve.
    slack = proposal_size if state == "proposed" else 0
    assert reserve - growth == slack
    admitted = _with_length(initial, MAX_LIFECYCLE_PAYLOAD_BYTES - reserve)
    refused = replace(admitted, root=admitted.root + "a")
    assert len(encode_file_call_admission(admitted)) == MAX_LIFECYCLE_PAYLOAD_BYTES - reserve
    retained = encode_file_call_obligation(_maximum_recovery(admitted))
    assert len(retained) == MAX_LIFECYCLE_PAYLOAD_BYTES - slack
    assert decode_file_call_obligation(retained) == _maximum_recovery(admitted)
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_admission(refused)
    if not slack:
        with pytest.raises(FileCallObligationCodecError):
            encode_file_call_obligation(_maximum_recovery(refused))


def test_v2_envelope_accepts_8192_and_refuses_8193() -> None:
    exact = _with_length(_numeric(), MAX_LIFECYCLE_PAYLOAD_BYTES)
    assert len(encode_file_call_obligation(exact)) == MAX_LIFECYCLE_PAYLOAD_BYTES
    assert decode_file_call_obligation(encode_file_call_obligation(exact)) == exact
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_obligation(replace(exact, root=exact.root + "a"))
