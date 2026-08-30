"""Typed persistence for desired overlays and applied instance-state slices."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeGuard, cast

from agentworks.errors import StateError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from agentworks.db.database import Database

type InstanceKind = Literal["vm", "workspace", "agent", "session"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_DESIRED_OVERLAY = "desired-overlay"
_APPLIED_STATE = "applied-state"
_DESIRED_KEY = "spec"
_MAX_APPLIED_KEY_LENGTH = 64
_MAX_RECORD_DISCRIMINATOR_LENGTH = 64
_MAX_OWNER_DISPLAY_LENGTH = 256
_APPLIED_KEY_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_SAFE_METADATA_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-./][a-z0-9]+)*")
_SAFE_OWNER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")
_UTC_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class AppliedStateKey(StrEnum):
    """Closed keys for applied facts understood by this repository."""

    HARDWARE_PROVENANCE = "hardware-provenance"
    SSH_IDENTITY = "ssh-identity"


class InstanceRecordDiagnostic(StrEnum):
    """Closed, value-free reasons that a persisted record is malformed."""

    INVALID_INSTANCE_KIND = "invalid-instance-kind"
    INVALID_INSTANCE_NAME = "invalid-instance-name"
    INVALID_PAYLOAD_VERSION = "invalid-payload-version"
    INVALID_PAYLOAD = "invalid-payload"
    NONCANONICAL_PAYLOAD = "noncanonical-payload"
    INVALID_RECORDED_AT = "invalid-recorded-at"
    INVALID_FUTURE_DISCRIMINATOR = "invalid-future-discriminator"
    INVALID_DESIRED_DISCRIMINATOR = "invalid-desired-discriminator"
    INVALID_DESIRED_OPERATION = "invalid-desired-operation"
    INVALID_APPLIED_DISCRIMINATOR = "invalid-applied-discriminator"
    INVALID_APPLIED_KEY = "invalid-applied-key"
    INVALID_APPLIED_OPERATION = "invalid-applied-operation"
    APPLIED_KEY_OWNER_MISMATCH = "applied-key-owner-mismatch"


_APPLIED_KEYS_BY_KIND: dict[InstanceKind, frozenset[AppliedStateKey]] = {
    "vm": frozenset(
        {
            AppliedStateKey.HARDWARE_PROVENANCE,
            AppliedStateKey.SSH_IDENTITY,
        }
    ),
    "workspace": frozenset(),
    "agent": frozenset(),
    "session": frozenset(),
}
_INSTANCE_KINDS = frozenset(_APPLIED_KEYS_BY_KIND)


@dataclass(frozen=True)
class VersionedPayload:
    """A domain-validated JSON object paired with its codec version."""

    payload_version: int
    value: JsonObject

    def __post_init__(self) -> None:
        if type(self.payload_version) is not int or self.payload_version <= 0:
            raise ValueError("payload_version must be a positive integer")
        if not isinstance(self.value, dict):
            raise TypeError("value must be a JSON object")
        _validate_json_value(self.value)


@dataclass(frozen=True)
class DesiredOverlayRecord:
    """The desired declaration recorded for one live instance."""

    instance_kind: InstanceKind
    instance_name: str
    payload: VersionedPayload
    recorded_at: str


@dataclass(frozen=True)
class AppliedStateSlice:
    """One fact established by a completed lifecycle operation."""

    instance_kind: InstanceKind
    instance_name: str
    key: AppliedStateKey
    payload: VersionedPayload
    operation: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class InstanceRecordMetadata:
    """Value-free identity and version facts safe to expose during inspection."""

    instance_kind: InstanceKind | None
    instance_name: str | None
    record_type: str | None
    record_key: str | None
    payload_version: int | None
    recorded_at: str | None
    owner_exists: bool

    def __post_init__(self) -> None:
        if self.instance_kind is not None and self.instance_kind not in _INSTANCE_KINDS:
            raise ValueError("instance_kind metadata is invalid")
        if self.instance_name is not None and not _is_safe_display_name(self.instance_name):
            raise ValueError("instance_name metadata is unsafe")
        for label, value in (("record_type", self.record_type), ("record_key", self.record_key)):
            if value is not None and not _is_safe_record_metadata_text(value):
                raise ValueError(f"{label} metadata is unsafe")
        if self.payload_version is not None and (type(self.payload_version) is not int or self.payload_version <= 0):
            raise ValueError("payload_version metadata is invalid")
        if self.recorded_at is not None and not _is_safe_recorded_at(self.recorded_at):
            raise ValueError("recorded_at metadata is unsafe")
        if type(self.owner_exists) is not bool:
            raise TypeError("owner_exists metadata must be bool")


@dataclass(frozen=True, slots=True)
class InspectedDesiredOverlay:
    """One structurally valid desired record plus sanitized metadata."""

    record: DesiredOverlayRecord
    metadata: InstanceRecordMetadata

    @property
    def owner_exists(self) -> bool:
        """Whether the record's database owner exists."""
        return self.metadata.owner_exists


