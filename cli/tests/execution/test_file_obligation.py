"""Recovery-payload coverage for private file-call lifecycle obligations."""

from __future__ import annotations

import json
import sys
from dataclasses import replace

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.execution._file_effect_gate import FileEffectGateBinding, FileEffectGateError
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
from agentworks.execution._file_obligation import (
    _FILE_CALL_RECOVERY_HEADROOM_BYTES,
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    FileCallObligationCodecError,
    FileCallUncertainty,
    decode_file_call_obligation,
    encode_file_call_admission,
    encode_file_call_obligation,
)
from agentworks.execution._file_publication import PublicationCleanupDebt
from agentworks.execution._file_publication_wire import bind_publication_cleanup_debt
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._publication_receipt import (
    _RECORD_BUILD_MODE,
    _RECORD_MODE,
    PublicationStageCleanupDebt,
    PublicationStageOwnership,
    publication_stage_name,
)
from agentworks.execution._publication_receipt import (
    _Identity as PublicationIdentity,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import (
    _RECEIPT_BUILD_MODE,
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchOperation,
    ScratchOwnership,
    ScratchReceiptContext,
    scratch_name,
)
from agentworks.execution._scratch_receipt import (
    _Identity as ScratchIdentity,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

_TOKEN = bytes(range(16))
_TARGET = ManagedTargetIdentity(
    ManagedTargetKind.VM,
    "fixture-vm",
    "v1:" + "a" * 64,
    "123e4567-e89b-12d3-a456-426614174000",
)
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_MAXIMUM = (1 << 64) - 1


def test_guest_gate_path_uses_posix_normalization_on_any_controller_os() -> None:
    guest = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1)
    path = "/run/agentworks/file-gates-v1/fence\\name.db"
    binding = FileEffectGateBinding(path, _TOKEN, b"g" * 16, guest, 1001, "fixture-vm", 1, 2)
    assert binding.path == path
    with pytest.raises(FileEffectGateError):
        replace(binding, path="/run/agentworks/../file-gates-v1/fence.db")


def _context(family: FileCallFamily, plan: IdentityPlan = _PLAN) -> ScratchReceiptContext:
    operation = ScratchOperation.SNAPSHOT if family is FileCallFamily.DOWNLOAD else ScratchOperation.STAGE
    return ScratchReceiptContext(operation, plan.expected)


def _reference(family: FileCallFamily, *, token: bytes = _TOKEN, plan: IdentityPlan = _PLAN) -> ScratchReference:
    return ScratchReference(
        ScratchOwnership(
            token,
            _context(family, plan),
            ScratchIdentity(1, 2),
            ScratchIdentity(1, 3),
            ScratchIdentity(1, 4),
            1002,
            23,
            ScratchIdentity(1, 5),
        )
    )


def _cleanup_debt(token: bytes = _TOKEN, plan: IdentityPlan = _PLAN) -> ScratchCleanupDebt:
    return ScratchCleanupDebt(
        scratch_name(token),
        ScratchIdentity(1, 2),
        ScratchIdentity(1, 3),
        ScratchIdentity(1, 4),
        ScratchIdentity(1, 5),
        (_RECEIPT_MODE,),
        plan.expected.euid,
        1002,
    )


def _maximum_reference(family: FileCallFamily, plan: IdentityPlan = _PLAN) -> ScratchReference:
    identity = ScratchIdentity(_MAXIMUM, _MAXIMUM)
    return ScratchReference(
        ScratchOwnership(
            _TOKEN,
            _context(family, plan),
            identity,
            identity,
            identity,
            _MAXIMUM,
            (1 << 63) - 1,
            identity,
        )
    )


def _maximum_cleanup_debt(plan: IdentityPlan = _PLAN) -> ScratchCleanupDebt:
    identity = ScratchIdentity(_MAXIMUM, _MAXIMUM)
    return ScratchCleanupDebt(
        scratch_name(_TOKEN),
        identity,
        identity,
        identity,
        identity,
        (_RECEIPT_BUILD_MODE, _RECEIPT_MODE),
        plan.expected.euid,
        _MAXIMUM,
    )


def _maximum_publication_debt(reference: ScratchReference):
    identity = PublicationIdentity(_MAXIMUM, _MAXIMUM)
    ownership = PublicationStageOwnership(
        reference._ownership,
        identity,
        publication_stage_name(_TOKEN),
        identity,
        identity,
        (_RECORD_BUILD_MODE, _RECORD_MODE),
    )
    return bind_publication_cleanup_debt(reference, identity, PublicationStageCleanupDebt(ownership, False))


def _obligation(family: FileCallFamily, *, token: bytes | None = None) -> FileCallObligation:
    has_scratch = family in {
        FileCallFamily.DOWNLOAD,
        FileCallFamily.UPLOAD,
        FileCallFamily.PACKAGE_UPLOAD,
        FileCallFamily.JSON_UPDATE,
    }
    selected_token = _TOKEN if has_scratch and family is not FileCallFamily.JSON_UPDATE else token
    return FileCallObligation(
        family=family,
        target=_TARGET,
        root="/srv/agentworks",
        relative_path="settings/naive-cafe.json",
        identity_plan=_PLAN,
        runtime_selection=_RUNTIME,
        token=selected_token,
        attempt=None,
        batch_index=4095 if family is FileCallFamily.PACKAGE_UPLOAD else None,
    )


@pytest.mark.parametrize("family", list(FileCallFamily))
def test_every_file_family_has_a_deterministic_typed_round_trip(family: FileCallFamily) -> None:
    obligation = _obligation(family)

    encoded = encode_file_call_obligation(obligation)

    assert encoded.isascii()
    assert decode_file_call_obligation(encoded) == obligation
    assert encode_file_call_obligation(decode_file_call_obligation(encoded)) == encoded


def test_unicode_paths_are_ascii_escaped_and_reconstruct_exactly() -> None:
    obligation = replace(_obligation(FileCallFamily.STAT), root="/srv/cafe", relative_path="dossier/naive-cafe.json")
    unicode_obligation = replace(
        obligation, root="/srv/caf\u00e9", relative_path="dossier/na\u00efve/\u6587\u4ef6.json"
    )

    encoded = encode_file_call_obligation(unicode_obligation)

    assert encoded.isascii()
    assert decode_file_call_obligation(encoded) == unicode_obligation


def test_target_identity_plan_and_runtime_selection_are_preserved_exactly() -> None:
    obligation = replace(
        _obligation(FileCallFamily.STAT),
        target=ManagedTargetIdentity(
            ManagedTargetKind.PLATFORM_HOST,
            "host-01",
            "v1:" + "b" * 64,
            "123e4567-e89b-12d3-a456-426614174001",
        ),
        identity_plan=IdentityPlan(IdentityExpectation(0, 0, (0, 12)), IdentityMode.SUDO_ROOT),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.DARWIN),
    )

    decoded = decode_file_call_obligation(encode_file_call_obligation(obligation))

    assert decoded.target == obligation.target
    assert decoded.identity_plan == obligation.identity_plan
    assert decoded.runtime_selection == obligation.runtime_selection


