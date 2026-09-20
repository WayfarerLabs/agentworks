"""Durable ownership of coarse resource operations."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db.database import Database


_OPERATION_KIND = re.compile(r"[a-z](?:[a-z0-9]*)(?:-[a-z0-9]+)*\Z")
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class OperationResourceKind(StrEnum):
    """Core-owned coarse resource kinds that can serialize operations."""

    VM = "vm"
    PLATFORM_HOST = "platform-host"


class OperationClaimState(StrEnum):
    """Durable evidence recorded for an owned operation."""

    RESERVED = "reserved"
    POSSIBLE_DISPATCH = "possible-dispatch"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class OperationScope:
    """One core-selected resource whose operations must not overlap."""

    resource_kind: OperationResourceKind
    resource_name: str

    def __post_init__(self) -> None:
        _validate_scope(self)


@dataclass(frozen=True)
class OperationOwnership:
    """Fresh identity used to fence a claim from later claims on the scope.

    The identity is not a secret or an authentication credential. Core keeps
    the value returned by ``claim`` so delayed database updates and releases
    can be rejected after the resource has been claimed again. It does not
    fence remote work by itself.
    """

    scope: OperationScope
    operation_id: str

    def __post_init__(self) -> None:
        _validate_ownership(self)


@dataclass(frozen=True)
class OperationClaim:
    """Bounded, non-secret persisted facts about an operation claim."""

    ownership: OperationOwnership
    operation_kind: str
    state: OperationClaimState
    claimed_at: str
    updated_at: str


def _validate_scope(scope: OperationScope) -> None:
    if not isinstance(scope.resource_kind, OperationResourceKind):
        raise TypeError("resource_kind must use OperationResourceKind")
    if not isinstance(scope.resource_name, str):
        raise TypeError("resource_name must be a string")
    try:
        encoded_name = scope.resource_name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("resource_name must be valid UTF-8") from error
    if not encoded_name or len(encoded_name) > 255:
        raise ValueError("resource_name must contain 1 to 255 UTF-8 bytes")
    if any(not character.isprintable() for character in scope.resource_name):
        raise ValueError("resource_name must contain only printable characters")


def _validate_operation_kind(operation_kind: str) -> None:
    if not isinstance(operation_kind, str):
        raise TypeError("operation_kind must be a string")
    if len(operation_kind) > 64 or _OPERATION_KIND.fullmatch(operation_kind) is None:
        raise ValueError("operation_kind must be 1 to 64 lower-kebab ASCII characters")


def _validate_ownership(ownership: OperationOwnership) -> None:
    if not isinstance(ownership.scope, OperationScope):
        raise TypeError("scope must use OperationScope")
    if (
        not isinstance(ownership.operation_id, str)
        or len(ownership.operation_id) != 32
        or _IDENTIFIER.fullmatch(ownership.operation_id) is None
    ):
        raise ValueError("operation_id must be a 32-character lowercase hexadecimal identifier")


def _utc_now() -> str:
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def _decode_timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) != 20:
        raise ValueError
    parsed = datetime.strptime(value, _TIMESTAMP_FORMAT)
    if parsed.strftime(_TIMESTAMP_FORMAT) != value:
        raise ValueError
    return value


class OperationRepository:
    """Short, durable state transitions for coarse operation ownership.

    Claims have no lease or expiry and are never released by connection close,
    process exit, or exception cleanup.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._connection = database._conn  # noqa: SLF001

    def claim(self, scope: OperationScope, operation_kind: str) -> OperationOwnership:
        """Reserve one unclaimed scope and return its fresh stale-owner fence."""
        _validate_operation_kind(operation_kind)
        ownership = OperationOwnership(scope, uuid4().hex)
        now = _utc_now()

        with self._standalone_transaction():
            cursor = self._connection.execute(
                "INSERT INTO operation_claims "
                "(resource_kind, resource_name, operation_id, operation_kind, state, claimed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resource_kind, resource_name) DO NOTHING",
                (
                    scope.resource_kind,
                    scope.resource_name,
                    ownership.operation_id,
                    operation_kind,
                    OperationClaimState.RESERVED,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    "another operation already owns this resource",
                    entity_kind=scope.resource_kind,
                    entity_name=scope.resource_name,
                )
        return ownership

    def inspect(self, scope: OperationScope) -> OperationClaim | None:
        """Return bounded non-secret persisted facts for one resource claim."""
        row = self._connection.execute(
            "SELECT * FROM operation_claims WHERE resource_kind = ? AND resource_name = ?",
            (scope.resource_kind, scope.resource_name),
        ).fetchone()
        return None if row is None else self._decode_claim(row)

    def mark_possible_dispatch(self, ownership: OperationOwnership) -> OperationClaim:
        """Durably record that remote effects may occur before remote work starts."""
        return self._transition(
            ownership,
            expected=OperationClaimState.RESERVED,
            target=OperationClaimState.POSSIBLE_DISPATCH,
        )

    def record_effects_resolved(self, ownership: OperationOwnership) -> OperationClaim:
        """Record core's evidence that no remote effects can remain.

        Core must establish completion, rollback, or another operation-specific
        no-further-effects fact before calling this method. The database only
        persists that decision; it does not observe or prove remote quiescence.
        """
        return self._transition(
            ownership,
            expected=OperationClaimState.POSSIBLE_DISPATCH,
            target=OperationClaimState.RESOLVED,
        )

    def abandon_reserved(self, ownership: OperationOwnership) -> None:
        """Release a claim only while core knows dispatch was never possible."""
        self._delete(ownership, expected=OperationClaimState.RESERVED)

    def release_resolved(self, ownership: OperationOwnership) -> None:
        """Release a claim after core explicitly recorded effects as resolved."""
        self._delete(ownership, expected=OperationClaimState.RESOLVED)

    def _transition(
        self,
        ownership: OperationOwnership,
        *,
        expected: OperationClaimState,
        target: OperationClaimState,
    ) -> OperationClaim:
        updated_at = _utc_now()
        with self._standalone_transaction():
            cursor = self._connection.execute(
                "UPDATE operation_claims SET state = ?, updated_at = ? "
                "WHERE resource_kind = ? AND resource_name = ? AND operation_id = ? AND state = ?",
                (
                    target,
                    updated_at,
                    ownership.scope.resource_kind,
                    ownership.scope.resource_name,
                    ownership.operation_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_stale_or_invalid_state(ownership, expected)
            row = self._connection.execute(
                "SELECT * FROM operation_claims WHERE resource_kind = ? AND resource_name = ?",
                (ownership.scope.resource_kind, ownership.scope.resource_name),
            ).fetchone()
            assert row is not None
            return self._decode_claim(row)

    def _delete(self, ownership: OperationOwnership, *, expected: OperationClaimState) -> None:
        with self._standalone_transaction():
            cursor = self._connection.execute(
                "DELETE FROM operation_claims "
                "WHERE resource_kind = ? AND resource_name = ? AND operation_id = ? AND state = ?",
                (
                    ownership.scope.resource_kind,
                    ownership.scope.resource_name,
                    ownership.operation_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_stale_or_invalid_state(ownership, expected)

    def _raise_stale_or_invalid_state(
        self,
        ownership: OperationOwnership,
        expected: OperationClaimState,
    ) -> None:
        row = self._connection.execute(
            "SELECT * FROM operation_claims WHERE resource_kind = ? AND resource_name = ?",
            (ownership.scope.resource_kind, ownership.scope.resource_name),
        ).fetchone()
        claim = None if row is None else self._decode_claim(row)
        if claim is None or claim.ownership.operation_id != ownership.operation_id:
            raise StateError(
                "operation ownership is stale",
                entity_kind=ownership.scope.resource_kind,
                entity_name=ownership.scope.resource_name,
            )
        raise StateError(
            f"operation claim must be {expected.value!r}, not {claim.state.value!r}",
            entity_kind=ownership.scope.resource_kind,
            entity_name=ownership.scope.resource_name,
        )

    @contextmanager
    def _standalone_transaction(self) -> Iterator[None]:
        if self._database._read_only:  # noqa: SLF001
            raise StateError("operation claims require a writable database", entity_kind="database")
        if self._database._tx_depth or self._connection.in_transaction:  # noqa: SLF001
            raise StateError(
                "operation claim mutations cannot join another database transaction",
                entity_kind="database",
            )
        try:
            with self._database.transaction():
                yield
        except sqlite3.DatabaseError as error:
            from agentworks.db.backup import _is_busy
            from agentworks.errors import BusyStateError

            if _is_busy(error):
                raise BusyStateError() from error
            raise

    @staticmethod
    def _decode_claim(row: sqlite3.Row) -> OperationClaim:
        """Validate persisted operation state at the execution boundary."""
        try:
            scope = OperationScope(
                OperationResourceKind(row["resource_kind"]),
                row["resource_name"],
            )
            ownership = OperationOwnership(scope, row["operation_id"])
            operation_kind = row["operation_kind"]
            _validate_operation_kind(operation_kind)
            state = OperationClaimState(row["state"])
            claimed_at = _decode_timestamp(row["claimed_at"])
            updated_at = _decode_timestamp(row["updated_at"])
        except (KeyError, TypeError, ValueError):
            raise StateError(
                "persisted operation claim is malformed",
                entity_kind="database",
                hint="Repair the state database and reconcile outstanding remote operations before retrying.",
            ) from None
        return OperationClaim(ownership, operation_kind, state, claimed_at, updated_at)
