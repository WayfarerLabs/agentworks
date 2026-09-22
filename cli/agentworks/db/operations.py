"""Durable ownership of coarse resource operations."""

from __future__ import annotations

import re
import sqlite3
import threading
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
_CLAIM_SELECT = """
    SELECT
        claims.resource_kind,
        claims.resource_name,
        claims.operation_id,
        owners.generation_id,
        owners.recovery_predecessor_generation_id,
        owners.operation_kind,
        owners.state,
        owners.claimed_at,
        owners.updated_at,
        owners.obligations_sealed_at
    FROM operation_claims AS claims
    JOIN operation_owners AS owners ON owners.operation_id = claims.operation_id
"""

# Lifecycle payloads are recovery metadata, not a general workflow store.
# Keep the shared envelope small enough that a stuck operation remains cheap
# to inspect, back up, and move through SQLite's single writer.
MAX_LIFECYCLE_OBLIGATIONS = 128
MAX_LIFECYCLE_PAYLOAD_BYTES = 8_192
MAX_LIFECYCLE_PAYLOAD_VERSION = 2_147_483_647


class OperationResourceKind(StrEnum):
    """Core-owned coarse resource kinds that can serialize operations."""

    VM = "vm"
    PLATFORM_HOST = "platform-host"


class OperationClaimState(StrEnum):
    """Durable evidence recorded for an owned operation."""

    RESERVED = "reserved"
    POSSIBLE_DISPATCH = "possible-dispatch"
    RESOLVED = "resolved"


class LifecycleObligationState(StrEnum):
    """Closed durable states for one independently discharged effect."""

    REGISTERED = "registered"
    POSSIBLE_EFFECT = "possible-effect"
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
    generation_id: str

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
    obligations_sealed_at: str | None


@dataclass(frozen=True)
class LifecycleObligation:
    """Bounded adapter-owned recovery facts for one exact operation effect.

    ``payload`` is opaque to this repository. Callers must keep it non-secret
    and limited to durable recovery identity or receipts, never command input,
    output, credentials, or arbitrary workflow state.
    """

    ownership: OperationOwnership
    obligation_id: str
    obligation_kind: str
    state: LifecycleObligationState
    payload_version: int
    payload: bytes
    payload_revision: int
    registered_at: str
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


def _validate_obligation_id(obligation_id: str) -> None:
    if not isinstance(obligation_id, str) or len(obligation_id) != 32 or _IDENTIFIER.fullmatch(obligation_id) is None:
        raise ValueError("obligation_id must be a 32-character lowercase hexadecimal identifier")


def _validate_generation_id(generation_id: str) -> None:
    if not isinstance(generation_id, str) or len(generation_id) != 32 or _IDENTIFIER.fullmatch(generation_id) is None:
        raise ValueError("generation_id must be a 32-character lowercase hexadecimal identifier")


def _validate_payload(payload_version: int, payload: bytes) -> None:
    if not isinstance(payload_version, int) or isinstance(payload_version, bool):
        raise TypeError("payload_version must be an integer")
    if not 1 <= payload_version <= MAX_LIFECYCLE_PAYLOAD_VERSION:
        raise ValueError(f"payload_version must be between 1 and {MAX_LIFECYCLE_PAYLOAD_VERSION}")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValueError(f"payload must contain at most {MAX_LIFECYCLE_PAYLOAD_BYTES} bytes")


