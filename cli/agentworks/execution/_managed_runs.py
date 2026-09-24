"""Private durable reservation and receipt reconciliation for managed runs.

This module records launch identity and evidence only. It does not launch,
observe, stop, retain output for, or dispose of a workload by itself.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Never, Protocol, cast
from uuid import UUID

from agentworks.errors import BusyStateError, StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_wire import canonical_shell_path
from agentworks.execution.carrier import Dispatch
from agentworks.execution.models import Shell

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db.database import Database


MANAGED_PROFILE_REVISION = 1
MANAGED_RECEIPT_PROTOCOL_VERSION = 1
MANAGED_RECEIPT_NAMESPACE = "agentworks-managed-runs-v1"

_UNIT_PREFIX = "agw-managed-"
_MAX_ID = 2**32 - 1
_MAX_GROUPS = 65_536
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_INCARNATION = re.compile(r"v1:[0-9a-f]{64}\Z")
_SAFE_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]*\Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ManagedRunLifetime(StrEnum):
    """Ownership duration selected before a managed launch."""

    OPERATION = "operation"
    INDEPENDENT = "independent"


class ManagedRunOwnerKind(StrEnum):
    """Closed owner identities understood by this persistence slice."""

    OPERATION = "operation"
    RESOURCE = "resource"


class ManagedTargetKind(StrEnum):
    """Core-selected target resource kinds, independent from route aliases."""

    VM = "vm"
    PLATFORM_HOST = "platform-host"


class ManagedLaunchState(StrEnum):
    """The launch evidence persisted by this private checkpoint."""

    RESERVED = "reserved"
    POSSIBLE_DISPATCH = "possible-dispatch"
    RECEIPT_CONFIRMED = "receipt-confirmed"
    NOT_LAUNCHED = "not-launched"


@dataclass(frozen=True, slots=True)
class ManagedRunIdentity:
    """Canonical non-secret identity for one managed run."""

    run_id: str

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or _RUN_ID.fullmatch(self.run_id) is None:
            raise ValidationError("Managed run identity must be 32 lowercase hexadecimal characters")

    @classmethod
    def fresh(cls) -> ManagedRunIdentity:
        return cls(secrets.token_hex(16))

    @property
    def unit_name(self) -> str:
        return f"{_UNIT_PREFIX}{self.run_id}.service"


@dataclass(frozen=True, slots=True)
class ManagedTargetIdentity:
    """Core resource, versioned instance fingerprint, and current-boot fence.

    ``name`` is the core resource name used for binding and diagnostics, never
    authority by itself. ``incarnation`` binds a provider-owned locator and a
    core-provisioned or adopted random instance marker. ``boot_id`` fences one
    boot within that incarnation.
    """

    kind: ManagedTargetKind
    name: str
    incarnation: str
    boot_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ManagedTargetKind):
            raise ValidationError("Managed target kind is invalid")
        _validate_identity_text(self.name, "target name")
        if type(self.incarnation) is not str or _INCARNATION.fullmatch(self.incarnation) is None:
            raise ValidationError("Target incarnation must use the versioned fingerprint codec")
        if type(self.boot_id) is not str:
            raise ValidationError("Target boot identity is invalid")
        try:
            parsed_boot_id = UUID(self.boot_id)
        except ValueError:
            raise ValidationError("Target boot identity is invalid") from None
        if str(parsed_boot_id) != self.boot_id:
            raise ValidationError("Target boot identity is invalid")


@dataclass(frozen=True, slots=True)
class ManagedShellIdentity:
    """Requested shell semantics and the exact resolved executable identity."""

    requested: Shell | None
    resolved_executable: str | None
    login: bool = False
    interactive: bool = False

    def __post_init__(self) -> None:
        if self.requested is not None and not isinstance(self.requested, Shell):
            raise ValidationError("Managed shell identity requires a closed shell selection")
        if type(self.login) is not bool or type(self.interactive) is not bool:
            raise ValidationError("Managed shell startup flags must be booleans")
        if self.requested is None:
            if self.resolved_executable is not None or self.login or self.interactive:
                raise ValidationError("Literal command launch cannot carry shell startup identity")
            return
        if not canonical_shell_path(self.resolved_executable):
            raise ValidationError("Resolved shell executable must be a canonical absolute POSIX path")


@dataclass(frozen=True, slots=True)
class ManagedRunOwner:
    """Exact operation or resource that owns one managed run."""

    kind: ManagedRunOwnerKind
    owner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ManagedRunOwnerKind):
            raise ValidationError("Managed run owner kind is invalid")
        if self.kind is ManagedRunOwnerKind.OPERATION:
            if type(self.owner_id) is not str or _RUN_ID.fullmatch(self.owner_id) is None:
                raise ValidationError("Operation-owned managed runs require an exact operation identity")
        else:
            _validate_identity_text(self.owner_id, "managed run resource owner")


@dataclass(frozen=True, slots=True)
class ManagedRunSpec:
    """Bounded non-secret facts that must match every launch receipt."""

    target: ManagedTargetIdentity
    workload: IdentityExpectation
    shell: ManagedShellIdentity
    owner: ManagedRunOwner
    lifetime: ManagedRunLifetime
    managed_profile_revision: int = MANAGED_PROFILE_REVISION
    receipt_namespace: str = MANAGED_RECEIPT_NAMESPACE
    receipt_protocol_version: int = MANAGED_RECEIPT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.target, ManagedTargetIdentity):
            raise ValidationError("Managed run requires an exact target identity")
        _validate_workload_identity(self.workload)
        if not isinstance(self.shell, ManagedShellIdentity):
            raise ValidationError("Managed run requires an exact shell identity")
        if not isinstance(self.owner, ManagedRunOwner) or not isinstance(self.lifetime, ManagedRunLifetime):
            raise ValidationError("Managed run requires an exact owner and lifetime")
        expected_owner = (
            ManagedRunOwnerKind.OPERATION
            if self.lifetime is ManagedRunLifetime.OPERATION
            else ManagedRunOwnerKind.RESOURCE
        )
        if self.owner.kind is not expected_owner:
            raise ValidationError("Managed run lifetime does not match its owner kind")
        if type(self.managed_profile_revision) is not int or self.managed_profile_revision != MANAGED_PROFILE_REVISION:
            raise ValidationError("Managed run profile revision is unsupported")
        if type(self.receipt_namespace) is not str or self.receipt_namespace != MANAGED_RECEIPT_NAMESPACE:
            raise ValidationError("Managed run receipt namespace is unsupported")
        if (
            type(self.receipt_protocol_version) is not int
            or self.receipt_protocol_version != MANAGED_RECEIPT_PROTOCOL_VERSION
        ):
            raise ValidationError("Managed run receipt protocol is unsupported")


@dataclass(frozen=True, slots=True)
class ManagedRunRecord:
    """One persisted managed-run reservation and launch evidence."""

    identity: ManagedRunIdentity
    spec: ManagedRunSpec
    launch_state: ManagedLaunchState
    created_at: str
    updated_at: str
    possible_dispatch_at: str | None
    launch_reconciled_at: str | None


@dataclass(frozen=True, slots=True)
class ManagedRunReceipt:
    """Exact target receipt for a realized managed run boundary."""

    identity: ManagedRunIdentity
    unit_name: str
    spec: ManagedRunSpec

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ManagedRunIdentity) or not _is_managed_unit_name(self.unit_name):
            raise ValidationError("Managed run receipt identity is invalid")
        if not isinstance(self.spec, ManagedRunSpec):
            raise ValidationError("Managed run receipt facts are invalid")


@dataclass(frozen=True, slots=True)
class ManagedRunReceiptAbsent:
    """Exact absence observation from the bound protected receipt namespace."""

    identity: ManagedRunIdentity
    unit_name: str
    target: ManagedTargetIdentity
    receipt_namespace: str
    receipt_protocol_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ManagedRunIdentity) or not _is_managed_unit_name(self.unit_name):
            raise ValidationError("Managed receipt absence identity is invalid")
        if not isinstance(self.target, ManagedTargetIdentity):
            raise ValidationError("Managed receipt absence requires an exact target identity")
        if type(self.receipt_namespace) is not str or self.receipt_namespace != MANAGED_RECEIPT_NAMESPACE:
            raise ValidationError("Managed receipt absence namespace is unsupported")
        if self.receipt_protocol_version != MANAGED_RECEIPT_PROTOCOL_VERSION:
            raise ValidationError("Managed receipt absence protocol is unsupported")


@dataclass(frozen=True, slots=True)
class ManagedLaunchObservation:
    """One launch-boundary result with optional exact receipt evidence."""

    dispatch: Dispatch
    receipt: ManagedRunReceipt | ManagedRunReceiptAbsent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch, Dispatch) or (
            self.receipt is not None and not isinstance(self.receipt, ManagedRunReceipt | ManagedRunReceiptAbsent)
        ):
            raise ValidationError("Managed launch observation is invalid")


class ManagedLaunchBoundary(Protocol):
    """One-shot launch boundary supplied after durable possible-dispatch."""

    def __call__(self, run: ManagedRunRecord) -> ManagedLaunchObservation: ...


class ManagedRunRepository:
    """Fail-closed persistence for one managed-run launch state machine."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._connection = database._conn  # noqa: SLF001

    def reserve(
        self,
        spec: ManagedRunSpec,
        *,
        identity: ManagedRunIdentity | None = None,
    ) -> ManagedRunRecord:
        """Reserve a fresh run before any launch can be attempted."""
        if not isinstance(spec, ManagedRunSpec):
            raise ValidationError("Managed run reservation requires a validated specification")
        identity = identity or ManagedRunIdentity.fresh()
        if not isinstance(identity, ManagedRunIdentity):
            raise ValidationError("Managed run reservation requires a canonical identity")
        now = _utc_now()
        with self._write_transaction():
            if self._select(identity) is not None:
                raise StateError("managed run identity is already reserved", entity_kind="execution-run")
            self._connection.execute(
                "INSERT INTO execution_runs ("
                "run_id, target_kind, target_name, target_incarnation, target_boot_id, "
                "workload_euid, workload_egid, workload_groups, requested_shell, resolved_shell, "
                "shell_login, shell_interactive, managed_profile_revision, owner_kind, owner_id, lifetime, "
                "receipt_namespace, receipt_protocol_version, launch_state, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _insert_values(identity, spec, now),
            )
            return self._require_record(identity)

    def inspect(self, identity: ManagedRunIdentity) -> ManagedRunRecord | None:
        """Read and validate one persisted run without changing it."""
        if not isinstance(identity, ManagedRunIdentity):
            raise ValidationError("Managed run inspection requires a canonical identity")
        try:
            row = self._select(identity)
            return None if row is None else self._decode_record(row)
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error)

    def mark_possible_dispatch(self, expected: ManagedRunRecord) -> ManagedRunRecord:
        """Commit possible dispatch before the launch boundary is called."""
        _validate_expected_record(expected)
        now = _utc_now()
        with self._write_transaction():
            current = self._require_record(expected.identity)
            self._require_same_spec(expected, current)
            if current.launch_state is not ManagedLaunchState.RESERVED:
                raise StateError(
                    "managed run has already left reservation; launch must not be replayed",
                    entity_kind="execution-run",
                )
            self._connection.execute(
                "UPDATE execution_runs SET launch_state = ?, updated_at = ?, possible_dispatch_at = ? "
                "WHERE run_id = ? AND launch_state = ?",
                (
                    ManagedLaunchState.POSSIBLE_DISPATCH,
                    now,
                    now,
                    expected.identity.run_id,
                    ManagedLaunchState.RESERVED,
                ),
            )
            return self._require_record(expected.identity)

    def reconcile(
        self,
        expected: ManagedRunRecord,
        observation: ManagedLaunchObservation,
    ) -> ManagedRunRecord:
        """Reconcile exact receipt evidence without dispatching or stopping work."""
        _validate_expected_record(expected)
        if not isinstance(observation, ManagedLaunchObservation):
            raise ValidationError("Managed run reconciliation requires a validated launch observation")
        with self._write_transaction():
            current = self._require_record(expected.identity)
            self._require_same_spec(expected, current)
            target = self._reconciled_launch_state(current, observation)
            if target is None or target is current.launch_state:
                return current
            now = _utc_now()
            self._connection.execute(
                "UPDATE execution_runs SET launch_state = ?, updated_at = ?, launch_reconciled_at = ? "
                "WHERE run_id = ? AND launch_state = ?",
                (target, now, now, current.identity.run_id, ManagedLaunchState.POSSIBLE_DISPATCH),
            )
            return self._require_record(current.identity)

    def _reconciled_launch_state(
        self,
        current: ManagedRunRecord,
        observation: ManagedLaunchObservation,
    ) -> ManagedLaunchState | None:
        receipt = observation.receipt
        target: ManagedLaunchState | None = None
        if isinstance(receipt, ManagedRunReceipt):
            if not _receipt_matches(current, receipt):
                raise StateError("managed run receipt does not match its reservation", entity_kind="execution-run")
            if observation.dispatch is Dispatch.NOT_SENT:
                raise StateError("managed launch evidence is contradictory", entity_kind="execution-run")
            target = ManagedLaunchState.RECEIPT_CONFIRMED
        elif isinstance(receipt, ManagedRunReceiptAbsent):
            if not _absence_matches(current, receipt):
                raise StateError(
                    "managed receipt absence does not match its reservation",
                    entity_kind="execution-run",
                )
            if observation.dispatch is Dispatch.NOT_SENT:
                target = ManagedLaunchState.NOT_LAUNCHED

        if current.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH:
            return target
        if current.launch_state is target and target in {
            ManagedLaunchState.RECEIPT_CONFIRMED,
            ManagedLaunchState.NOT_LAUNCHED,
        }:
            return current.launch_state
        if current.launch_state is ManagedLaunchState.RESERVED:
            raise StateError("managed run was never marked for possible dispatch", entity_kind="execution-run")
        if target is None:
            raise StateError("managed run launch is already reconciled", entity_kind="execution-run")
        raise StateError("managed run launch evidence conflicts with prior reconciliation", entity_kind="execution-run")

    @staticmethod
    def _require_same_spec(expected: ManagedRunRecord, current: ManagedRunRecord) -> None:
        if expected.identity != current.identity or expected.spec != current.spec:
            raise StateError("managed run reservation identity is stale", entity_kind="execution-run")

    def _select(self, identity: ManagedRunIdentity) -> sqlite3.Row | None:
        row = self._connection.execute(
            "SELECT * FROM execution_runs WHERE run_id = ?",
            (identity.run_id,),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def _require_record(self, identity: ManagedRunIdentity) -> ManagedRunRecord:
        row = self._select(identity)
        if row is None:
            raise StateError("managed run reservation is missing", entity_kind="execution-run")
        return self._decode_record(row)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        if self._database._read_only:  # noqa: SLF001
            raise StateError("managed runs require a writable database", entity_kind="database")
        if self._database._tx_depth or self._connection.in_transaction:  # noqa: SLF001
            raise StateError(
                "managed run mutations cannot join another database transaction",
                entity_kind="database",
            )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error)

    @staticmethod
    def _raise_database_error(error: sqlite3.DatabaseError) -> Never:
        from agentworks.db.backup import _is_busy

        if _is_busy(error):
            raise BusyStateError() from error
        raise StateError(
            "managed run state is unavailable or malformed",
            entity_kind="database",
            hint="Repair the state database and reconcile outstanding managed work before retrying.",
        ) from error

    @staticmethod
    def _decode_record(row: sqlite3.Row) -> ManagedRunRecord:
        """Validate every persisted fact before lifecycle code may use it."""
        try:
            identity = ManagedRunIdentity(row["run_id"])
            requested_value = row["requested_shell"]
            requested = None if requested_value == "none" else Shell(requested_value)
            login, interactive = row["shell_login"], row["shell_interactive"]
            if login not in (0, 1) or interactive not in (0, 1):
                raise ValueError
            shell = ManagedShellIdentity(requested, row["resolved_shell"], bool(login), bool(interactive))
            workload = IdentityExpectation(
                row["workload_euid"],
                row["workload_egid"],
                _decode_groups(row["workload_groups"]),
            )
            spec = ManagedRunSpec(
                ManagedTargetIdentity(
                    ManagedTargetKind(row["target_kind"]),
                    row["target_name"],
                    row["target_incarnation"],
                    row["target_boot_id"],
                ),
                workload,
                shell,
                ManagedRunOwner(ManagedRunOwnerKind(row["owner_kind"]), row["owner_id"]),
                ManagedRunLifetime(row["lifetime"]),
                row["managed_profile_revision"],
                row["receipt_namespace"],
                row["receipt_protocol_version"],
            )
            launch_state = ManagedLaunchState(row["launch_state"])
            created_at = _decode_timestamp(row["created_at"])
            updated_at = _decode_timestamp(row["updated_at"])
            possible_at = _decode_optional_timestamp(row["possible_dispatch_at"])
            reconciled_at = _decode_optional_timestamp(row["launch_reconciled_at"])
            _validate_state_timestamps(launch_state, possible_at, reconciled_at)
        except (IndexError, KeyError, TypeError, ValueError, ValidationError):
            raise StateError(
                "persisted managed run is malformed",
                entity_kind="database",
                hint="Repair the state database and reconcile outstanding managed work before retrying.",
            ) from None
        return ManagedRunRecord(
            identity,
            spec,
            launch_state,
            created_at,
            updated_at,
            possible_at,
            reconciled_at,
        )


