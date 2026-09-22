"""Recovery-payload coverage for private file-call lifecycle obligations."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    FileCallObligationCodecError,
    FileCallUncertainty,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_publication import PublicationCleanupDebt
from agentworks.execution._file_publication_wire import bind_publication_cleanup_debt
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._publication_receipt import _Identity as PublicationIdentity
from agentworks.execution._publication_receipt import publication_stage_name
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import (
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

_TOKEN = bytes(range(16))
_TARGET = ManagedTargetIdentity(
    ManagedTargetKind.VM,
    "fixture-vm",
    "v1:" + "a" * 64,
    "123e4567-e89b-12d3-a456-426614174000",
)
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")


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


def _obligation(family: FileCallFamily, *, token: bytes | None = None) -> FileCallObligation:
    has_scratch = family in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.JSON_UPDATE}
    selected_token = _TOKEN if has_scratch and family is not FileCallFamily.JSON_UPDATE else token
    return FileCallObligation(
        family=family,
        target=_TARGET,
        root="/srv/agentworks",
        relative_path="settings/naive-cafe.json",
        identity_plan=_PLAN,
        runtime_selection=_RUNTIME,
        token=selected_token,
        attempt=1 if selected_token is not None else None,
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
        uncertainty=frozenset({FileCallUncertainty.DISPATCH, FileCallUncertainty.PUBLICATION_OWNERSHIP}),
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


def test_envelope_bound_accepts_a_maximal_path_and_refuses_an_over_bound_payload() -> None:
    baseline = _obligation(FileCallFamily.STAT)
    fixed_size = len(encode_file_call_obligation(replace(baseline, relative_path="a")))
    accepted = replace(baseline, relative_path="a" * (MAX_LIFECYCLE_PAYLOAD_BYTES - fixed_size + 1))

    encoded = encode_file_call_obligation(accepted)

    assert len(encoded) == MAX_LIFECYCLE_PAYLOAD_BYTES
    with pytest.raises(FileCallObligationCodecError):
        encode_file_call_obligation(replace(accepted, relative_path=accepted.relative_path + "a"))


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