def test_scratch_and_publication_cleanup_debts_round_trip_with_their_exact_reference() -> None:
    reference = _reference(FileCallFamily.UPLOAD)
    publication_debt = bind_publication_cleanup_debt(
        reference,
        PublicationIdentity(1, 2),
        PublicationCleanupDebt(publication_stage_name(_TOKEN), 1, 6),
    )
    obligation = replace(
        _obligation(FileCallFamily.UPLOAD),
        scratch_reference=reference,
        scratch_cleanup_debt=_cleanup_debt(),
        publication_cleanup_debt=publication_debt,
        uncertainty=frozenset(
            {
                FileCallUncertainty.PENDING_REMOTE_EFFECT,
                FileCallUncertainty.COORDINATION_UNCERTAINTY,
                FileCallUncertainty.PUBLICATION_OWNERSHIP,
            }
        ),
    )

    decoded = decode_file_call_obligation(encode_file_call_obligation(obligation))

    assert decoded == obligation
    assert decoded.publication_cleanup_debt is not None
    assert decoded.publication_cleanup_debt._reference == decoded.scratch_reference


def test_json_preparation_omits_a_child_identity_and_child_state_retains_one() -> None:
    prepared = _obligation(FileCallFamily.JSON_UPDATE)
    child = replace(
        prepared,
        token=_TOKEN,
        attempt=4,
        scratch_reference=_reference(FileCallFamily.JSON_UPDATE),
    )

    prepared_value = json.loads(encode_file_call_obligation(prepared))
    child_value = json.loads(encode_file_call_obligation(child))

    assert "attempt" not in prepared_value and "token" not in prepared_value
    assert child_value["attempt"] == 4 and child_value["token"] == _TOKEN.hex()
    assert decode_file_call_obligation(encode_file_call_obligation(prepared)) == prepared
    assert decode_file_call_obligation(encode_file_call_obligation(child)) == child


