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
_MAX_OWNER_DISPLAY_LENGTH = 256
_APPLIED_KEY_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class AppliedStateKey(StrEnum):
    """Closed keys for applied facts understood by this repository."""

    HARDWARE_PROVENANCE = "hardware-provenance"
    SSH_IDENTITY = "ssh-identity"


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
        and len(repr(instance_name)) <= _MAX_OWNER_DISPLAY_LENGTH
    )


def _is_well_formed_applied_key(raw_key: object) -> TypeGuard[str]:
    return (
        isinstance(raw_key, str)
        and 0 < len(raw_key) <= _MAX_APPLIED_KEY_LENGTH
        and _APPLIED_KEY_PATTERN.fullmatch(raw_key) is not None
    )


def _state_error(detail: str, row: sqlite3.Row) -> StateError:
    instance_kind = row["instance_kind"]
    instance_name = row["instance_name"]
    hint = "Back up the state database before repairing it, or restore a known-good backup."
    if isinstance(instance_kind, str) and instance_kind in _INSTANCE_KINDS:
        if _is_safe_display_name(instance_name):
            return StateError(
                f"stored {instance_kind} {instance_name!r} instance state is malformed: {detail}",
                entity_kind=instance_kind,
                entity_name=instance_name,
                hint=hint,
            )
        return StateError(
            f"stored {instance_kind} instance state is malformed: {detail}",
            entity_kind=instance_kind,
            hint=hint,
        )
    return StateError(
        f"instance state record is malformed: {detail}",
        entity_kind="database",
        hint=hint,
    )


def _validate_instance_kind(instance_kind: str) -> InstanceKind:
    if instance_kind not in _INSTANCE_KINDS:
        raise ValueError(f"unsupported instance kind: {instance_kind}")
    return instance_kind


def _validate_identity(instance_kind: str, instance_name: str) -> InstanceKind:
    validated_kind = _validate_instance_kind(instance_kind)
    if not isinstance(instance_name, str) or not instance_name:
        raise ValueError("instance_name must be a nonempty string")
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
        raise _state_error("invalid instance kind", row)
    if not isinstance(instance_name, str) or not instance_name:
        raise _state_error("invalid instance name", row)
    if type(payload_version) is not int or payload_version <= 0:
        raise _state_error("invalid payload version", row)
    if not isinstance(value_json, str):
        raise _state_error("payload is not text", row)
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise _state_error("payload is not valid JSON", row) from error
    if not isinstance(value, dict):
        raise _state_error("payload is not a JSON object", row)
    try:
        payload = VersionedPayload(payload_version, cast("JsonObject", value))
    except (RecursionError, TypeError, ValueError) as error:
        raise _state_error("payload contains invalid JSON values", row) from error
    try:
        canonical_value = _encode_payload(payload)
    except (TypeError, ValueError) as error:
        raise _state_error("payload contains invalid JSON values", row) from error
    if canonical_value != value_json:
        raise _state_error("payload is not canonically encoded", row)
    if not isinstance(recorded_at, str):
        raise _state_error("recorded timestamp is not text", row)
    try:
        parsed_recorded_at = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise _state_error("recorded timestamp is invalid", row) from error
    if parsed_recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") != recorded_at:
        raise _state_error("recorded timestamp is not canonical UTC", row)
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
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation must be a nonempty string")
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

    def list_applied_slices(self, instance_kind: InstanceKind) -> tuple[AppliedStateSlice, ...]:
        _validate_instance_kind(instance_kind)
        rows = self._connection.execute(
            "SELECT * FROM instance_records WHERE instance_kind = ? AND record_type = ? "
            "ORDER BY instance_name, record_key",
            (instance_kind, _APPLIED_STATE),
        ).fetchall()
        return tuple(record for row in rows if (record := self._to_applied(row)) is not None)

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
            raise _state_error("invalid desired-overlay discriminator", row)
        if row["operation"] is not None:
            raise _state_error("desired overlay has lifecycle provenance", row)
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        return DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)

    @staticmethod
    def _to_applied(row: sqlite3.Row) -> AppliedStateSlice | None:
        if row["record_type"] != _APPLIED_STATE:
            raise _state_error("invalid applied-state discriminator", row)
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        raw_key = row["record_key"]
        operation = row["operation"]
        if not _is_well_formed_applied_key(raw_key):
            raise _state_error("invalid applied-state slice key", row)
        if not isinstance(operation, str) or not operation:
            raise _state_error("applied state has no lifecycle provenance", row)
        try:
            key = AppliedStateKey(raw_key)
        except ValueError:
            # A newer release may add an applied fact this release does not
            # consume. Partial replacement preserves that row, so omitting it
            # from this release's typed result is forward-compatible and lossless.
            return None
        if key not in _APPLIED_KEYS_BY_KIND[instance_kind]:
            raise _state_error("applied-state slice key is invalid for its owner kind", row)
        return AppliedStateSlice(instance_kind, instance_name, key, payload, operation, recorded_at)