def _validate_ownership(ownership: OperationOwnership) -> None:
    if not isinstance(ownership.scope, OperationScope):
        raise TypeError("scope must use OperationScope")
    if (
        not isinstance(ownership.operation_id, str)
        or len(ownership.operation_id) != 32
        or _IDENTIFIER.fullmatch(ownership.operation_id) is None
    ):
        raise ValueError("operation_id must be a 32-character lowercase hexadecimal identifier")
    _validate_generation_id(ownership.generation_id)


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
        self._connection = database._operation_connection_for_repository()  # noqa: SLF001
        self._connection_lock = database._operation_lock  # noqa: SLF001

    def claim(self, scope: OperationScope, operation_kind: str) -> OperationOwnership:
        """Reserve one unclaimed scope and return its fresh stale-owner fence."""
        _validate_operation_kind(operation_kind)
        ownership = OperationOwnership(scope, uuid4().hex, uuid4().hex)
        now = _utc_now()

        with self._standalone_transaction():
            self._connection.execute(
                "INSERT INTO operation_owners "
                "(operation_id, generation_id, operation_kind, state, claimed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ownership.operation_id,
                    ownership.generation_id,
                    operation_kind,
                    OperationClaimState.RESERVED,
                    now,
                    now,
                ),
            )
            cursor = self._connection.execute(
                "INSERT INTO operation_claims (resource_kind, resource_name, operation_id) VALUES (?, ?, ?) "
                "ON CONFLICT(resource_kind, resource_name) DO NOTHING",
                (
                    scope.resource_kind,
                    scope.resource_name,
                    ownership.operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    "another operation already owns this resource",
                    entity_kind=scope.resource_kind,
                    entity_name=scope.resource_name,
                )
        return ownership

    def recover_takeover(
        self,
        predecessor: OperationOwnership,
        generation_id: str,
    ) -> OperationClaim:
        """Atomically fence a predecessor and seal its stable obligation ledger.

        The caller retains ``generation_id`` so an interrupted committed
        request can be retried exactly. This transition coordinates database
        submissions only; it neither observes nor proves remote quiescence.
        """
        _validate_generation_id(generation_id)
        if generation_id == predecessor.generation_id:
            raise ValueError("recovery generation_id must differ from the predecessor generation_id")
        now = _utc_now()
        with self._standalone_transaction():
            row = self._connection.execute(
                _CLAIM_SELECT + " WHERE claims.resource_kind = ? AND claims.resource_name = ?",
                (predecessor.scope.resource_kind, predecessor.scope.resource_name),
            ).fetchone()
            current = None if row is None else self._decode_claim(row)
            requested = OperationOwnership(predecessor.scope, predecessor.operation_id, generation_id)
            if (
                current is not None
                and current.ownership == requested
                and row["recovery_predecessor_generation_id"] == predecessor.generation_id
            ):
                return current
            if current is None or current.ownership != predecessor:
                raise StateError(
                    "operation ownership is stale",
                    entity_kind=predecessor.scope.resource_kind,
                    entity_name=predecessor.scope.resource_name,
                )
            cursor = self._connection.execute(
                "UPDATE operation_owners SET generation_id = ?, recovery_predecessor_generation_id = ?, "
                "obligations_sealed_at = COALESCE(obligations_sealed_at, ?), "
                "updated_at = ? WHERE operation_id = ? AND generation_id = ?",
                (
                    generation_id,
                    predecessor.generation_id,
                    now,
                    now,
                    predecessor.operation_id,
                    predecessor.generation_id,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_stale_or_invalid_state(predecessor, None)
            return self._require_owned_claim(requested)

    def inspect(self, scope: OperationScope) -> OperationClaim | None:
        """Return bounded non-secret persisted facts for one resource claim."""
        with self._connection_lock:
            row = self._connection.execute(
                _CLAIM_SELECT + " WHERE claims.resource_kind = ? AND claims.resource_name = ?",
                (scope.resource_kind, scope.resource_name),
            ).fetchone()
            return None if row is None else self._decode_claim(row)

    def list_lifecycle_obligations(self, ownership: OperationOwnership) -> tuple[LifecycleObligation, ...]:
        """Return every bounded obligation for one exact currently owned operation."""
        with self._connection_lock:
            claim = self._inspect_owned(ownership)
            if claim is None:
                self._raise_stale_or_invalid_state(ownership, None)
            rows = self._connection.execute(
                "SELECT obligations.* FROM lifecycle_obligations AS obligations "
                "JOIN operation_owners AS owners ON owners.operation_id = obligations.operation_id "
                "WHERE obligations.operation_id = ? AND owners.generation_id = ? "
                "ORDER BY obligations.registered_at, obligations.obligation_id",
                (ownership.operation_id, ownership.generation_id),
            ).fetchall()
            return tuple(self._decode_obligation(row, ownership) for row in rows)

    def rebind_lifecycle_obligation(
        self,
        ownership: OperationOwnership,
        obligation_id: str,
        obligation_kind: str,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        """Return an existing exact adapter obligation without opening the ledger."""
        _validate_obligation_id(obligation_id)
        _validate_operation_kind(obligation_kind)
        _validate_payload(payload_version, payload)
        with self._connection_lock:
            self._require_owned_claim(ownership)
            recovery = self._connection.execute(
                "SELECT obligations_sealed_at, recovery_predecessor_generation_id "
                "FROM operation_owners WHERE operation_id = ? AND generation_id = ?",
                (ownership.operation_id, ownership.generation_id),
            ).fetchone()
            if recovery is None:
                self._raise_stale_or_invalid_state(ownership, None)
            assert recovery is not None
            if recovery["obligations_sealed_at"] is None or recovery["recovery_predecessor_generation_id"] is None:
                raise StateError(
                    "lifecycle obligations can only be rebound by sealed recovery ownership",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            obligation = self._load_obligation(ownership, obligation_id)
            if (
                obligation.obligation_kind != obligation_kind
                or obligation.payload_version != payload_version
                or obligation.payload != payload
            ):
                raise StateError(
                    "lifecycle obligation recovery binding conflicts with persisted obligation",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            return obligation

    def register_lifecycle_obligation(
        self,
        ownership: OperationOwnership,
        obligation_kind: str,
        payload_version: int,
        payload: bytes,
        *,
        obligation_id: str | None = None,
    ) -> LifecycleObligation:
        """Register one effect before admission, refusing a sealed operation.

        A caller that retains ``obligation_id`` can safely retry registration
        after an interrupted commit. That retry succeeds only when every
        persisted registration field is identical.
        """
        _validate_operation_kind(obligation_kind)
        _validate_payload(payload_version, payload)
        if obligation_id is None:
            obligation_id = uuid4().hex
        else:
            _validate_obligation_id(obligation_id)
        now = _utc_now()
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            existing = self._connection.execute(
                "SELECT * FROM lifecycle_obligations WHERE operation_id = ? AND obligation_id = ?",
                (ownership.operation_id, obligation_id),
            ).fetchone()
            if existing is not None:
                obligation = self._decode_obligation(existing, ownership)
                if (
                    obligation.obligation_kind == obligation_kind
                    and obligation.payload_version == payload_version
                    and obligation.payload == payload
                ):
                    return obligation
                raise StateError(
                    "lifecycle obligation registration conflicts with an existing obligation",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            if claim.obligations_sealed_at is not None:
                raise StateError(
                    "operation lifecycle obligations are sealed",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            cursor = self._connection.execute(
                "INSERT INTO lifecycle_obligations "
                "(operation_id, obligation_id, obligation_kind, state, payload_version, payload, payload_revision, "
                "registered_at, updated_at) "
                "SELECT ?, ?, ?, ?, ?, ?, 0, ?, ? "
                "WHERE (SELECT COUNT(*) FROM lifecycle_obligations WHERE operation_id = ?) < ?",
                (
                    ownership.operation_id,
                    obligation_id,
                    obligation_kind,
                    LifecycleObligationState.REGISTERED,
                    payload_version,
                    payload,
                    now,
                    now,
                    ownership.operation_id,
                    MAX_LIFECYCLE_OBLIGATIONS,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    "operation lifecycle obligation limit is reached",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            return self._load_obligation(ownership, obligation_id)

    def mark_lifecycle_obligation_possible_effect(
        self,
        ownership: OperationOwnership,
        obligation_id: str,
    ) -> LifecycleObligation:
        """Record admission before an effect and atomically arm its claim."""
        _validate_obligation_id(obligation_id)
        now = _utc_now()
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            obligation = self._load_obligation(ownership, obligation_id)
            if obligation.state is LifecycleObligationState.RESOLVED:
                raise StateError(
                    "lifecycle obligation is already resolved",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            if obligation.state is LifecycleObligationState.REGISTERED:
                cursor = self._connection.execute(
                    "UPDATE lifecycle_obligations SET state = ?, updated_at = ? "
                    "WHERE operation_id = ? AND obligation_id = ? AND state = ? "
                    "AND EXISTS (SELECT 1 FROM operation_owners WHERE operation_id = ? AND generation_id = ?)",
                    (
                        LifecycleObligationState.POSSIBLE_EFFECT,
                        now,
                        ownership.operation_id,
                        obligation_id,
                        LifecycleObligationState.REGISTERED,
                        ownership.operation_id,
                        ownership.generation_id,
                    ),
                )
                assert cursor.rowcount == 1
            if claim.state is OperationClaimState.RESERVED:
                cursor = self._connection.execute(
                    "UPDATE operation_owners SET state = ?, updated_at = ? "
                    "WHERE operation_id = ? AND generation_id = ? AND state = ?",
                    (
                        OperationClaimState.POSSIBLE_DISPATCH,
                        now,
                        ownership.operation_id,
                        ownership.generation_id,
                        OperationClaimState.RESERVED,
                    ),
                )
                if cursor.rowcount != 1:
                    self._raise_stale_or_invalid_state(ownership, OperationClaimState.RESERVED)
            elif claim.state is not OperationClaimState.POSSIBLE_DISPATCH:
                self._raise_stale_or_invalid_state(ownership, OperationClaimState.POSSIBLE_DISPATCH)
            return self._load_obligation(ownership, obligation_id)

    def publish_lifecycle_obligation_payload(
        self,
        ownership: OperationOwnership,
        obligation_id: str,
        *,
        expected_revision: int,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        """CAS-replace identity payload while the obligation is unresolved.

        Repeating a committed publication is safe when it requests the exact
        same adapter payload. A concurrent different publication refuses
        rather than silently overwriting another adapter observation.
        """
        _validate_obligation_id(obligation_id)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        _validate_payload(payload_version, payload)
        now = _utc_now()
        with self._standalone_transaction():
            self._require_owned_claim(ownership)
            obligation = self._load_obligation(ownership, obligation_id)
            if obligation.state is LifecycleObligationState.RESOLVED:
                raise StateError(
                    "lifecycle obligation payload cannot be published after resolution",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            if obligation.payload_version == payload_version and obligation.payload == payload:
                return obligation
            cursor = self._connection.execute(
                "UPDATE lifecycle_obligations "
                "SET payload_version = ?, payload = ?, payload_revision = payload_revision + 1, updated_at = ? "
                "WHERE operation_id = ? AND obligation_id = ? AND state IN (?, ?) AND payload_revision = ? "
                "AND EXISTS (SELECT 1 FROM operation_owners WHERE operation_id = ? AND generation_id = ?)",
                (
                    payload_version,
                    payload,
                    now,
                    ownership.operation_id,
                    obligation_id,
                    LifecycleObligationState.REGISTERED,
                    LifecycleObligationState.POSSIBLE_EFFECT,
                    expected_revision,
                    ownership.operation_id,
                    ownership.generation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    "lifecycle obligation payload changed concurrently",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            return self._load_obligation(ownership, obligation_id)

    def resolve_lifecycle_obligation(
        self,
        ownership: OperationOwnership,
        obligation_id: str,
    ) -> LifecycleObligation:
        """Record adapter-established no-further-effects evidence for one row."""
        _validate_obligation_id(obligation_id)
        now = _utc_now()
        with self._standalone_transaction():
            self._require_owned_claim(ownership)
            obligation = self._load_obligation(ownership, obligation_id)
            if obligation.state is LifecycleObligationState.RESOLVED:
                return obligation
            cursor = self._connection.execute(
                "UPDATE lifecycle_obligations SET state = ?, updated_at = ? "
                "WHERE operation_id = ? AND obligation_id = ? AND state IN (?, ?) "
                "AND EXISTS (SELECT 1 FROM operation_owners WHERE operation_id = ? AND generation_id = ?)",
                (
                    LifecycleObligationState.RESOLVED,
                    now,
                    ownership.operation_id,
                    obligation_id,
                    LifecycleObligationState.REGISTERED,
                    LifecycleObligationState.POSSIBLE_EFFECT,
                    ownership.operation_id,
                    ownership.generation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    "lifecycle obligation could not be resolved",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            return self._load_obligation(ownership, obligation_id)

    def seal_lifecycle_obligations(self, ownership: OperationOwnership) -> OperationClaim:
        """Forbid further registrations once the workflow cannot create effects."""
        now = _utc_now()
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            if claim.obligations_sealed_at is None:
                cursor = self._connection.execute(
                    "UPDATE operation_owners SET obligations_sealed_at = ?, updated_at = ? "
                    "WHERE operation_id = ? AND generation_id = ? "
                    "AND obligations_sealed_at IS NULL",
                    (
                        now,
                        now,
                        ownership.operation_id,
                        ownership.generation_id,
                    ),
                )
                assert cursor.rowcount == 1
            return self._require_owned_claim(ownership)

    def record_effects_resolved(self, ownership: OperationOwnership) -> OperationClaim:
        """Record core's evidence that no remote effects can remain.

        Core must establish completion, rollback, or another operation-specific
        no-further-effects fact before calling this method. The database only
        persists that decision; it does not observe or prove remote quiescence.
        """
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            if claim.obligations_sealed_at is None:
                raise StateError(
                    "operation lifecycle obligations must be sealed before resolution",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            obligations = self.list_lifecycle_obligations(ownership)
            if any(obligation.state is not LifecycleObligationState.RESOLVED for obligation in obligations):
                raise StateError(
                    "operation lifecycle obligations require explicit resolution",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            if claim.state is OperationClaimState.RESOLVED:
                return claim
            cursor = self._connection.execute(
                "UPDATE operation_owners SET state = ?, updated_at = ? "
                "WHERE operation_id = ? AND generation_id = ? AND state = ?",
                (
                    OperationClaimState.RESOLVED,
                    _utc_now(),
                    ownership.operation_id,
                    ownership.generation_id,
                    claim.state,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_stale_or_invalid_state(ownership, claim.state)
            return self._require_owned_claim(ownership)

    def abandon_reserved(self, ownership: OperationOwnership) -> None:
        """Release a claim only while core knows dispatch was never possible."""
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            if claim.state is not OperationClaimState.RESERVED:
                self._raise_stale_or_invalid_state(ownership, OperationClaimState.RESERVED)
            if claim.obligations_sealed_at is not None or self.list_lifecycle_obligations(ownership):
                raise StateError(
                    "operation lifecycle obligations require explicit resolution",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            self._delete_in_transaction(ownership, expected=OperationClaimState.RESERVED)

    def release_resolved(self, ownership: OperationOwnership) -> None:
        """Release a claim after core explicitly recorded effects as resolved."""
        with self._standalone_transaction():
            claim = self._require_owned_claim(ownership)
            if claim.state is not OperationClaimState.RESOLVED:
                self._raise_stale_or_invalid_state(ownership, OperationClaimState.RESOLVED)
            obligations = self.list_lifecycle_obligations(ownership)
            if any(obligation.state is not LifecycleObligationState.RESOLVED for obligation in obligations):
                raise StateError(
                    "operation lifecycle obligations require explicit resolution",
                    entity_kind=ownership.scope.resource_kind,
                    entity_name=ownership.scope.resource_name,
                )
            self._delete_in_transaction(ownership, expected=OperationClaimState.RESOLVED)

    def _delete_in_transaction(self, ownership: OperationOwnership, *, expected: OperationClaimState) -> None:
        cursor = self._connection.execute(
            "DELETE FROM operation_owners WHERE operation_id = ? AND generation_id = ? AND state = ?",
            (
                ownership.operation_id,
                ownership.generation_id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            self._raise_stale_or_invalid_state(ownership, expected)

    def _inspect_owned(self, ownership: OperationOwnership) -> OperationClaim | None:
        row = self._connection.execute(
            _CLAIM_SELECT + " WHERE claims.resource_kind = ? AND claims.resource_name = ?",
            (ownership.scope.resource_kind, ownership.scope.resource_name),
        ).fetchone()
        if row is None:
            return None
        claim = self._decode_claim(row)
        return claim if claim.ownership == ownership else None

    def _require_owned_claim(self, ownership: OperationOwnership) -> OperationClaim:
        claim = self._inspect_owned(ownership)
        if claim is None:
            self._raise_stale_or_invalid_state(ownership, None)
        assert claim is not None
        return claim

    def _load_obligation(self, ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        row = self._connection.execute(
            "SELECT obligations.* FROM lifecycle_obligations AS obligations "
            "JOIN operation_owners AS owners ON owners.operation_id = obligations.operation_id "
            "WHERE obligations.operation_id = ? AND obligations.obligation_id = ? AND owners.generation_id = ?",
            (ownership.operation_id, obligation_id, ownership.generation_id),
        ).fetchone()
        if row is None:
            raise StateError(
                "lifecycle obligation is not owned by this operation",
                entity_kind=ownership.scope.resource_kind,
                entity_name=ownership.scope.resource_name,
            )
        return self._decode_obligation(row, ownership)

    def _raise_stale_or_invalid_state(
        self,
        ownership: OperationOwnership,
        expected: OperationClaimState | None,
    ) -> None:
        row = self._connection.execute(
            _CLAIM_SELECT + " WHERE claims.resource_kind = ? AND claims.resource_name = ?",
            (ownership.scope.resource_kind, ownership.scope.resource_name),
        ).fetchone()
        claim = None if row is None else self._decode_claim(row)
        if claim is None or claim.ownership != ownership:
            raise StateError(
                "operation ownership is stale",
                entity_kind=ownership.scope.resource_kind,
                entity_name=ownership.scope.resource_name,
            )
        if expected is None:
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
        with self._connection_lock:
            if self._database._read_only:  # noqa: SLF001
                raise StateError("operation claims require a writable database", entity_kind="database")
            if (  # noqa: SLF001
                self._database._tx_depth and self._database._transaction_thread_id == threading.get_ident()
            ) or self._connection.in_transaction:
                raise StateError(
                    "operation claim mutations cannot join another database transaction",
                    entity_kind="database",
                )
            try:
                # Claim and ledger transitions read before they write. Take
                # SQLite's write slot before that read so concurrent owners
                # serialize instead of one retaining a stale snapshot and
                # receiving SQLITE_BUSY_SNAPSHOT after another commits.
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
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
            ownership = OperationOwnership(scope, row["operation_id"], row["generation_id"])
            operation_kind = row["operation_kind"]
            _validate_operation_kind(operation_kind)
            state = OperationClaimState(row["state"])
            claimed_at = _decode_timestamp(row["claimed_at"])
            updated_at = _decode_timestamp(row["updated_at"])
            sealed_at = row["obligations_sealed_at"]
            if sealed_at is not None:
                sealed_at = _decode_timestamp(sealed_at)
            prior_generation = row["recovery_predecessor_generation_id"]
            if prior_generation is not None:
                _validate_generation_id(prior_generation)
                if prior_generation == ownership.generation_id:
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise StateError(
                "persisted operation claim is malformed",
                entity_kind="database",
                hint="Repair the state database and reconcile outstanding remote operations before retrying.",
            ) from None
        return OperationClaim(ownership, operation_kind, state, claimed_at, updated_at, sealed_at)

    @staticmethod
    def _decode_obligation(row: sqlite3.Row, ownership: OperationOwnership) -> LifecycleObligation:
        """Validate opaque-envelope shape without interpreting adapter payload."""
        try:
            if row["operation_id"] != ownership.operation_id:
                raise ValueError
            obligation_id = row["obligation_id"]
            _validate_obligation_id(obligation_id)
            obligation_kind = row["obligation_kind"]
            _validate_operation_kind(obligation_kind)
            state = LifecycleObligationState(row["state"])
            payload_version = row["payload_version"]
            payload = row["payload"]
            _validate_payload(payload_version, payload)
            payload_revision = row["payload_revision"]
            if not isinstance(payload_revision, int) or isinstance(payload_revision, bool) or payload_revision < 0:
                raise ValueError
            registered_at = _decode_timestamp(row["registered_at"])
            updated_at = _decode_timestamp(row["updated_at"])
        except (KeyError, TypeError, ValueError):
            raise StateError(
                "persisted lifecycle obligation is malformed",
                entity_kind="database",
                hint="Repair the state database and reconcile outstanding remote operations before retrying.",
            ) from None
        return LifecycleObligation(
            ownership,
            obligation_id,
            obligation_kind,
            state,
            payload_version,
            payload,
            payload_revision,
            registered_at,
            updated_at,
        )