def test_envelope_bound_accepts_8192_bytes_within_each_path_bound_and_refuses_8193() -> None:
    baseline = _obligation(FileCallFamily.STAT)
    root = "/" + "a" * 4_095
    fixed_size = len(encode_file_call_obligation(replace(baseline, root=root, relative_path="a")))
    accepted = replace(
        baseline,
        root=root,
        relative_path="a" * (MAX_LIFECYCLE_PAYLOAD_BYTES - fixed_size + 1),
    )

    encoded = encode_file_call_obligation(accepted)

    assert len(accepted.root.encode()) <= 4_096
    assert len(accepted.relative_path.encode()) <= 4_096
    assert len(encoded) == MAX_LIFECYCLE_PAYLOAD_BYTES
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_obligation(replace(accepted, relative_path=accepted.relative_path + "a"))


def _maximum_recovery_obligation(
    family: FileCallFamily, *, initial: FileCallObligation | None = None
) -> FileCallObligation:
    initial = _obligation(family) if initial is None else initial
    uncertainty = {
        FileCallUncertainty.PENDING_REMOTE_EFFECT,
        FileCallUncertainty.COORDINATION_UNCERTAINTY,
    }
    if family not in {
        FileCallFamily.DOWNLOAD,
        FileCallFamily.UPLOAD,
        FileCallFamily.PACKAGE_UPLOAD,
        FileCallFamily.JSON_UPDATE,
    }:
        return replace(initial, uncertainty=frozenset(uncertainty))

    reference = _maximum_reference(family, initial.identity_plan)
    uncertainty.add(FileCallUncertainty.SCRATCH_OWNERSHIP)
    changes: dict[str, object] = {
        "token": _TOKEN,
        "scratch_reference": reference,
        "scratch_cleanup_debt": _maximum_cleanup_debt(initial.identity_plan),
        "uncertainty": frozenset(uncertainty),
    }
    if family is FileCallFamily.JSON_UPDATE:
        changes["attempt"] = 8
    if family in {FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD, FileCallFamily.JSON_UPDATE}:
        uncertainty.add(FileCallUncertainty.PUBLICATION_OWNERSHIP)
        changes["publication_cleanup_debt"] = _maximum_publication_debt(reference)
        changes["uncertainty"] = frozenset(uncertainty)
    return replace(initial, **changes)  # type: ignore[arg-type]


def test_admission_reserves_the_actual_largest_recovery_payload_for_every_family() -> None:
    for family, growth in _FILE_CALL_RECOVERY_HEADROOM_BYTES.items():
        initial = _obligation(family)
        recovery = _maximum_recovery_obligation(family)

        assert len(encode_file_call_obligation(recovery)) - len(encode_file_call_obligation(initial)) == growth
        assert encode_file_call_admission(initial) == encode_file_call_obligation(initial)


def _obligation_with_encoded_length(family: FileCallFamily, length: int) -> FileCallObligation:
    baseline = replace(_obligation(family), root="/", relative_path="a")
    remaining = length - len(encode_file_call_obligation(baseline))
    relative_padding = min(remaining, 4_095)
    root_padding = remaining - relative_padding
    assert 0 <= root_padding <= 4_095
    return replace(baseline, root="/" + "a" * root_padding, relative_path="a" * (relative_padding + 1))


@pytest.mark.parametrize("family", list(FileCallFamily))
def test_admission_ceiling_leaves_room_for_the_largest_retained_payload(family: FileCallFamily) -> None:
    growth = _FILE_CALL_RECOVERY_HEADROOM_BYTES[family]
    admitted = _obligation_with_encoded_length(family, MAX_LIFECYCLE_PAYLOAD_BYTES - growth)
    recovery = replace(
        _maximum_recovery_obligation(family),
        root=admitted.root,
        relative_path=admitted.relative_path,
    )
    one_byte_larger = _obligation_with_encoded_length(family, MAX_LIFECYCLE_PAYLOAD_BYTES - growth + 1)
    oversized_recovery = replace(
        _maximum_recovery_obligation(family),
        root=one_byte_larger.root,
        relative_path=one_byte_larger.relative_path,
    )

    assert len(encode_file_call_admission(admitted)) == MAX_LIFECYCLE_PAYLOAD_BYTES - growth
    assert len(encode_file_call_obligation(recovery)) == MAX_LIFECYCLE_PAYLOAD_BYTES
    assert len(encode_file_call_obligation(one_byte_larger)) == MAX_LIFECYCLE_PAYLOAD_BYTES - growth + 1
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_admission(one_byte_larger)
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_obligation(oversized_recovery)
    if family is FileCallFamily.JSON_UPDATE:
        child = replace(admitted, token=_TOKEN, attempt=8)
        assert len(encode_file_call_obligation(child)) == len(encode_file_call_obligation(admitted)) + 55