@dataclass(frozen=True, slots=True)
class InspectedAppliedStateSlice:
    """One recognized applied slice plus sanitized metadata."""

    record: AppliedStateSlice
    metadata: InstanceRecordMetadata

    @property
    def owner_exists(self) -> bool:
        """Whether the record's database owner exists."""
        return self.metadata.owner_exists


@dataclass(frozen=True, slots=True)
class UnconsumedInstanceRecord:
    """Safe metadata for a valid record this release does not understand."""

    metadata: InstanceRecordMetadata


@dataclass(frozen=True, slots=True)
class MalformedInstanceRecord:
    """Safe metadata and a value-free diagnostic for one damaged record."""

    metadata: InstanceRecordMetadata
    diagnostic: InstanceRecordDiagnostic


@dataclass(frozen=True, slots=True)
class InstanceStateInspection:
    """Closed singular or fleet inspection result with row failures isolated."""

    desired_overlays: tuple[InspectedDesiredOverlay, ...]
    applied_slices: tuple[InspectedAppliedStateSlice, ...]
    unconsumed_records: tuple[UnconsumedInstanceRecord, ...]
    malformed_records: tuple[MalformedInstanceRecord, ...]


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _encode_payload(payload: VersionedPayload) -> str:
    _validate_json_value(payload.value)
    return json.dumps(
        payload.value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_safe_display_name(instance_name: object) -> TypeGuard[str]:
    # The polymorphic record table deliberately has no owner foreign key, so a
    # damaged or hand-edited database can contain names that normal creation
    # paths reject. Bound and sanitize database-sourced identities before they
    # enter operator-facing errors.
    return (
        isinstance(instance_name, str)
        and bool(instance_name)
        and instance_name.isprintable()
        and _SAFE_OWNER_PATTERN.fullmatch(instance_name) is not None
        and len(repr(instance_name)) <= _MAX_OWNER_DISPLAY_LENGTH
    )


def _is_well_formed_applied_key(raw_key: object) -> TypeGuard[str]:
    return (
        isinstance(raw_key, str)
        and 0 < len(raw_key) <= _MAX_APPLIED_KEY_LENGTH
        and _APPLIED_KEY_PATTERN.fullmatch(raw_key) is not None
    )


def _is_well_formed_operation(operation: object) -> TypeGuard[str]:
    return _is_well_formed_applied_key(operation)


def _is_safe_record_metadata_text(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_RECORD_DISCRIMINATOR_LENGTH
        and _SAFE_METADATA_PATTERN.fullmatch(value) is not None
    )


def _is_safe_recorded_at(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _UTC_TIMESTAMP_PATTERN.fullmatch(value) is not None


def _safe_record_metadata(row: sqlite3.Row) -> InstanceRecordMetadata:
    """Project database-sourced metadata without exposing unsafe text."""
    raw_kind = row["instance_kind"]
    instance_kind: InstanceKind | None = raw_kind if isinstance(raw_kind, str) and raw_kind in _INSTANCE_KINDS else None
    raw_name = row["instance_name"]
    instance_name = raw_name if _is_safe_display_name(raw_name) else None
    raw_version = row["payload_version"]
    payload_version = raw_version if type(raw_version) is int and raw_version > 0 else None
    raw_recorded_at = row["recorded_at"]
    recorded_at = raw_recorded_at if _is_safe_recorded_at(raw_recorded_at) else None
    raw_type = row["record_type"]
    raw_key = row["record_key"]
    return InstanceRecordMetadata(
        instance_kind=instance_kind,
        instance_name=instance_name,
        record_type=raw_type if _is_safe_record_metadata_text(raw_type) else None,
        record_key=raw_key if _is_safe_record_metadata_text(raw_key) else None,
        payload_version=payload_version,
        recorded_at=recorded_at,
        owner_exists=bool(row["owner_exists"]),
    )


_DIAGNOSTIC_DETAILS: dict[InstanceRecordDiagnostic, str] = {
    InstanceRecordDiagnostic.INVALID_INSTANCE_KIND: "invalid instance kind",
    InstanceRecordDiagnostic.INVALID_INSTANCE_NAME: "invalid instance name",
    InstanceRecordDiagnostic.INVALID_PAYLOAD_VERSION: "invalid payload version",
    InstanceRecordDiagnostic.INVALID_PAYLOAD: "invalid payload",
    InstanceRecordDiagnostic.NONCANONICAL_PAYLOAD: "payload is not canonically encoded",
    InstanceRecordDiagnostic.INVALID_RECORDED_AT: "invalid recorded timestamp",
    InstanceRecordDiagnostic.INVALID_FUTURE_DISCRIMINATOR: "invalid future record discriminator",
    InstanceRecordDiagnostic.INVALID_DESIRED_DISCRIMINATOR: "invalid desired-overlay discriminator",
    InstanceRecordDiagnostic.INVALID_DESIRED_OPERATION: "desired overlay has lifecycle provenance",
    InstanceRecordDiagnostic.INVALID_APPLIED_DISCRIMINATOR: "invalid applied-state discriminator",
    InstanceRecordDiagnostic.INVALID_APPLIED_KEY: "invalid applied-state slice key",
    InstanceRecordDiagnostic.INVALID_APPLIED_OPERATION: "invalid applied-state lifecycle provenance",
    InstanceRecordDiagnostic.APPLIED_KEY_OWNER_MISMATCH: "applied-state slice key is invalid for its owner kind",
}


class _MalformedPersistedRecord(StateError):
    """Internal typed failure carrying only a closed inspection diagnostic."""

    def __init__(
        self,
        message: str,
        diagnostic: InstanceRecordDiagnostic,
        *,
        entity_kind: str,
        entity_name: str | None,
        hint: str,
    ) -> None:
        super().__init__(
            message,
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint=hint,
        )
        self.diagnostic = diagnostic


def _state_error(diagnostic: InstanceRecordDiagnostic, row: sqlite3.Row) -> _MalformedPersistedRecord:
    instance_kind = row["instance_kind"]
    instance_name = row["instance_name"]
    detail = _DIAGNOSTIC_DETAILS[diagnostic]
    hint = "Back up the state database before repairing it, or restore a known-good backup."
    if isinstance(instance_kind, str) and instance_kind in _INSTANCE_KINDS:
        if _is_safe_display_name(instance_name):
            return _MalformedPersistedRecord(
                f"stored {instance_kind} {instance_name!r} instance state is malformed: {detail}",
                diagnostic,
                entity_kind=instance_kind,
                entity_name=instance_name,
                hint=hint,
            )
        return _MalformedPersistedRecord(
            f"stored {instance_kind} instance state is malformed: {detail}",
            diagnostic,
            entity_kind=instance_kind,
            entity_name=None,
            hint=hint,
        )
    return _MalformedPersistedRecord(
        f"instance state record is malformed: {detail}",
        diagnostic,
        entity_kind="database",
        entity_name=None,
        hint=hint,
    )


def _validate_instance_kind(instance_kind: str) -> InstanceKind:
    if instance_kind not in _INSTANCE_KINDS:
        raise ValueError(f"unsupported instance kind: {instance_kind}")
    return instance_kind


def _validate_identity(instance_kind: str, instance_name: str) -> InstanceKind:
    validated_kind = _validate_instance_kind(instance_kind)
    if not _is_safe_display_name(instance_name):
        raise ValueError("instance_name must be a bounded safe identifier")
    return validated_kind


def _validate_applied_key(instance_kind: InstanceKind, key: AppliedStateKey) -> None:
    if not isinstance(key, AppliedStateKey):
        raise TypeError("applied-state slice keys must use AppliedStateKey")
    if not _is_well_formed_applied_key(key.value):
        raise ValueError(f"registered applied-state key {key.value!r} is malformed")
    if key not in _APPLIED_KEYS_BY_KIND[instance_kind]:
        raise ValueError(f"applied-state key {key.value!r} is not valid for {instance_kind}")


def _decode_common(row: sqlite3.Row) -> tuple[InstanceKind, str, VersionedPayload, str]:
    instance_kind = row["instance_kind"]
    instance_name = row["instance_name"]
    payload_version = row["payload_version"]
    value_json = row["value_json"]
    recorded_at = row["recorded_at"]

    if not isinstance(instance_kind, str) or instance_kind not in _INSTANCE_KINDS:
        raise _state_error(InstanceRecordDiagnostic.INVALID_INSTANCE_KIND, row)
    if not _is_safe_display_name(instance_name):
        raise _state_error(InstanceRecordDiagnostic.INVALID_INSTANCE_NAME, row)
    if type(payload_version) is not int or payload_version <= 0:
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD_VERSION, row)
    if not isinstance(value_json, str):
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD, row)
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD, row) from error
    if not isinstance(value, dict):
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD, row)
    try:
        payload = VersionedPayload(payload_version, cast("JsonObject", value))
    except (RecursionError, TypeError, ValueError) as error:
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD, row) from error
    try:
        canonical_value = _encode_payload(payload)
    except (TypeError, ValueError) as error:
        raise _state_error(InstanceRecordDiagnostic.INVALID_PAYLOAD, row) from error
    if canonical_value != value_json:
        raise _state_error(InstanceRecordDiagnostic.NONCANONICAL_PAYLOAD, row)
    if not isinstance(recorded_at, str):
        raise _state_error(InstanceRecordDiagnostic.INVALID_RECORDED_AT, row)
    try:
        parsed_recorded_at = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise _state_error(InstanceRecordDiagnostic.INVALID_RECORDED_AT, row) from error
    if parsed_recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") != recorded_at:
        raise _state_error(InstanceRecordDiagnostic.INVALID_RECORDED_AT, row)
    return instance_kind, instance_name, payload, recorded_at


