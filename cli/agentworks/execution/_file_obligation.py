"""Canonical recovery payloads for private file-call lifecycle obligations.

This codec retains only the identity needed to recover a possible file effect.
It deliberately does not retain file contents, JSON documents, source or sink
objects, carrier details, diagnostics, commands, or replay instructions.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError
from agentworks.execution._file_effect_gate import (
    FileEffectGateBinding,
    FileEffectGateError,
    decode_file_effect_gate,
    encode_file_effect_gate,
)
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
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
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import ScratchCleanupDebt, ScratchOperation, ScratchReceiptContext
from agentworks.execution._scratch_wire import (
    ScratchWireError,
    decode_cleanup_debt,
    decode_scratch_reference,
    encode_cleanup_debt,
    encode_scratch_reference,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id

FILE_CALL_OBLIGATION_PAYLOAD_VERSION = 1
NUMERIC_FILE_CALL_OBLIGATION_PAYLOAD_VERSION = 2
UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION = 3
NUMERIC_UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION = 4
MAX_PACKAGE_UPLOAD_MEMBERS = 4096
_TOKEN_BYTES = 16
_MAX_JSON_ATTEMPTS = 8
_MAX_PATH_BYTES = 4_096
_LOWER_HEX = frozenset("0123456789abcdef")
_BASE_FIELDS = frozenset({"family", "identity", "path", "root", "runtime", "target", "uncertainty", "version"})
_OPTIONAL_FIELDS = frozenset(
    {"attempt", "batch_index", "effect_gate", "gate_setup", "publication_cleanup_debt", "scratch", "token"}
)
_TARGET_FIELDS = frozenset({"boot_id", "incarnation", "kind", "name"})
_IDENTITY_FIELDS = frozenset({"egid", "euid", "groups", "mode"})
_RUNTIME_FIELDS = frozenset({"explicit_path", "target_os"})
_SCRATCH_FIELDS = frozenset({"cleanup_debt", "reference"})
_GATE_SETUP_FIELDS = frozenset({"guest", "path"})
_GUEST_FIELDS = frozenset({"boot_id", "init_start_ticks", "instance_marker"})
_BOOTSTRAP_FIELDS = frozenset({"guest", "root_entry"})
_UPLOAD_CHILD_FIELDS = frozenset({"transfer_id", "member_ordinal"})


class FileCallFamily(StrEnum):
    """Closed private file-call families that can own one lifecycle effect."""

    DOWNLOAD = "download"
    UPLOAD = "upload"
    PACKAGE_UPLOAD = "package-upload"
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
# identity without a gate proposal: 814 download, 1150 upload or package
# upload, 1205 JSON, and 50 for each other single call. Typed maximum-object
# and exact-boundary tests prove these values. A bound gate reserves its
# proposed generation separately.
# Immutable numeric bootstrap identity appears in both payloads, so its
# presence does not change these growth reserves.
_FILE_CALL_RECOVERY_HEADROOM_BYTES = {
    FileCallFamily.DOWNLOAD: 814,
    FileCallFamily.UPLOAD: 1150,
    FileCallFamily.PACKAGE_UPLOAD: 1150,
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
class UploadChildAssociation:
    """Bounded transfer identity, without a plan or application ownership."""

    transfer_id: bytes
    member_ordinal: int

    def __post_init__(self) -> None:
        """Validate caller and persisted association at the codec boundary."""
        _validate_upload_child(self)

    @classmethod
    def fresh(cls) -> UploadChildAssociation:
        """Begin a new bounded transfer at its first member."""
        return cls(secrets.token_bytes(_TOKEN_BYTES), 0)


def _validate_upload_child(child: UploadChildAssociation) -> None:
    """Check bounded child identity at the durable codec boundary."""
    if (
        type(child) is not UploadChildAssociation
        or type(child.transfer_id) is not bytes
        or len(child.transfer_id) != _TOKEN_BYTES
        or type(child.member_ordinal) is not int
        or not 0 <= child.member_ordinal < MAX_PACKAGE_UPLOAD_MEMBERS
    ):
        raise FileCallObligationCodecError


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
    batch_index: int | None = None
    scratch_reference: ScratchReference | None = None
    scratch_cleanup_debt: ScratchCleanupDebt | None = None
    publication_cleanup_debt: BoundPublicationCleanupDebt | None = None
    effect_gate: FileEffectGateBinding | None = None
    gate_setup: FileEffectGateSetup | None = None
    uncertainty: frozenset[FileCallUncertainty] = frozenset()
    bootstrap: _NumericGuestBootstrap | None = None
    upload_child: UploadChildAssociation | None = None
    upload_child_complete: bool = False

    def __post_init__(self) -> None:
        _validate_obligation(self)

    @property
    def payload_version(self) -> int:
        """Select the durable format without reinterpreting legacy records."""
        if self.upload_child is not None:
            return (
                NUMERIC_UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION
                if self.bootstrap is not None
                else UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION
            )
        if self.bootstrap is not None:
            return NUMERIC_FILE_CALL_OBLIGATION_PAYLOAD_VERSION
        return FILE_CALL_OBLIGATION_PAYLOAD_VERSION


def encode_file_call_obligation(obligation: FileCallObligation) -> bytes:
    """Encode one valid file-call obligation as bounded canonical ASCII JSON."""
    if type(obligation) is not FileCallObligation:
        raise FileCallObligationCodecError
    _validate_obligation(obligation)
    encoded = _encode_unbounded(obligation)
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise FileCallObligationCodecError
    return encoded


def _encode_unbounded(obligation: FileCallObligation) -> bytes:
    """Render a validated row before applying the database envelope limit."""
    value = _encode_obligation(obligation)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        raise FileCallObligationCodecError from None
    return encoded


def encode_file_call_admission(obligation: FileCallObligation) -> bytes:
    """Encode an initial payload while reserving its possible recovery facts."""
    encoded = encode_file_call_obligation(obligation)
    reserve = _FILE_CALL_RECOVERY_HEADROOM_BYTES[obligation.family]
    if obligation.gate_setup is not None:
        setup = obligation.gate_setup
        bound = FileEffectGateBinding(
            setup.path,
            b"\0" * _TOKEN_BYTES,
            b"\1" * _TOKEN_BYTES,
            setup.guest,
            obligation.identity_plan.expected.euid,
            obligation.target.name,
            (1 << 64) - 1,
            (1 << 64) - 1,
            b"\2" * _TOKEN_BYTES,
        )
        full = replace(obligation, gate_setup=None, effect_gate=bound)
        reserve += len(_encode_unbounded(full)) - len(encoded)
    elif obligation.effect_gate is not None:
        # A takeover first persists its proposed token in this same row.
        reserve += len(',"proposed_generation":"' + "0" * 32 + '"')
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
    if obligation.bootstrap is not None:
        _validate_bootstrap(obligation)
    if type(obligation.uncertainty) is not frozenset or any(
        type(item) is not FileCallUncertainty for item in obligation.uncertainty
    ):
        raise FileCallObligationCodecError
    if type(obligation.upload_child_complete) is not bool:
        raise FileCallObligationCodecError
    if obligation.upload_child is not None:
        if (
            type(obligation.upload_child) is not UploadChildAssociation
            or obligation.family is not FileCallFamily.UPLOAD
        ):
            raise FileCallObligationCodecError
        _validate_upload_child(obligation.upload_child)
    elif obligation.upload_child_complete:
        raise FileCallObligationCodecError
    if obligation.upload_child_complete and (
        obligation.uncertainty
        or obligation.scratch_cleanup_debt is not None
        or obligation.publication_cleanup_debt is not None
        or obligation.gate_setup is not None
        or obligation.scratch_reference is None
    ):
        raise FileCallObligationCodecError
    if obligation.family is FileCallFamily.PACKAGE_UPLOAD:
        if type(obligation.batch_index) is not int or not 0 <= obligation.batch_index < MAX_PACKAGE_UPLOAD_MEMBERS:
            raise FileCallObligationCodecError
    elif obligation.batch_index is not None:
        raise FileCallObligationCodecError
    if obligation.effect_gate is not None:
        if (
            obligation.family not in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD}
            or obligation.target.kind is not ManagedTargetKind.VM
            or obligation.runtime_selection.target_os is not RuntimeTargetOS.LINUX
            or type(obligation.effect_gate) is not FileEffectGateBinding
        ):
            raise FileCallObligationCodecError
        if obligation.effect_gate.euid != obligation.identity_plan.expected.euid:
            raise FileCallObligationCodecError
        if obligation.effect_gate.scope_name != obligation.target.name:
            raise FileCallObligationCodecError
        if vm_guest_boot_id(obligation.effect_gate.guest) != obligation.target.boot_id:
            raise FileCallObligationCodecError
        if obligation.effect_gate.path != file_effect_gate_path(
            obligation.target, obligation.identity_plan.expected.euid, obligation.effect_gate.guest
        ):
            raise FileCallObligationCodecError
        try:
            decode_file_effect_gate(encode_file_effect_gate(obligation.effect_gate))
        except FileEffectGateError:
            raise FileCallObligationCodecError from None
    if obligation.gate_setup is not None:
        setup = obligation.gate_setup
        if (
            obligation.family not in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD}
            or (obligation.family is FileCallFamily.PACKAGE_UPLOAD and obligation.batch_index != 0)
            or obligation.target.kind is not ManagedTargetKind.VM
            or obligation.runtime_selection.target_os is not RuntimeTargetOS.LINUX
            or obligation.effect_gate is not None
            or type(setup) is not FileEffectGateSetup
            or type(setup.guest) is not VMGuestIdentity
            or type(setup.path) is not str
            or setup.path
            != file_effect_gate_path(obligation.target, obligation.identity_plan.expected.euid, setup.guest)
            or vm_guest_boot_id(setup.guest) != obligation.target.boot_id
            or obligation.scratch_reference is not None
            or obligation.scratch_cleanup_debt is not None
            or obligation.publication_cleanup_debt is not None
            or FileCallUncertainty.SCRATCH_OWNERSHIP in obligation.uncertainty
            or FileCallUncertainty.PUBLICATION_OWNERSHIP in obligation.uncertainty
        ):
            raise FileCallObligationCodecError

    scratch_family = obligation.family in {
        FileCallFamily.DOWNLOAD,
        FileCallFamily.UPLOAD,
        FileCallFamily.PACKAGE_UPLOAD,
        FileCallFamily.JSON_UPDATE,
    }
    publication_family = obligation.family in {
        FileCallFamily.UPLOAD,
        FileCallFamily.PACKAGE_UPLOAD,
        FileCallFamily.JSON_UPDATE,
    }
    if obligation.token is None:
        if obligation.attempt is not None:
            raise FileCallObligationCodecError
        if obligation.family in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD}:
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
        "version": obligation.payload_version,
    }
    if obligation.bootstrap is not None:
        value["bootstrap"] = {
            "root_entry": _encode_identity_plan(obligation.bootstrap.root_entry),
            "guest": _encode_guest(obligation.bootstrap.guest),
        }
    if obligation.upload_child is not None:
        value["upload_child"] = {
            "transfer_id": obligation.upload_child.transfer_id.hex(),
            "member_ordinal": obligation.upload_child.member_ordinal,
        }
        value["upload_child_complete"] = obligation.upload_child_complete
    if obligation.token is not None:
        value["token"] = obligation.token.hex()
    if obligation.attempt is not None:
        value["attempt"] = obligation.attempt
    if obligation.batch_index is not None:
        value["batch_index"] = obligation.batch_index
    scratch: dict[str, object] = {}
    if obligation.scratch_reference is not None:
        scratch["reference"] = encode_scratch_reference(obligation.scratch_reference)
    if obligation.scratch_cleanup_debt is not None:
        scratch["cleanup_debt"] = encode_cleanup_debt(obligation.scratch_cleanup_debt)
    if scratch:
        value["scratch"] = scratch
    if obligation.publication_cleanup_debt is not None:
        value["publication_cleanup_debt"] = encode_publication_cleanup_debt(obligation.publication_cleanup_debt)
    if obligation.effect_gate is not None:
        value["effect_gate"] = encode_file_effect_gate(obligation.effect_gate)
    if obligation.gate_setup is not None:
        value["gate_setup"] = {
            "guest": _encode_guest(obligation.gate_setup.guest),
            "path": obligation.gate_setup.path,
        }
    return value


def _decode_obligation(value: object) -> FileCallObligation:
    if type(value) is not dict or type(value.get("version")) is not int:
        raise FileCallObligationCodecError
    if value["version"] == FILE_CALL_OBLIGATION_PAYLOAD_VERSION:
        required = _BASE_FIELDS
    elif value["version"] == NUMERIC_FILE_CALL_OBLIGATION_PAYLOAD_VERSION:
        required = _BASE_FIELDS | {"bootstrap"}
    elif value["version"] == UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION:
        required = _BASE_FIELDS | {"upload_child", "upload_child_complete"}
    elif value["version"] == NUMERIC_UPLOAD_CHILD_OBLIGATION_PAYLOAD_VERSION:
        required = _BASE_FIELDS | {"bootstrap", "upload_child", "upload_child_complete"}
    else:
        raise FileCallObligationCodecError
    if not required <= set(value) <= required | _OPTIONAL_FIELDS:
        raise FileCallObligationCodecError
    bootstrap = _decode_bootstrap(value["bootstrap"]) if "bootstrap" in value else None
    upload_child = None
    if "upload_child" in value:
        child = value["upload_child"]
        if type(child) is not dict or set(child) != _UPLOAD_CHILD_FIELDS:
            raise FileCallObligationCodecError
        transfer_id = child["transfer_id"]
        if type(transfer_id) is not str or len(transfer_id) != 32 or not set(transfer_id) <= _LOWER_HEX:
            raise FileCallObligationCodecError
        upload_child = UploadChildAssociation(bytes.fromhex(transfer_id), child["member_ordinal"])
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
    effect_gate = None
    if "effect_gate" in value:
        try:
            effect_gate = decode_file_effect_gate(value["effect_gate"])
        except FileEffectGateError:
            raise FileCallObligationCodecError from None
    gate_setup = None
    if "gate_setup" in value:
        setup_value = value["gate_setup"]
        if type(setup_value) is not dict or set(setup_value) != _GATE_SETUP_FIELDS:
            raise FileCallObligationCodecError
        try:
            gate_setup = FileEffectGateSetup(
                setup_value["path"],
                _decode_guest(setup_value["guest"]),
            )
        except (TypeError, ValueError):
            raise FileCallObligationCodecError from None
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
            batch_index=value.get("batch_index"),
            scratch_reference=scratch_reference,
            scratch_cleanup_debt=scratch_cleanup_debt,
            publication_cleanup_debt=publication_cleanup_debt,
            effect_gate=effect_gate,
            gate_setup=gate_setup,
            uncertainty=uncertainty,
            bootstrap=bootstrap,
            upload_child=upload_child,
            upload_child_complete=value.get("upload_child_complete", False),
        )
    except FileCallObligationCodecError:
        raise
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _encode_guest(guest: VMGuestIdentity) -> dict[str, object]:
    if type(guest) is not VMGuestIdentity:
        raise FileCallObligationCodecError
    return {
        "boot_id": guest.boot_id,
        "init_start_ticks": guest.init_start_ticks,
        "instance_marker": guest.instance_marker,
    }


def _decode_guest(value: object) -> VMGuestIdentity:
    if type(value) is not dict or set(value) != _GUEST_FIELDS:
        raise FileCallObligationCodecError
    try:
        return VMGuestIdentity(value["instance_marker"], value["boot_id"], value["init_start_ticks"])
    except (TypeError, ValueError):
        raise FileCallObligationCodecError from None


def _decode_bootstrap(value: object) -> _NumericGuestBootstrap:
    if type(value) is not dict or set(value) != _BOOTSTRAP_FIELDS:
        raise FileCallObligationCodecError
    try:
        return _NumericGuestBootstrap(_decode_identity_plan(value["root_entry"]), _decode_guest(value["guest"]))
    except (TypeError, ValidationError, ValueError):
        raise FileCallObligationCodecError from None


def _validate_bootstrap(obligation: FileCallObligation) -> None:
    """Validate numeric launch identity at the persisted recovery boundary."""
    bootstrap = obligation.bootstrap
    if (
        type(bootstrap) is not _NumericGuestBootstrap
        or obligation.target.kind is not ManagedTargetKind.VM
        or obligation.runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or obligation.runtime_selection.explicit_path not in (None, "/usr/bin/python3")
    ):
        raise FileCallObligationCodecError
    guest = bootstrap.guest
    if vm_guest_boot_id(guest) != obligation.target.boot_id:
        raise FileCallObligationCodecError
    for gate in (obligation.effect_gate, obligation.gate_setup):
        if gate is not None and (type(gate) not in (FileEffectGateBinding, FileEffectGateSetup) or gate.guest != guest):
            raise FileCallObligationCodecError


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
