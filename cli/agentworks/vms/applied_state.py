"""Typed VM applied-state payloads and configured SSH identity policy."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agentworks.db import AppliedStateKey, AppliedStateSlice, VersionedPayload
from agentworks.errors import ConfigError, StateError
from agentworks.path_rendering import format_host_path
from agentworks.ssh_identity import (
    SSHIdentity,
    SSHIdentityReadError,
    UnverifiableSSHIdentity,
    VerifiedSSHIdentity,
    parse_public_ssh_identity,
    read_private_ssh_identity,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.db.instance_state import JsonObject
    from agentworks.vms.initializer.ssh_keys import AuthorizedKeysApplied

_PAYLOAD_VERSION = 1
_MAX_PUBLIC_KEY_BYTES = 1024 * 1024
_MAX_DIAGNOSTIC_CHARACTERS = 256
_FINGERPRINT_PATTERN = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


@dataclass(frozen=True, slots=True)
class HardwareProvenance:
    """Marker proving VM creation reached its successful terminal checkpoint."""


@dataclass(frozen=True, slots=True)
class VerifiedSSHAppliedState:
    """A configured private-key reference with authoritative public identity."""

    private_key_ref: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class UnverifiableSSHAppliedState:
    """A configured private-key reference whose format exposes no public identity."""

    private_key_ref: str


type SSHAppliedState = VerifiedSSHAppliedState | UnverifiableSSHAppliedState


@dataclass(frozen=True, slots=True)
class PreparedConfiguredSSHIdentity:
    """Validated public content and its configured private identity."""

    public_text: str
    private_key_ref: str
    identity: SSHIdentity


class VMSSHIdentityState(StrEnum):
    NOT_RECORDED = "not-recorded"
    UNVERIFIABLE = "unverifiable"
    MATCH = "match"
    DRIFT = "drift"


@dataclass(frozen=True, slots=True)
class VMSSHIdentityComparison:
    """Structural comparison of recorded and current configured SSH identity."""

    state: VMSSHIdentityState
    vm_name: str


def encode_hardware_provenance(_marker: HardwareProvenance | None = None) -> VersionedPayload:
    """Encode the version-1 empty hardware provenance marker."""
    return VersionedPayload(_PAYLOAD_VERSION, {})


def decode_hardware_provenance(record: AppliedStateSlice) -> HardwareProvenance:
    """Decode a persisted VM hardware marker at its trust boundary."""
    _require_vm_slice(record, AppliedStateKey.HARDWARE_PROVENANCE)
    if record.payload.payload_version != _PAYLOAD_VERSION:
        raise _unsupported_payload_version_error(record)
    if record.payload.value:
        raise _malformed_stored_state_error(record, "payload must be an empty object")
    return HardwareProvenance()


def encode_ssh_identity(private_key_ref: str | Path, identity: SSHIdentity) -> VersionedPayload:
    """Encode configured SSH identity evidence as the closed version-1 sum."""
    key_ref = str(private_key_ref)
    key_ref = _validated_private_key_ref(key_ref)
    if isinstance(identity, VerifiedSSHIdentity):
        fingerprint = _validated_fingerprint(identity.fingerprint)
        value = {
            "fingerprint": fingerprint,
            "private_key_ref": key_ref,
            "status": "verified",
        }
    elif isinstance(identity, UnverifiableSSHIdentity):
        value = {"private_key_ref": key_ref, "status": "unverifiable"}
    else:
        raise TypeError("identity must be a verified or unverifiable SSH identity")
    return VersionedPayload(_PAYLOAD_VERSION, cast("JsonObject", value))


def decode_ssh_identity(record: AppliedStateSlice) -> SSHAppliedState:
    """Decode persisted VM SSH evidence at its trust boundary."""
    _require_vm_slice(record, AppliedStateKey.SSH_IDENTITY)
    if record.payload.payload_version != _PAYLOAD_VERSION:
        raise _unsupported_payload_version_error(record)

    value = record.payload.value
    status = value.get("status")
    if status == "verified":
        if set(value) != {"fingerprint", "private_key_ref", "status"}:
            raise _malformed_stored_state_error(record, "verified payload has invalid fields")
        private_key_ref = value["private_key_ref"]
        fingerprint = value["fingerprint"]
        try:
            validated_ref = _validated_private_key_ref(private_key_ref)
            validated_fingerprint = _validated_fingerprint(fingerprint)
        except (TypeError, ValueError) as error:
            raise _malformed_stored_state_error(record, str(error)) from error
        return VerifiedSSHAppliedState(validated_ref, validated_fingerprint)
    if status == "unverifiable":
        if set(value) != {"private_key_ref", "status"}:
            raise _malformed_stored_state_error(record, "unverifiable payload has invalid fields")
        private_key_ref = value["private_key_ref"]
        try:
            validated_ref = _validated_private_key_ref(private_key_ref)
        except (TypeError, ValueError) as error:
            raise _malformed_stored_state_error(record, str(error)) from error
        return UnverifiableSSHAppliedState(validated_ref)
    raise _malformed_stored_state_error(record, "payload status is invalid")


def prepare_configured_ssh_identity(
    public_key_path: Path,
    private_key_path: Path,
) -> PreparedConfiguredSSHIdentity:
    """Read and cross-check the configured public and private identity."""
    public_text = _read_public_key_text(public_key_path)
    try:
        public_identity = parse_public_ssh_identity(public_text)
    except SSHIdentityReadError as error:
        raise _configured_identity_error(error, public_key_path) from error
    try:
        private_identity = read_private_ssh_identity(private_key_path)
    except SSHIdentityReadError as error:
        raise _configured_identity_error(error, private_key_path) from error
    if isinstance(private_identity, VerifiedSSHIdentity) and private_identity != public_identity:
        raise ConfigError(
            "configured SSH public and private keys do not identify the same key",
            hint="Set operator.ssh_public_key and operator.ssh_private_key to one matching SSH identity.",
        )
    return PreparedConfiguredSSHIdentity(public_text, str(private_key_path), private_identity)


def build_vm_initialization_slices(
    applied: AuthorizedKeysApplied | None,
    *,
    include_hardware: bool,
) -> Mapping[AppliedStateKey, VersionedPayload]:
    """Build slices proven by the post-write VM initialization outcome.

    A successful remote write is not enough to checkpoint SSH identity. The
    configured private carrier is read again through the exact reference used
    for that write so a replacement during initialization leaves no stale
    evidence.
    """
    slices: dict[AppliedStateKey, VersionedPayload] = {}
    if include_hardware:
        slices[AppliedStateKey.HARDWARE_PROVENANCE] = encode_hardware_provenance()
    if applied is None:
        return slices

    try:
        current_identity = read_private_ssh_identity(Path(applied.private_key_ref))
    except SSHIdentityReadError:
        return slices

    before = applied.identity
    stable_verified = (
        isinstance(before, VerifiedSSHIdentity)
        and isinstance(current_identity, VerifiedSSHIdentity)
        and before.fingerprint == current_identity.fingerprint
    )
    stable_unverifiable = isinstance(before, UnverifiableSSHIdentity) and isinstance(
        current_identity,
        UnverifiableSSHIdentity,
    )
    if stable_verified or stable_unverifiable:
        slices[AppliedStateKey.SSH_IDENTITY] = encode_ssh_identity(
            applied.private_key_ref,
            current_identity,
        )
    return slices


def compare_vm_ssh_identity(
    db: Database,
    vm_name: str,
    private_key_path: Path,
) -> VMSSHIdentityComparison:
    """Compare current configured private identity with persisted VM evidence."""
    try:
        current = read_private_ssh_identity(private_key_path)
    except SSHIdentityReadError as error:
        raise _configured_identity_error(error, private_key_path) from error

    record = next(
        (
            item
            for item in db.instance_state.get_applied_slices("vm", vm_name)
            if item.key is AppliedStateKey.SSH_IDENTITY
        ),
        None,
    )
    if record is None:
        return VMSSHIdentityComparison(VMSSHIdentityState.NOT_RECORDED, vm_name)

    applied = decode_ssh_identity(record)
    if isinstance(applied, UnverifiableSSHAppliedState) or isinstance(current, UnverifiableSSHIdentity):
        return VMSSHIdentityComparison(VMSSHIdentityState.UNVERIFIABLE, vm_name)
    state = VMSSHIdentityState.MATCH if applied.fingerprint == current.fingerprint else VMSSHIdentityState.DRIFT
    return VMSSHIdentityComparison(state, vm_name)


def require_vm_ssh_identity(
    comparison: VMSSHIdentityComparison,
    *,
    allow_not_recorded: bool = False,
) -> None:
    """Apply ordinary or establishment policy to a structural comparison."""
    if comparison.state in {VMSSHIdentityState.MATCH, VMSSHIdentityState.UNVERIFIABLE}:
        return
    if comparison.state is VMSSHIdentityState.NOT_RECORDED and allow_not_recorded:
        return
    if comparison.state is VMSSHIdentityState.NOT_RECORDED:
        vm_context, entity_name = _vm_context(comparison.vm_name)
        reinit_hint = (
            f"Run 'agw vm reinit {comparison.vm_name}' to establish SSH identity evidence."
            if entity_name is not None
            else "Run 'agw vm reinit' for this VM to establish SSH identity evidence."
        )
        raise StateError(
            f"{vm_context} has no recorded SSH identity",
            entity_kind="vm",
            entity_name=entity_name,
            hint=reinit_hint,
        )
    vm_context, entity_name = _vm_context(comparison.vm_name)
    raise StateError(
        f"configured SSH identity for {vm_context} does not match its recorded identity",
        entity_kind="vm",
        entity_name=entity_name,
        hint="Restore the private key matching the recorded fingerprint, or delete and recreate the VM.",
    )


def canonicalize_vm_applied_slice(record: AppliedStateSlice) -> VersionedPayload:
    """Decode and re-encode a known VM applied slice for safe export."""
    if record.key is AppliedStateKey.HARDWARE_PROVENANCE:
        return encode_hardware_provenance(decode_hardware_provenance(record))
    if record.key is AppliedStateKey.SSH_IDENTITY:
        applied = decode_ssh_identity(record)
        if isinstance(applied, VerifiedSSHAppliedState):
            identity: SSHIdentity = VerifiedSSHIdentity(applied.fingerprint)
        else:
            identity = UnverifiableSSHIdentity()
        return encode_ssh_identity(applied.private_key_ref, identity)
    raise TypeError(f"unsupported VM applied-state key: {record.key}")


def _read_public_key_text(path: Path) -> str:
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_PUBLIC_KEY_BYTES + 1)
    except (OSError, ValueError) as error:
        detail = f"cannot read configured SSH public key: {type(error).__name__}"
        raise ConfigError(
            f"configured SSH public key is unavailable: {_display_path(path)}",
            hint=detail,
        ) from error
    if len(raw) > _MAX_PUBLIC_KEY_BYTES:
        raise ConfigError(
            f"configured SSH public key is invalid: {_display_path(path)}",
            hint="The file exceeds 1 MiB.",
        )
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ConfigError(
            f"configured SSH public key is invalid: {_display_path(path)}",
            hint="The public key file must be UTF-8 text.",
        ) from error


def _configured_identity_error(error: SSHIdentityReadError, path: Path) -> ConfigError:
    return ConfigError(
        f"configured SSH key is {error.kind}: {_display_path(path)}",
        hint=error.detail,
    )


def _require_vm_slice(record: AppliedStateSlice, key: AppliedStateKey) -> None:
    if record.instance_kind != "vm" or record.key is not key:
        raise TypeError(f"{key.value} payload requires its matching VM applied-state slice")


def _validated_private_key_ref(value: object) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError("private_key_ref must be nonempty printable text")
    return value


def _validated_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ValueError("fingerprint must be an OpenSSH SHA-256 fingerprint")
    encoded = value.removeprefix("SHA256:")
    try:
        digest = base64.b64decode(f"{encoded}=", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("fingerprint must be an OpenSSH SHA-256 fingerprint") from error
    canonical = base64.b64encode(digest).decode("ascii").rstrip("=")
    if len(digest) != 32 or canonical != encoded:
        raise ValueError("fingerprint must be an OpenSSH SHA-256 fingerprint")
    return value


def _unsupported_payload_version_error(record: AppliedStateSlice) -> StateError:
    version = record.payload.payload_version
    if _is_safe_diagnostic_name(record.instance_name):
        message = (
            f"stored VM {record.instance_name!r} {record.key.value} applied state "
            f"uses unsupported payload version {version}"
        )
        entity_name = record.instance_name
    else:
        message = f"stored VM {record.key.value} applied state uses unsupported payload version {version}"
        entity_name = None
    return StateError(
        message,
        entity_kind="vm",
        entity_name=entity_name,
        hint="Use a compatible or newer Agentworks release to read this applied state.",
    )


def _malformed_stored_state_error(record: AppliedStateSlice, detail: str) -> StateError:
    if _is_safe_diagnostic_name(record.instance_name):
        message = f"stored VM {record.instance_name!r} {record.key.value} applied state is malformed: {detail}"
        entity_name = record.instance_name
    else:
        message = f"stored VM {record.key.value} applied state is malformed: {detail}"
        entity_name = None
    return StateError(
        message,
        entity_kind="vm",
        entity_name=entity_name,
        hint="Back up the state database before repairing it, or restore a known-good backup.",
    )


def _display_path(path: Path) -> str:
    rendered = format_host_path(path)
    if len(rendered) <= _MAX_DIAGNOSTIC_CHARACTERS:
        return rendered
    return f"{rendered[: _MAX_DIAGNOSTIC_CHARACTERS - 3]}..."


def _is_safe_diagnostic_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and len(repr(value)) <= _MAX_DIAGNOSTIC_CHARACTERS
    )


def _vm_context(vm_name: str) -> tuple[str, str | None]:
    if _is_safe_diagnostic_name(vm_name):
        return f"VM {vm_name!r}", vm_name
    return "VM", None
