"""Typed persistence for desired overlays and applied instance-state slices."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from agentworks.errors import StateError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from agentworks.db.database import Database

type InstanceKind = Literal["vm", "workspace", "agent", "session"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_INSTANCE_KINDS = frozenset({"vm", "workspace", "agent", "session"})
_DESIRED_OVERLAY = "desired-overlay"
_APPLIED_STATE = "applied-state"
_DESIRED_KEY = "spec"


@dataclass(frozen=True)
class VersionedPayload:
    """A domain-validated JSON object paired with its codec version."""

    schema_version: int
    value: JsonObject

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
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
    key: str
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


def _state_error(detail: str) -> StateError:
    return StateError(f"instance state record is malformed: {detail}", entity_kind="database")


def _validate_instance_kind(instance_kind: str) -> InstanceKind:
    if instance_kind not in _INSTANCE_KINDS:
        raise ValueError(f"unsupported instance kind: {instance_kind}")
    return cast("InstanceKind", instance_kind)


def _validate_identity(instance_kind: str, instance_name: str) -> InstanceKind:
    validated_kind = _validate_instance_kind(instance_kind)
    if not instance_name:
        raise ValueError("instance_name must not be empty")
    return validated_kind


def _decode_common(row: sqlite3.Row) -> tuple[InstanceKind, str, VersionedPayload, str]:
    instance_kind = row["instance_kind"]
    instance_name = row["instance_name"]
    schema_version = row["schema_version"]
    value_json = row["value_json"]
    recorded_at = row["recorded_at"]

    if not isinstance(instance_kind, str) or instance_kind not in _INSTANCE_KINDS:
        raise _state_error("invalid instance kind")
    if not isinstance(instance_name, str) or not instance_name:
        raise _state_error("invalid instance name")
    if type(schema_version) is not int or schema_version <= 0:
        raise _state_error("invalid schema version")
    if not isinstance(value_json, str):
        raise _state_error("payload is not text")
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise _state_error("payload is not valid JSON") from error
    if not isinstance(value, dict):
        raise _state_error("payload is not a JSON object")
    try:
        payload = VersionedPayload(schema_version, cast("JsonObject", value))
    except (RecursionError, TypeError, ValueError) as error:
        raise _state_error("payload contains invalid JSON values") from error
    try:
        canonical_value = _encode_payload(payload)
    except (TypeError, ValueError) as error:
        raise _state_error("payload contains invalid JSON values") from error
    if canonical_value != value_json:
        raise _state_error("payload is not canonically encoded")
    if not isinstance(recorded_at, str):
        raise _state_error("recorded timestamp is not text")
    try:
        parsed_recorded_at = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise _state_error("recorded timestamp is invalid") from error
    if parsed_recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") != recorded_at:
        raise _state_error("recorded timestamp is not canonical UTC")
    return cast("InstanceKind", instance_kind), instance_name, payload, recorded_at


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
            "AND record_kind = ? AND record_key = ?",
            (instance_kind, instance_name, _DESIRED_OVERLAY, _DESIRED_KEY),
        ).fetchone()
        return None if row is None else self._to_desired(row)

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
                "(instance_kind, instance_name, record_kind, record_key, schema_version, "
                "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(instance_kind, instance_name, record_kind, record_key) DO UPDATE SET "
                "schema_version = excluded.schema_version, value_json = excluded.value_json, "
                "recorded_at = excluded.recorded_at, operation = NULL",
                (
                    instance_kind,
                    instance_name,
                    _DESIRED_OVERLAY,
                    _DESIRED_KEY,
                    payload.schema_version,
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
                "AND record_kind = ? AND record_key = ?",
                (instance_kind, instance_name, _DESIRED_OVERLAY, _DESIRED_KEY),
            )

    def list_desired_overlays(self, instance_kind: InstanceKind) -> tuple[DesiredOverlayRecord, ...]:
        _validate_instance_kind(instance_kind)
        rows = self._connection.execute(
            "SELECT * FROM instance_records WHERE instance_kind = ? AND record_kind = ? "
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
            "WHERE instance_kind = ? AND instance_name = ? AND record_kind = ? "
            "ORDER BY record_key",
            (instance_kind, instance_name, _APPLIED_STATE),
        ).fetchall()
        return tuple(self._to_applied(row) for row in rows)

    def replace_applied_slices(
        self,
        instance_kind: InstanceKind,
        instance_name: str,
        operation: str,
        slices: Mapping[str, VersionedPayload],
    ) -> tuple[AppliedStateSlice, ...]:
        _validate_identity(instance_kind, instance_name)
        if not operation:
            raise ValueError("operation must not be empty")
        encoded: list[tuple[str, VersionedPayload, str]] = []
        for key, payload in slices.items():
            if not key:
                raise ValueError("applied-state slice keys must not be empty")
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
                    payload.schema_version,
                    value_json,
                    recorded_at,
                    operation,
                )
            )
        with self._transaction():
            self._connection.execute(
                "INSERT INTO instance_records "
                "(instance_kind, instance_name, record_kind, record_key, schema_version, "
                f"value_json, recorded_at, operation) VALUES {values_sql} "
                "ON CONFLICT(instance_kind, instance_name, record_kind, record_key) DO UPDATE SET "
                "schema_version = excluded.schema_version, value_json = excluded.value_json, "
                "recorded_at = excluded.recorded_at, operation = excluded.operation",
                parameters,
            )
        return self.get_applied_slices(instance_kind, instance_name)

    def list_applied_slices(self, instance_kind: InstanceKind) -> tuple[AppliedStateSlice, ...]:
        _validate_instance_kind(instance_kind)
        rows = self._connection.execute(
            "SELECT * FROM instance_records WHERE instance_kind = ? AND record_kind = ? "
            "ORDER BY instance_name, record_key",
            (instance_kind, _APPLIED_STATE),
        ).fetchall()
        return tuple(self._to_applied(row) for row in rows)

    def delete_instance_records(self, instance_kind: InstanceKind, instance_name: str) -> None:
        _validate_identity(instance_kind, instance_name)
        with self._transaction():
            self._delete_instance_records_for_names(instance_kind, (instance_name,))

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
        if row["record_kind"] != _DESIRED_OVERLAY or row["record_key"] != _DESIRED_KEY:
            raise _state_error("invalid desired-overlay discriminator")
        if row["operation"] is not None:
            raise _state_error("desired overlay has lifecycle provenance")
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        return DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)

    @staticmethod
    def _to_applied(row: sqlite3.Row) -> AppliedStateSlice:
        if row["record_kind"] != _APPLIED_STATE:
            raise _state_error("invalid applied-state discriminator")
        key = row["record_key"]
        operation = row["operation"]
        if not isinstance(key, str) or not key:
            raise _state_error("invalid applied-state slice key")
        if not isinstance(operation, str) or not operation:
            raise _state_error("applied state has no lifecycle provenance")
        instance_kind, instance_name, payload, recorded_at = _decode_common(row)
        return AppliedStateSlice(instance_kind, instance_name, key, payload, operation, recorded_at)