class InstanceStateRepository:
    """Closed, consumer-shaped instance-state operations on a Database connection."""

    def __init__(self, database: Database) -> None:
        self._connection = database._conn  # noqa: SLF001
        self._transaction = database.transaction

    def get_desired_overlay(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
    ) -> DesiredOverlayRecord | None:
        _validate_identity(instance_kind, instance_name)
        row = self._connection.execute(
            "SELECT * FROM instance_records "
            "WHERE instance_kind = ? AND instance_name = ? "
            "AND record_type = ? AND record_key = ?",
            (instance_kind, instance_name, _DESIRED_OVERLAY, _DESIRED_KEY),
        ).fetchone()
        return None if row is None else self._to_desired(row)

    def has_instance_records(self, instance_kind: InstanceKind, instance_name: str) -> bool:
        """Whether any current or future state record exists for this identity."""
        _validate_identity(instance_kind, instance_name)
        row = self._connection.execute(
            "SELECT 1 FROM instance_records WHERE instance_kind = ? AND instance_name = ? LIMIT 1",
            (instance_kind, instance_name),
        ).fetchone()
        return row is not None

    def put_desired_overlay(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
        payload: VersionedPayload,
    ) -> DesiredOverlayRecord:
        _validate_identity(instance_kind, instance_name)
        value_json = _encode_payload(payload)
        recorded_at = _utc_now()
        with self._transaction():
            self._connection.execute(
                "INSERT INTO instance_records "
                "(instance_kind, instance_name, record_type, record_key, payload_version, "
                "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(instance_kind, instance_name, record_type, record_key) DO UPDATE SET "
                "payload_version = excluded.payload_version, value_json = excluded.value_json, "
                "recorded_at = excluded.recorded_at, operation = NULL",
                (
                    instance_kind,
                    instance_name,
                    _DESIRED_OVERLAY,
                    _DESIRED_KEY,
                    payload.payload_version,
                    value_json,
                    recorded_at,
                ),
            )
        return DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)

    def clear_desired_overlay(self, instance_kind: InstanceKind, instance_name: str) -> None:
        _validate_identity(instance_kind, instance_name)
        with self._transaction():
            self._connection.execute(
                "DELETE FROM instance_records "
                "WHERE instance_kind = ? AND instance_name = ? "
                "AND record_type = ? AND record_key = ?",
                (instance_kind, instance_name, _DESIRED_OVERLAY, _DESIRED_KEY),
            )

    def list_desired_overlays(self, instance_kind: InstanceKind) -> tuple[DesiredOverlayRecord, ...]:
        _validate_instance_kind(instance_kind)
        rows = self._connection.execute(
            "SELECT * FROM instance_records WHERE instance_kind = ? AND record_type = ? "
            "ORDER BY instance_name, record_key",
            (instance_kind, _DESIRED_OVERLAY),
        ).fetchall()
        return tuple(self._to_desired(row) for row in rows)

    def has_vm_owner_tree_desired_overlay(self, vm_name: str) -> bool:
        """Whether a VM or any of its current descendants has a desired overlay."""
        return bool(self._vm_owner_tree_desired_overlay_rows(vm_name, limit_one=True))

    def list_vm_owner_tree_desired_overlays(self, vm_name: str) -> tuple[DesiredOverlayRecord, ...]:
        """Desired overlays belonging to one VM's current owner tree."""
        rows = self._vm_owner_tree_desired_overlay_rows(vm_name, limit_one=False)
        return tuple(self._to_desired(row) for row in rows)

    def _vm_owner_tree_desired_overlay_rows(
        self,
        vm_name: str,
        *,
        limit_one: bool,
    ) -> list[sqlite3.Row]:
        """Select the closed VM backup projection before decoding payloads."""
        _validate_identity("vm", vm_name)
        limit = " LIMIT 1" if limit_one else ""
        rows = self._connection.execute(
            "SELECT state.* FROM instance_records AS state "
            "WHERE state.record_type = ? AND state.record_key = ? AND ("
            "(state.instance_kind = 'vm' AND state.instance_name = ?) OR "
            "(state.instance_kind = 'workspace' AND state.instance_name IN "
            " (SELECT name FROM workspaces WHERE vm_name = ?)) OR "
            "(state.instance_kind = 'agent' AND state.instance_name IN "
            " (SELECT name FROM agents WHERE vm_name = ?)) OR "
            "(state.instance_kind = 'session' AND state.instance_name IN "
            " (SELECT sessions.name FROM sessions JOIN workspaces "
            "  ON sessions.workspace_name = workspaces.name WHERE workspaces.vm_name = ?))"
            ") ORDER BY CASE state.instance_kind "
            "WHEN 'vm' THEN 0 WHEN 'workspace' THEN 1 WHEN 'agent' THEN 2 ELSE 3 END, "
            "state.instance_name" + limit,
            (_DESIRED_OVERLAY, _DESIRED_KEY, vm_name, vm_name, vm_name, vm_name),
        ).fetchall()
        return rows

    def get_applied_slices(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
    ) -> tuple[AppliedStateSlice, ...]:
        _validate_identity(instance_kind, instance_name)
        rows = self._connection.execute(
            "SELECT * FROM instance_records "
            "WHERE instance_kind = ? AND instance_name = ? AND record_type = ? "
            "ORDER BY record_key",
            (instance_kind, instance_name, _APPLIED_STATE),
        ).fetchall()
        return tuple(record for row in rows if (record := self._to_applied(row)) is not None)

    def replace_applied_slices(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
        operation: str,
        slices: Mapping[AppliedStateKey, VersionedPayload],
    ) -> tuple[AppliedStateSlice, ...]:
        """Upsert supplied slices atomically while preserving every unlisted slice."""
        validated_kind = _validate_identity(instance_kind, instance_name)
        if not _is_well_formed_operation(operation):
            raise ValueError("operation must be 1 to 64 lower-kebab ASCII characters")
        encoded: list[tuple[AppliedStateKey, VersionedPayload, str]] = []
        for key, payload in slices.items():
            _validate_applied_key(validated_kind, key)
            encoded.append((key, payload, _encode_payload(payload)))
        if not encoded:
            return self.get_applied_slices(instance_kind, instance_name)

        recorded_at = _utc_now()
        values_sql = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?)" for _ in encoded)
        parameters: list[object] = []
        for key, payload, value_json in encoded:
            parameters.extend(
                (
                    instance_kind,
                    instance_name,
                    _APPLIED_STATE,
                    key,
                    payload.payload_version,
                    value_json,
                    recorded_at,
                    operation,
                )
            )
        with self._transaction():
            self._connection.execute(
                "INSERT INTO instance_records "
                "(instance_kind, instance_name, record_type, record_key, payload_version, "
                f"value_json, recorded_at, operation) VALUES {values_sql} "
                "ON CONFLICT(instance_kind, instance_name, record_type, record_key) DO UPDATE SET "
                "payload_version = excluded.payload_version, value_json = excluded.value_json, "
                "recorded_at = excluded.recorded_at, operation = excluded.operation",
                parameters,
            )
        return self.get_applied_slices(instance_kind, instance_name)

    def clear_applied_slice(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
        key: AppliedStateKey,
    ) -> None:
        """Delete one registered applied slice for one owner."""
        validated_kind = _validate_identity(instance_kind, instance_name)
        _validate_applied_key(validated_kind, key)

        with self._transaction():
            self._connection.execute(
                "DELETE FROM instance_records "
                "WHERE instance_kind = ? AND instance_name = ? AND record_type = ? "
                "AND record_key = ?",
                (instance_kind, instance_name, _APPLIED_STATE, key),
            )

    def list_applied_slices(self, instance_kind: InstanceKind) -> tuple[AppliedStateSlice, ...]:
        _validate_instance_kind(instance_kind)
        rows = self._connection.execute(
            "SELECT * FROM instance_records WHERE instance_kind = ? AND record_type = ? "
            "ORDER BY instance_name, record_key",
            (instance_kind, _APPLIED_STATE),
        ).fetchall()
        return tuple(record for row in rows if (record := self._to_applied(row)) is not None)

    def inspect_owner_state(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
    ) -> InstanceStateInspection:
        """Inspect every stored record for one exact typed owner."""
        _validate_identity(instance_kind, instance_name)
        return self._inspect(instance_kind=instance_kind, instance_name=instance_name)

    def inspect_all_instance_state(self) -> InstanceStateInspection:
        """Inspect the complete store in one deterministic fleet query."""
        return self._inspect()

    def _inspect(
        self,
        *,
        instance_kind: InstanceKind | None = None,
        instance_name: str | None = None,
    ) -> InstanceStateInspection:
        where = ""
        parameters: tuple[object, ...] = ()
        if instance_kind is not None:
            assert instance_name is not None
            where = "WHERE state.instance_kind = ? AND state.instance_name = ? "
            parameters = (instance_kind, instance_name)
        rows = self._connection.execute(
            "SELECT state.*, CASE state.instance_kind "
            "WHEN 'vm' THEN EXISTS(SELECT 1 FROM vms WHERE name = state.instance_name) "
            "WHEN 'workspace' THEN EXISTS(SELECT 1 FROM workspaces WHERE name = state.instance_name) "
            "WHEN 'agent' THEN EXISTS(SELECT 1 FROM agents WHERE name = state.instance_name) "
            "WHEN 'session' THEN EXISTS(SELECT 1 FROM sessions WHERE name = state.instance_name) "
            "ELSE 0 END AS owner_exists FROM instance_records AS state "
            + where
            + "ORDER BY state.instance_kind, state.instance_name, state.record_type, state.record_key",
            parameters,
        ).fetchall()

        desired: list[InspectedDesiredOverlay] = []
        applied: list[InspectedAppliedStateSlice] = []
        unconsumed: list[UnconsumedInstanceRecord] = []
        malformed: list[MalformedInstanceRecord] = []
        for row in rows:
            metadata = _safe_record_metadata(row)
            try:
                record_type = row["record_type"]
                if record_type == _DESIRED_OVERLAY:
                    desired.append(InspectedDesiredOverlay(self._to_desired(row), metadata))
                    continue
                if record_type == _APPLIED_STATE:
                    record = self._to_applied(row)
                    if record is not None:
                        applied.append(InspectedAppliedStateSlice(record, metadata))
                    else:
                        unconsumed.append(UnconsumedInstanceRecord(metadata))
                    continue
                if not isinstance(record_type, str) or not record_type:
                    raise _state_error(InstanceRecordDiagnostic.INVALID_FUTURE_DISCRIMINATOR, row)
                raw_key = row["record_key"]
                if not isinstance(raw_key, str) or not raw_key:
                    raise _state_error(InstanceRecordDiagnostic.INVALID_FUTURE_DISCRIMINATOR, row)
                _decode_common(row)
                unconsumed.append(UnconsumedInstanceRecord(metadata))
            except _MalformedPersistedRecord as error:
                malformed.append(MalformedInstanceRecord(metadata, error.diagnostic))
        return InstanceStateInspection(
            desired_overlays=tuple(desired),
            applied_slices=tuple(applied),
            unconsumed_records=tuple(unconsumed),
            malformed_records=tuple(malformed),
        )

    def _delete_instance_records_for_names(
        self,
        instance_kind: InstanceKind,
        instance_names: tuple[str, ...],
    ) -> None:
        """Delete a known typed owner batch inside the caller's transaction."""
        if not instance_names:
            return
        _validate_instance_kind(instance_kind)
        self._connection.executemany(
            "DELETE FROM instance_records WHERE instance_kind = ? AND instance_name = ?",
            ((instance_kind, instance_name) for instance_name in instance_names),
        )

    @staticmethod
    def _to_desired(row: sqlite3.Row) -> DesiredOverlayRecord:
        if row["record_type"] != _DESIRED_OVERLAY or row["record_key"] != _DESIRED_KEY:
            raise _state_error(InstanceRecordDiagnostic.INVALID_DESIRED_DISCRIMINATOR, row)
        if row["operation"] is not None:
            raise _state_error(InstanceRecordDiagnostic.INVALID_DESIRED_OPERATION, row)
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        return DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)

    @staticmethod
    def _to_applied(row: sqlite3.Row) -> AppliedStateSlice | None:
        if row["record_type"] != _APPLIED_STATE:
            raise _state_error(InstanceRecordDiagnostic.INVALID_APPLIED_DISCRIMINATOR, row)
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        raw_key = row["record_key"]
        operation = row["operation"]
        if not _is_well_formed_applied_key(raw_key):
            raise _state_error(InstanceRecordDiagnostic.INVALID_APPLIED_KEY, row)
        if not _is_well_formed_operation(operation):
            raise _state_error(InstanceRecordDiagnostic.INVALID_APPLIED_OPERATION, row)
        try:
            key = AppliedStateKey(raw_key)
        except ValueError:
            # A newer release may add an applied fact this release does not
            # consume. Partial replacement preserves that row, so omitting it
            # from this release's typed result is forward-compatible and lossless.
            return None
        if key not in _APPLIED_KEYS_BY_KIND[instance_kind]:
            raise _state_error(InstanceRecordDiagnostic.APPLIED_KEY_OWNER_MISMATCH, row)
        return AppliedStateSlice(instance_kind, instance_name, key, payload, operation, recorded_at)