def launch_managed_run(
    repository: ManagedRunRepository,
    reserved: ManagedRunRecord,
    boundary: ManagedLaunchBoundary,
) -> ManagedRunRecord:
    """Dispatch exactly once after possible dispatch is durable."""
    if not isinstance(repository, ManagedRunRepository):
        raise ValidationError("Managed launch requires its private repository")
    if not callable(boundary):
        raise ValidationError("Managed launch requires a callable boundary")
    possible = repository.mark_possible_dispatch(reserved)
    observation = boundary(possible)
    if not isinstance(observation, ManagedLaunchObservation):
        raise ValidationError("Managed launch boundary returned an invalid observation")
    return repository.reconcile(possible, observation)


def _insert_values(identity: ManagedRunIdentity, spec: ManagedRunSpec, now: str) -> tuple[object, ...]:
    requested_shell = "none" if spec.shell.requested is None else spec.shell.requested.value
    return (
        identity.run_id,
        spec.target.kind,
        spec.target.name,
        spec.target.incarnation,
        spec.target.boot_id,
        spec.workload.euid,
        spec.workload.egid,
        _encode_groups(spec.workload),
        requested_shell,
        spec.shell.resolved_executable,
        int(spec.shell.login),
        int(spec.shell.interactive),
        spec.managed_profile_revision,
        spec.owner.kind,
        spec.owner.owner_id,
        spec.lifetime,
        spec.receipt_namespace,
        spec.receipt_protocol_version,
        ManagedLaunchState.RESERVED,
        now,
        now,
    )