def test_gated_package_current_child_reserves_recovery_at_envelope_limit() -> None:
    guest = VMGuestIdentity("f" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)
    target = replace(_TARGET, boot_id=vm_guest_boot_id(guest))
    gate = FileEffectGateBinding(
        file_effect_gate_path(target, _PLAN.expected.euid, guest),
        b"i" * 16,
        b"g" * 16,
        guest,
        _PLAN.expected.euid,
        target.name,
        1,
        2,
    )
    baseline = replace(
        _obligation(FileCallFamily.PACKAGE_UPLOAD),
        target=target,
        effect_gate=gate,
        root="/",
        relative_path="a",
    )
    proposed = replace(gate, proposed_generation=b"p" * 16)
    growth = (
        _FILE_CALL_RECOVERY_HEADROOM_BYTES[FileCallFamily.PACKAGE_UPLOAD]
        + len(encode_file_call_obligation(replace(baseline, effect_gate=proposed)))
        - len(encode_file_call_obligation(baseline))
    )
    remaining = MAX_LIFECYCLE_PAYLOAD_BYTES - growth - len(encode_file_call_obligation(baseline))
    relative_padding = min(remaining, 4_095)
    root_padding = remaining - relative_padding
    assert 0 <= root_padding <= 4_095
    admitted = replace(
        baseline,
        root="/" + "a" * root_padding,
        relative_path="a" * (relative_padding + 1),
    )
    recovered = _maximum_recovery_obligation(
        FileCallFamily.PACKAGE_UPLOAD,
        initial=replace(admitted, effect_gate=proposed),
    )
    assert decode_file_call_obligation(encode_file_call_admission(admitted)) == admitted
    assert len(encode_file_call_obligation(recovered)) == MAX_LIFECYCLE_PAYLOAD_BYTES
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_admission(replace(admitted, root=admitted.root + "a"))


def _setup_download() -> FileCallObligation:
    maximum_uid = (1 << 32) - 1
    plan = IdentityPlan(IdentityExpectation(maximum_uid, maximum_uid, (maximum_uid,)), IdentityMode.DIRECT)
    guest = VMGuestIdentity("f" * 32, "123e4567-e89b-12d3-a456-426614174000", _MAXIMUM)
    initial = replace(
        _obligation(FileCallFamily.DOWNLOAD),
        root="/caf\u00e9",
        relative_path="na\u00efve/file",
        identity_plan=plan,
    )
    return replace(initial, gate_setup=FileEffectGateSetup.for_target(initial.target, maximum_uid, guest))


def _maximum_bound_download(setup: FileCallObligation) -> FileCallObligation:
    descriptor = setup.gate_setup
    assert descriptor is not None
    binding = FileEffectGateBinding(
        descriptor.path,
        b"\0" * 16,
        b"\1" * 16,
        descriptor.guest,
        setup.identity_plan.expected.euid,
        setup.target.name,
        _MAXIMUM,
        _MAXIMUM,
        b"\2" * 16,
    )
    return _maximum_recovery_obligation(
        FileCallFamily.DOWNLOAD,
        initial=replace(setup, gate_setup=None, effect_gate=binding),
    )


def _setup_with_encoded_length(length: int) -> FileCallObligation:
    baseline = _setup_download()
    remaining = length - len(encode_file_call_obligation(baseline))
    relative_padding = min(remaining, 4_096 - len(baseline.relative_path.encode("utf-8")))
    root_padding = remaining - relative_padding
    assert 0 <= root_padding <= 4_096 - len(baseline.root.encode("utf-8"))
    return replace(
        baseline,
        root=baseline.root + "a" * root_padding,
        relative_path=baseline.relative_path + "a" * relative_padding,
    )


