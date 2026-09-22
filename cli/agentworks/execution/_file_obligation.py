"""Canonical recovery payloads for private file-call lifecycle obligations.

This codec retains only the identity needed to recover a possible file effect.
It deliberately does not retain file contents, JSON documents, source or sink
objects, carrier details, diagnostics, commands, or replay instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError
from agentworks.execution._file_paths import normalized_relative_path, normalized_root
from agentworks.execution._file_publication_wire import (
    BoundPublicationCleanupDebt,
    FilePublicationWireError,
    decode_publication_cleanup_debt,
    encode_publication_cleanup_debt,
)
from agentworks.execution._helper_identity import IdentityExpectation, decode_identity
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import ScratchCleanupDebt, ScratchOperation, ScratchReceiptContext
from agentworks.execution._scratch_wire import (
    ScratchWireError,
    decode_cleanup_debt,
    decode_scratch_reference,
    encode_cleanup_debt,
    encode_scratch_reference,
)

FILE_CALL_OBLIGATION_PAYLOAD_VERSION = 1
_TOKEN_BYTES = 16
_MAX_JSON_ATTEMPTS = 8
_MAX_PATH_BYTES = 4_096
_LOWER_HEX = frozenset("0123456789abcdef")
_BASE_FIELDS = frozenset({"family", "identity", "path", "root", "runtime", "target", "uncertainty", "version"})
_OPTIONAL_FIELDS = frozenset({"attempt", "publication_cleanup_debt", "scratch", "token"})
_TARGET_FIELDS = frozenset({"boot_id", "incarnation", "kind", "name"})
_IDENTITY_FIELDS = frozenset({"egid", "euid", "groups", "mode"})
_RUNTIME_FIELDS = frozenset({"explicit_path", "target_os"})
_SCRATCH_FIELDS = frozenset({"cleanup_debt", "reference"})


class FileCallFamily(StrEnum):
    """Closed private file-call families that can own one lifecycle effect."""

    DOWNLOAD = "download"
    UPLOAD = "upload"
    JSON_UPDATE = "json-update"
    STAT = "stat"
    INVENTORY = "inventory"
    REMOVE = "remove"
    SET_METADATA = "set-metadata"
    ENSURE_DIRECTORY = "ensure-directory"


class FileCallUncertainty(StrEnum):
    """Recovery facts that remain unproved after a file-call attempt."""

    PENDING_REMOTE_EFFECT = "pending-remote-effect"
    COORDINATION_UNCERTAINTY = "coordination-uncertainty"
    SCRATCH_OWNERSHIP = "scratch-ownership"
    PUBLICATION_OWNERSHIP = "publication-ownership"


# The maximum expansions from an initial payload to its retained recovery
# identity: 814 download, 1150 upload, 1205 JSON, 50 for each single call.
# Typed maximum-object and exact-boundary tests prove these values.
_FILE_CALL_RECOVERY_HEADROOM_BYTES = {
    FileCallFamily.DOWNLOAD: 814,
    FileCallFamily.UPLOAD: 1150,
    FileCallFamily.JSON_UPDATE: 1205,
    FileCallFamily.STAT: 50,
    FileCallFamily.INVENTORY: 50,
    FileCallFamily.REMOVE: 50,
    FileCallFamily.SET_METADATA: 50,
    FileCallFamily.ENSURE_DIRECTORY: 50,
}


class FileCallObligationCodecError(ValueError):
    """A persisted file-call lifecycle payload is not a supported canonical record."""

    def __init__(self) -> None:
        super().__init__("invalid file-call lifecycle payload")


@dataclass(frozen=True, slots=True, repr=False)
class FileCallObligation:
    """Typed recovery identity for one private file-call lifecycle obligation."""

    family: FileCallFamily
    target: ManagedTargetIdentity
    root: str
    relative_path: str
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection
    token: bytes | None = None
    attempt: int | None = None
    scratch_reference: ScratchReference | None = None
    scratch_cleanup_debt: ScratchCleanupDebt | None = None
    publication_cleanup_debt: BoundPublicationCleanupDebt | None = None
    uncertainty: frozenset[FileCallUncertainty] = frozenset()

    def __post_init__(self) -> None:
        _validate_obligation(self)


def encode_file_call_obligation(obligation: FileCallObligation) -> bytes:
    """Encode one valid file-call obligation as bounded canonical ASCII JSON."""
    if type(obligation) is not FileCallObligation:
        raise FileCallObligationCodecError
    _validate_obligation(obligation)
    value = _encode_obligation(obligation)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        raise FileCallObligationCodecError from None
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise FileCallObligationCodecError
    return encoded


def encode_file_call_admission(obligation: FileCallObligation) -> bytes:
    """Encode an initial payload while reserving its possible recovery facts."""
    encoded = encode_file_call_obligation(obligation)
    reserve = _FILE_CALL_RECOVERY_HEADROOM_BYTES[obligation.family]
    if len(encoded) + reserve > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise FileCallObligationCodecError
    return encoded


def decode_file_call_obligation(payload: bytes) -> FileCallObligation:
    """Decode one bounded canonical payload into validated recovery types."""
    if type(payload) is not bytes or len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise FileCallObligationCodecError
    try:
        text = payload.decode("ascii")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise FileCallObligationCodecError from None
    obligation = _decode_obligation(value)
    if encode_file_call_obligation(obligation) != payload:
        raise FileCallObligationCodecError
    return obligation


def _validate_obligation(obligation: FileCallObligation) -> None:
    if type(obligation.family) is not FileCallFamily or type(obligation.target) is not ManagedTargetIdentity:
        raise FileCallObligationCodecError
    _validate_path(obligation.root, root=True)
    _validate_path(obligation.relative_path, root=False)
    _validate_identity_plan(obligation.identity_plan)
    _validate_runtime_selection(obligation.runtime_selection)
    if type(obligation.uncertainty) is not frozenset or any(
        type(item) is not FileCallUncertainty for item in obligation.uncertainty
    ):
        raise FileCallObligationCodecError

    scratch_family = obligation.family in {
        FileCallFamily.DOWNLOAD,
        FileCallFamily.UPLOAD,
        FileCallFamily.JSON_UPDATE,
    }
    publication_family = obligation.family in {FileCallFamily.UPLOAD, FileCallFamily.JSON_UPDATE}
    if obligation.token is None:
        if obligation.attempt is not None:
            raise FileCallObligationCodecError
        if obligation.family in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD}:
            raise FileCallObligationCodecError
        if (
            any(
                item is not None
                for item in (
                    obligation.scratch_reference,
                    obligation.scratch_cleanup_debt,
                    obligation.publication_cleanup_debt,
                )
            )
            or FileCallUncertainty.SCRATCH_OWNERSHIP in obligation.uncertainty
            or (FileCallUncertainty.PUBLICATION_OWNERSHIP in obligation.uncertainty)
        ):
            raise FileCallObligationCodecError
        return

    if not scratch_family or not _valid_token(obligation.token):
        raise FileCallObligationCodecError
    if obligation.family is FileCallFamily.JSON_UPDATE:
        if not _valid_attempt(obligation.attempt):
            raise FileCallObligationCodecError
    elif obligation.attempt is not None:
        raise FileCallObligationCodecError
    token = bytes(obligation.token)
    context = _scratch_context(obligation.family, obligation.identity_plan)
    if obligation.scratch_reference is not None:
        _validate_scratch_reference(obligation.scratch_reference, token, context)
    if obligation.scratch_cleanup_debt is not None:
        _validate_scratch_cleanup_debt(obligation.scratch_cleanup_debt, token, context)
    if obligation.publication_cleanup_debt is not None:
        if not publication_family or obligation.scratch_reference is None:
            raise FileCallObligationCodecError
        _validate_publication_cleanup_debt(obligation.publication_cleanup_debt, obligation.scratch_reference)
    if FileCallUncertainty.PUBLICATION_OWNERSHIP in obligation.uncertainty and (
        not publication_family or obligation.scratch_reference is None
    ):
        raise FileCallObligationCodecError


def _encode_obligation(obligation: FileCallObligation) -> dict[str, object]:
    value: dict[str, object] = {
        "family": obligation.family.value,
        "identity": _encode_identity_plan(obligation.identity_plan),
        "path": obligation.relative_path,
        "root": obligation.root,
        "runtime": _encode_runtime_selection(obligation.runtime_selection),
        "target": _encode_target(obligation.target),
        "uncertainty": sorted(item.value for item in obligation.uncertainty),
        "version": FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    }
    if obligation.token is not None:
        value["token"] = obligation.token.hex()
    if obligation.attempt is not None:
        value["attempt"] = obligation.attempt
    scratch: dict[str, object] = {}
    if obligation.scratch_reference is not None:
        scratch["reference"] = encode_scratch_reference(obligation.scratch_reference)
    if obligation.scratch_cleanup_debt is not None:
        scratch["cleanup_debt"] = encode_cleanup_debt(obligation.scratch_cleanup_debt)
    if scratch:
        value["scratch"] = scratch
    if obligation.publication_cleanup_debt is not None:
        value["publication_cleanup_debt"] = encode_publication_cleanup_debt(obligation.publication_cleanup_debt)
    return value


def _decode_obligation(value: object) -> FileCallObligation:
    if type(value) is not dict or not _BASE_FIELDS <= set(value) <= _BASE_FIELDS | _OPTIONAL_FIELDS:
        raise FileCallObligationCodecError
    if value["version"] != FILE_CALL_OBLIGATION_PAYLOAD_VERSION or type(value["version"]) is not int:
        raise FileCallObligationCodecError
    try:
        family = FileCallFamily(value["family"])
    except (TypeError, ValueError):
        raise FileCallObligationCodecError from None
    target = _decode_target(value["target"])
    root = _decode_path(value["root"], root=True)
    relative_path = _decode_path(value["path"], root=False)
    identity_plan = _decode_identity_plan(value["identity"])
    runtime_selection = _decode_runtime_selection(value["runtime"])
    uncertainty = _decode_uncertainty(value["uncertainty"])
    token, attempt = _decode_attempt(value)
    context = _scratch_context(family, identity_plan) if token is not None else None
    scratch_reference, scratch_cleanup_debt = _decode_scratch(value.get("scratch"), token, context)
    publication_cleanup_debt = _decode_publication_debt(value.get("publication_cleanup_debt"), scratch_reference)
    try:
        return FileCallObligation(
            family=family,
            target=target,
            root=root,
            relative_path=relative_path,
            identity_plan=identity_plan,
            runtime_selection=runtime_selection,
            token=token,
            attempt=attempt,
            scratch_reference=scratch_reference,
            scratch_cleanup_debt=scratch_cleanup_debt,
            publication_cleanup_debt=publication_cleanup_debt,
            uncertainty=uncertainty,
        )
    except FileCallObligationCodecError:
        raise
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _encode_target(target: ManagedTargetIdentity) -> dict[str, str]:
    return {
        "boot_id": target.boot_id,
        "incarnation": target.incarnation,
        "kind": target.kind.value,
        "name": target.name,
    }


def _decode_target(value: object) -> ManagedTargetIdentity:
    if type(value) is not dict or set(value) != _TARGET_FIELDS:
        raise FileCallObligationCodecError
    try:
        return ManagedTargetIdentity(
            ManagedTargetKind(value["kind"]),
            cast("str", value["name"]),
            cast("str", value["incarnation"]),
            cast("str", value["boot_id"]),
        )
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _encode_identity_plan(plan: IdentityPlan) -> dict[str, object]:
    expected = plan.expected
    return {
        "egid": expected.egid,
        "euid": expected.euid,
        "groups": list(expected.groups),
        "mode": plan.mode.value,
    }


def _decode_identity_plan(value: object) -> IdentityPlan:
    if type(value) is not dict or set(value) != _IDENTITY_FIELDS:
        raise FileCallObligationCodecError
    try:
        expected = decode_identity({key: value[key] for key in ("egid", "euid", "groups")})
        plan = IdentityPlan(expected, IdentityMode(value["mode"]))
        _validate_identity_plan(plan)
        return plan
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _validate_identity_plan(plan: object) -> IdentityExpectation:
    if type(plan) is not IdentityPlan or type(plan.expected) is not IdentityExpectation:
        raise FileCallObligationCodecError
    try:
        expected = decode_identity(
            {"egid": plan.expected.egid, "euid": plan.expected.euid, "groups": list(plan.expected.groups)}
        )
    except (TypeError, ValueError):
        raise FileCallObligationCodecError from None
    if (
        plan.mode is not IdentityMode.DIRECT
        and plan.mode is not IdentityMode.SUDO_ROOT
        and plan.mode is not IdentityMode.DEMOTE
    ):
        raise FileCallObligationCodecError
    if (plan.mode is IdentityMode.SUDO_ROOT and expected.euid != 0) or (
        plan.mode is IdentityMode.DEMOTE and expected.euid == 0
    ):
        raise FileCallObligationCodecError
    return expected


def _encode_runtime_selection(selection: RuntimeSelection) -> dict[str, str | None]:
    return {"explicit_path": selection.explicit_path, "target_os": selection.target_os.value}


def _decode_runtime_selection(value: object) -> RuntimeSelection:
    if type(value) is not dict or set(value) != _RUNTIME_FIELDS:
        raise FileCallObligationCodecError
    try:
        selection = RuntimeSelection(RuntimeTargetOS(value["target_os"]), cast("str | None", value["explicit_path"]))
        _validate_runtime_selection(selection)
        return selection
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _validate_runtime_selection(selection: object) -> None:
    if type(selection) is not RuntimeSelection:
        raise FileCallObligationCodecError
    try:
        RuntimeSelection(selection.target_os, selection.explicit_path)
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _decode_path(value: object, *, root: bool) -> str:
    if type(value) is not str:
        raise FileCallObligationCodecError
    _validate_path(value, root=root)
    return value


def _validate_path(path: object, *, root: bool) -> None:
    if type(path) is not str:
        raise FileCallObligationCodecError
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise FileCallObligationCodecError from None
    if len(encoded) > _MAX_PATH_BYTES or not (normalized_root(path) if root else normalized_relative_path(path)):
        raise FileCallObligationCodecError


def _decode_uncertainty(value: object) -> frozenset[FileCallUncertainty]:
    if type(value) is not list:
        raise FileCallObligationCodecError
    try:
        uncertainty = frozenset(FileCallUncertainty(item) for item in value)
    except (TypeError, ValueError):
        raise FileCallObligationCodecError from None
    if len(uncertainty) != len(value):
        raise FileCallObligationCodecError
    return uncertainty


def _decode_attempt(value: dict[str, object]) -> tuple[bytes | None, int | None]:
    token_present = "token" in value
    attempt_present = "attempt" in value
    if attempt_present and not token_present:
        raise FileCallObligationCodecError
    if not token_present:
        return None, None
    token_value = value["token"]
    if (
        type(token_value) is not str
        or len(token_value) != _TOKEN_BYTES * 2
        or any(character not in _LOWER_HEX for character in token_value)
    ):
        raise FileCallObligationCodecError
    token = bytes.fromhex(token_value)
    if not attempt_present:
        return token, None
    attempt = value["attempt"]
    if not _valid_attempt(attempt):
        raise FileCallObligationCodecError
    assert type(attempt) is int
    return token, attempt


def _decode_scratch(
    value: object,
    token: bytes | None,
    context: ScratchReceiptContext | None,
) -> tuple[ScratchReference | None, ScratchCleanupDebt | None]:
    if value is None:
        return None, None
    if token is None or context is None or type(value) is not dict or not set(value) <= _SCRATCH_FIELDS or not value:
        raise FileCallObligationCodecError
    reference = None
    cleanup_debt = None
    try:
        if "reference" in value:
            reference = decode_scratch_reference(value["reference"], token, context)
        if "cleanup_debt" in value:
            cleanup_debt = decode_cleanup_debt(value["cleanup_debt"], token, context)
    except (ScratchWireError, TypeError, ValueError):
        raise FileCallObligationCodecError from None
    return reference, cleanup_debt


def _decode_publication_debt(
    value: object,
    reference: ScratchReference | None,
) -> BoundPublicationCleanupDebt | None:
    if value is None:
        return None
    if reference is None:
        raise FileCallObligationCodecError
    try:
        return decode_publication_cleanup_debt(value, reference)
    except (FilePublicationWireError, TypeError, ValueError):
        raise FileCallObligationCodecError from None


def _scratch_context(family: FileCallFamily, plan: IdentityPlan) -> ScratchReceiptContext:
    operation = ScratchOperation.SNAPSHOT if family is FileCallFamily.DOWNLOAD else ScratchOperation.STAGE
    return ScratchReceiptContext(operation, plan.expected)


def _validate_scratch_reference(reference: object, token: bytes, context: ScratchReceiptContext) -> None:
    if (
        type(reference) is not ScratchReference
        or reference._ownership._token != token
        or reference._ownership._context != context
    ):
        raise FileCallObligationCodecError
    try:
        if decode_scratch_reference(encode_scratch_reference(reference), token, context) != reference:
            raise FileCallObligationCodecError
    except (ScratchWireError, TypeError, ValueError):
        raise FileCallObligationCodecError from None


def _validate_scratch_cleanup_debt(debt: object, token: bytes, context: ScratchReceiptContext) -> None:
    if type(debt) is not ScratchCleanupDebt:
        raise FileCallObligationCodecError
    try:
        if decode_cleanup_debt(encode_cleanup_debt(debt), token, context) != debt:
            raise FileCallObligationCodecError
    except (ScratchWireError, TypeError, ValueError):
        raise FileCallObligationCodecError from None


def _validate_publication_cleanup_debt(debt: object, reference: ScratchReference) -> None:
    if type(debt) is not BoundPublicationCleanupDebt or debt._reference != reference:
        raise FileCallObligationCodecError
    try:
        if decode_publication_cleanup_debt(encode_publication_cleanup_debt(debt), reference) != debt:
            raise FileCallObligationCodecError
    except (FilePublicationWireError, TypeError, ValueError):
        raise FileCallObligationCodecError from None


def _valid_token(token: object) -> bool:
    return type(token) is bytes and len(token) == _TOKEN_BYTES


def _valid_attempt(attempt: object) -> bool:
    return type(attempt) is int and 1 <= attempt <= _MAX_JSON_ATTEMPTS