def _validate_expected_record(record: object) -> None:
    if not isinstance(record, ManagedRunRecord):
        raise ValidationError("Managed run state transition requires a persisted record")


def _validate_identity_text(value: object, label: str) -> None:
    if type(value) is not str or _SAFE_IDENTITY.fullmatch(value) is None or len(value.encode("utf-8")) > 255:
        raise ValidationError(f"{label.capitalize()} is invalid")


def _validate_workload_identity(identity: object) -> None:
    if not isinstance(identity, IdentityExpectation):
        raise ValidationError("Managed run requires an exact workload identity")
    if (
        type(identity.euid) is not int
        or not 0 <= identity.euid <= _MAX_ID
        or type(identity.egid) is not int
        or not 0 <= identity.egid <= _MAX_ID
        or type(identity.groups) is not tuple
        or not identity.groups
        or len(identity.groups) > _MAX_GROUPS
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in identity.groups)
        or identity.groups != tuple(sorted(set(identity.groups)))
        or identity.egid not in identity.groups
    ):
        raise ValidationError("Managed run workload identity is invalid")


def _encode_groups(identity: IdentityExpectation) -> str:
    _validate_workload_identity(identity)
    return ",".join(str(group) for group in identity.groups)


def _decode_groups(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        raise ValueError
    try:
        groups = tuple(int(item) for item in value.split(","))
    except ValueError:
        raise ValueError from None
    if ",".join(str(group) for group in groups) != value:
        raise ValueError
    return groups


def _is_managed_unit_name(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_UNIT_PREFIX)
        and value.endswith(".service")
        and _RUN_ID.fullmatch(value[len(_UNIT_PREFIX) : -len(".service")]) is not None
    )