def test_gate_setup_round_trip_and_legacy_download_remains_ungated() -> None:
    legacy = _obligation(FileCallFamily.DOWNLOAD)
    old_payload = encode_file_call_obligation(legacy)
    expected_legacy = (
        b'{"family":"download","identity":{"egid":1002,"euid":1001,"groups":[1002,1003],'
        b'"mode":"direct"},"path":"settings/naive-cafe.json","root":"/srv/agentworks",'
        b'"runtime":{"explicit_path":"/usr/bin/python3","target_os":"linux"},'
        b'"target":{"boot_id":"123e4567-e89b-12d3-a456-426614174000",'
        b'"incarnation":"v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"kind":"vm","name":"fixture-vm"},"token":"000102030405060708090a0b0c0d0e0f",'
        b'"uncertainty":[],"version":1}'
    )
    setup = _setup_download()
    encoded = encode_file_call_obligation(setup)

    assert FILE_CALL_OBLIGATION_PAYLOAD_VERSION == 1
    assert old_payload == expected_legacy
    assert b'"gate_setup"' not in old_payload
    assert b'"effect_gate"' not in old_payload
    assert decode_file_call_obligation(old_payload) == legacy
    assert encode_file_call_obligation(decode_file_call_obligation(old_payload)) == old_payload
    assert decode_file_call_obligation(encoded) == setup
    assert b'"gate_setup"' in encoded
    assert b"\\u00e9" in encoded and b"\\u00ef" in encoded
    assert setup.gate_setup is not None
    assert (
        setup.gate_setup.path
        == FileEffectGateSetup.for_target(
            replace(setup.target, incarnation="v1:" + "b" * 64),
            setup.identity_plan.expected.euid,
            setup.gate_setup.guest,
        ).path
    )


def test_gate_setup_decoder_refuses_unrecognized_or_replaced_identity() -> None:
    setup = _setup_download()
    value = json.loads(encode_file_call_obligation(setup))
    setup_value = value["gate_setup"]
    for changed_setup in (
        {**setup_value, "future": True},
        {**setup_value, "guest": {**setup_value["guest"], "init_start_ticks": True}},
        {**setup_value, "path": setup_value["path"] + "-other"},
    ):
        changed = {**value, "gate_setup": changed_setup}
        payload = json.dumps(changed, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        with pytest.raises(FileCallObligationCodecError):
            decode_file_call_obligation(payload)


def test_gate_setup_is_closed_to_wrong_family_identity_path_and_bound_state() -> None:
    setup = _setup_download()
    descriptor = setup.gate_setup
    assert descriptor is not None
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, family=FileCallFamily.UPLOAD)
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, target=replace(setup.target, name="other-vm"))
    other_uid = setup.identity_plan.expected.euid - 1
    with pytest.raises(FileCallObligationCodecError):
        replace(
            setup,
            identity_plan=IdentityPlan(IdentityExpectation(other_uid, other_uid, (other_uid,)), IdentityMode.DIRECT),
        )
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, gate_setup=replace(descriptor, guest=replace(descriptor.guest, init_start_ticks=1)))
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, gate_setup=replace(descriptor, path=descriptor.path + "-other"))
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, effect_gate=_maximum_bound_download(setup).effect_gate)
    with pytest.raises(FileCallObligationCodecError):
        replace(setup, scratch_cleanup_debt=_maximum_cleanup_debt(setup.identity_plan))


def test_bound_gate_obligation_refuses_another_canonical_gate_name() -> None:
    bound = _maximum_bound_download(_setup_download())
    gate = bound.effect_gate
    assert gate is not None
    other_digest = "0" * 64 if not gate.path.endswith("0" * 64 + ".db") else "1" * 64
    wrong = replace(gate, path=gate.path[:-67] + other_digest + ".db")
    with pytest.raises(FileCallObligationCodecError):
        replace(bound, effect_gate=wrong)

    encoded = json.loads(encode_file_call_obligation(bound))
    encoded["effect_gate"]["path"] = wrong.path
    payload = json.dumps(encoded, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(payload)


def test_gate_setup_admission_reserves_maximum_bound_proposal_and_download_growth() -> None:
    baseline = _setup_download()
    maximum = _maximum_bound_download(baseline)
    growth = len(encode_file_call_obligation(maximum)) - len(encode_file_call_obligation(baseline))
    admitted = _setup_with_encoded_length(MAX_LIFECYCLE_PAYLOAD_BYTES - growth)
    refused = _setup_with_encoded_length(MAX_LIFECYCLE_PAYLOAD_BYTES - growth + 1)

    assert maximum.effect_gate is not None
    assert maximum.effect_gate.device == maximum.effect_gate.inode == _MAXIMUM
    assert maximum.effect_gate.euid == (1 << 32) - 1
    assert maximum.effect_gate.guest.init_start_ticks == _MAXIMUM
    assert len(encode_file_call_admission(admitted)) == MAX_LIFECYCLE_PAYLOAD_BYTES - growth
    assert len(encode_file_call_obligation(_maximum_bound_download(admitted))) == MAX_LIFECYCLE_PAYLOAD_BYTES
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_admission(refused)
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_obligation(_maximum_bound_download(refused))


def test_decoder_normalizes_python_integer_digit_refusal() -> None:
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        payload = b'{"version":' + b"9" * 641 + b"}"
        with pytest.raises(FileCallObligationCodecError):
            decode_file_call_obligation(payload)
    finally:
        sys.set_int_max_str_digits(previous_limit)


def test_upload_and_download_tokens_do_not_carry_json_attempt_identity() -> None:
    for family in (FileCallFamily.UPLOAD, FileCallFamily.DOWNLOAD):
        encoded = encode_file_call_obligation(_obligation(family))

        assert "attempt" not in json.loads(encoded)
        with pytest.raises(FileCallObligationCodecError):
            replace(_obligation(family), attempt=1)


def test_json_attempt_is_bounded_to_eight() -> None:
    with pytest.raises(FileCallObligationCodecError):
        replace(_obligation(FileCallFamily.JSON_UPDATE), token=_TOKEN, attempt=9)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: {**value, "unknown": True},
        lambda value: {key: item for key, item in value.items() if key != "target"},
        lambda value: {**value, "token": _TOKEN.hex()},
        lambda value: {**value, "version": FILE_CALL_OBLIGATION_PAYLOAD_VERSION + 1},
    ],
)
def test_unknown_malformed_irrelevant_and_future_payload_fields_refuse(change: object) -> None:
    value = json.loads(encode_file_call_obligation(_obligation(FileCallFamily.STAT)))
    changed = change(value)  # type: ignore[operator]
    payload = json.dumps(changed, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")

    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(payload)


def test_noncanonical_and_non_ascii_payloads_refuse() -> None:
    canonical = encode_file_call_obligation(_obligation(FileCallFamily.STAT))

    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(canonical.replace(b"{", b"{ ", 1))
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(canonical.replace(b"settings", "caf\u00e9".encode(), 1))


def test_token_and_context_mismatches_cannot_be_retained() -> None:
    different_token = bytes(reversed(_TOKEN))
    mismatched_plan = IdentityPlan(IdentityExpectation(1004, 1002, (1002, 1003)), IdentityMode.DIRECT)

    with pytest.raises(FileCallObligationCodecError):
        replace(
            _obligation(FileCallFamily.UPLOAD),
            scratch_reference=_reference(FileCallFamily.UPLOAD, token=different_token),
        )
    with pytest.raises(FileCallObligationCodecError):
        replace(
            _obligation(FileCallFamily.UPLOAD),
            scratch_reference=_reference(FileCallFamily.UPLOAD, plan=mismatched_plan),
        )


def test_publication_state_requires_an_upload_or_json_reference() -> None:
    reference = _reference(FileCallFamily.UPLOAD)
    debt = bind_publication_cleanup_debt(
        reference,
        PublicationIdentity(1, 2),
        PublicationCleanupDebt(publication_stage_name(_TOKEN), 1, 6),
    )

    with pytest.raises(FileCallObligationCodecError):
        replace(_obligation(FileCallFamily.UPLOAD), publication_cleanup_debt=debt)
    with pytest.raises(FileCallObligationCodecError):
        replace(_obligation(FileCallFamily.DOWNLOAD), scratch_reference=reference, publication_cleanup_debt=debt)


def test_application_values_and_sensitive_operational_fields_have_no_payload_slot() -> None:
    application_value = "operator-json-value"
    encoded = encode_file_call_obligation(_obligation(FileCallFamily.JSON_UPDATE))
    value = json.loads(encoded)
    injected = json.dumps(
        {**value, "contents": application_value, "credentials": "secret", "command": "do-not-run"},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    assert application_value.encode() not in encoded
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(injected)