def _receipt_matches(record: ManagedRunRecord, receipt: ManagedRunReceipt) -> bool:
    return (
        receipt.identity == record.identity
        and receipt.unit_name == record.identity.unit_name
        and receipt.spec == record.spec
    )


def _absence_matches(record: ManagedRunRecord, absence: ManagedRunReceiptAbsent) -> bool:
    return (
        absence.identity == record.identity
        and absence.unit_name == record.identity.unit_name
        and absence.target == record.spec.target
        and absence.receipt_namespace == record.spec.receipt_namespace
        and absence.receipt_protocol_version == record.spec.receipt_protocol_version
    )


def _utc_now() -> str:
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def _decode_timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) != 20:
        raise ValueError
    parsed = datetime.strptime(value, _TIMESTAMP_FORMAT)
    if parsed.strftime(_TIMESTAMP_FORMAT) != value:
        raise ValueError
    return value


def _decode_optional_timestamp(value: object) -> str | None:
    return None if value is None else _decode_timestamp(value)


def _validate_state_timestamps(
    state: ManagedLaunchState,
    possible_at: str | None,
    reconciled_at: str | None,
) -> None:
    if state is ManagedLaunchState.RESERVED:
        valid = possible_at is None and reconciled_at is None
    elif state is ManagedLaunchState.POSSIBLE_DISPATCH:
        valid = possible_at is not None and reconciled_at is None
    else:
        valid = possible_at is not None and reconciled_at is not None
    if not valid:
        raise ValueError
