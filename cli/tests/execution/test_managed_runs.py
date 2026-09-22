"""Durable reservation and receipt reconciliation for private managed runs."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_runs import (
    MANAGED_RECEIPT_NAMESPACE,
    MANAGED_RECEIPT_PROTOCOL_VERSION,
    ManagedApplicationState,
    ManagedCleanupState,
    ManagedDisposalState,
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunReceiptAbsent,
    ManagedRunRepository,
    ManagedRunService,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution.carrier import Dispatch
from agentworks.execution.models import Shell

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._managed_runs import ManagedRunRecord


_RUN_ID = ManagedRunIdentity("1" * 32)
_OPERATION_ID = "2" * 32
_INCARNATION = f"v1:{'a' * 64}"
_BOOT_ID = "00000000-0000-4000-8000-000000000001"


def _spec(
    *,
    target: ManagedTargetIdentity | None = None,
    workload: IdentityExpectation | None = None,
    shell: ManagedShellIdentity | None = None,
) -> ManagedRunSpec:
    return ManagedRunSpec(
        target or ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", _INCARNATION, _BOOT_ID),
        workload or IdentityExpectation(1001, 1001, (1001, 1002)),
        shell or ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/bash", login=True),
        ManagedRunOwner(ManagedRunOwnerKind.OPERATION, _OPERATION_ID),
        ManagedRunLifetime.OPERATION,
    )


def _reserve(database: Database, spec: ManagedRunSpec | None = None) -> tuple[ManagedRunRepository, ManagedRunRecord]:
    repository = ManagedRunRepository(database)
    return repository, repository.reserve(spec or _spec(), identity=_RUN_ID)


def _receipt(
    record: ManagedRunRecord,
    *,
    identity: ManagedRunIdentity | None = None,
    unit_name: str | None = None,
    spec: ManagedRunSpec | None = None,
) -> ManagedRunReceipt:
    return ManagedRunReceipt(
        identity or record.identity,
        unit_name or record.identity.unit_name,
        spec or record.spec,
    )


def _absence(
    record: ManagedRunRecord,
    *,
    identity: ManagedRunIdentity | None = None,
    unit_name: str | None = None,
    target: ManagedTargetIdentity | None = None,
    receipt_namespace: str = MANAGED_RECEIPT_NAMESPACE,
    receipt_protocol_version: int = MANAGED_RECEIPT_PROTOCOL_VERSION,
) -> ManagedRunReceiptAbsent:
    return ManagedRunReceiptAbsent(
        identity or record.identity,
        unit_name or record.identity.unit_name,
        target or record.spec.target,
        receipt_namespace,
        receipt_protocol_version,
    )


def test_reservation_persists_exact_nonsecret_identity_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    _, reserved = _reserve(database)
    database.close()

    reopened = Database(path)
    persisted = ManagedRunRepository(reopened).inspect(_RUN_ID)
    reopened.close()

    assert persisted == reserved
    assert persisted is not None
    assert persisted.identity.unit_name == f"agw-managed-{_RUN_ID.run_id}.service"
    assert persisted.spec.shell == ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/bash", login=True)
    assert persisted.launch_state is ManagedLaunchState.RESERVED
    assert persisted.application_state is ManagedApplicationState.UNOBSERVED
    assert persisted.cleanup_state is ManagedCleanupState.UNOBSERVED
    assert persisted.disposal_state is ManagedDisposalState.RETAINED


def test_malformed_persisted_row_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    _reserve(database)
    database.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute("UPDATE execution_runs SET workload_groups = '01001,1002'")
    connection.commit()
    connection.close()

    reopened = Database(path)
    with pytest.raises(StateError):
        ManagedRunRepository(reopened).inspect(_RUN_ID)
    reopened.close()


def test_missing_persisted_column_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, _reserved = _reserve(database)
    row = database._conn.execute("SELECT run_id FROM execution_runs").fetchone()
    assert row is not None

    with pytest.raises(StateError):
        repository._decode_record(row)
    database.close()


def test_duplicate_and_stale_reservations_refuse_before_launch(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    with pytest.raises(StateError):
        repository.reserve(_spec(), identity=_RUN_ID)

    called = False

    def boundary(_record: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal called
        called = True
        return ManagedLaunchObservation(Dispatch.UNKNOWN)

    stale_target = replace(reserved.spec.target, boot_id="00000000-0000-4000-8000-000000000002")
    stale = replace(reserved, spec=replace(reserved.spec, target=stale_target))
    with pytest.raises(StateError):
        ManagedRunService(repository).launch(stale, boundary)
    assert called is False
    assert repository.inspect(_RUN_ID) == reserved
    database.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("application_state", ManagedApplicationState.COMPLETED.value),
        ("cleanup_state", ManagedCleanupState.COMPLETE.value),
        ("disposal_state", ManagedDisposalState.DISPOSED.value),
    ],
)
def test_unproduced_lifecycle_evidence_fails_closed_before_launch(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    database._conn.execute("PRAGMA ignore_check_constraints = ON")
    database._conn.execute(f"UPDATE execution_runs SET {column} = ?", (value,))
    database._conn.commit()
    called = False

    def boundary(_record: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal called
        called = True
        return ManagedLaunchObservation(Dispatch.SENT)

    with pytest.raises(StateError):
        ManagedRunService(repository).launch(reserved, boundary)
    assert called is False
    database.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("application_state", ManagedApplicationState.STARTED.value),
        ("cleanup_state", ManagedCleanupState.REQUIRED.value),
        ("disposal_state", ManagedDisposalState.DISPOSED.value),
    ],
)
def test_unproduced_lifecycle_evidence_fails_closed_during_reconciliation(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = ManagedRunService(repository).launch(
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    database._conn.execute("PRAGMA ignore_check_constraints = ON")
    database._conn.execute(f"UPDATE execution_runs SET {column} = ?", (value,))
    database._conn.commit()

    with pytest.raises(StateError):
        repository.reconcile(
            possible,
            ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible)),
        )
    database.close()


def test_possible_dispatch_is_durable_before_boundary_and_ambiguous_launch_is_not_replayed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    service = ManagedRunService(repository)
    calls = 0

    def boundary(record: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal calls
        calls += 1
        observed = repository.inspect(record.identity)
        assert observed is not None and observed.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
        assert observed.possible_dispatch_at is not None
        return ManagedLaunchObservation(Dispatch.UNKNOWN)

    possible = service.launch(reserved, boundary)
    assert possible.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    with pytest.raises(StateError, match="must not be replayed"):
        service.launch(reserved, boundary)
    assert calls == 1
    database.close()


def test_boundary_exception_retains_possible_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)

    def boundary(_record: ManagedRunRecord) -> ManagedLaunchObservation:
        raise RuntimeError("lost acknowledgment")

    with pytest.raises(RuntimeError, match="lost acknowledgment"):
        ManagedRunService(repository).launch(reserved, boundary)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_database_transition_failure_prevents_boundary_invocation(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    database._conn.execute(
        "CREATE TRIGGER refuse_execution_run_update BEFORE UPDATE ON execution_runs "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    database._conn.commit()
    called = False

    def boundary(_record: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal called
        called = True
        return ManagedLaunchObservation(Dispatch.SENT)

    with pytest.raises(StateError, match="unavailable or malformed"):
        ManagedRunService(repository).launch(reserved, boundary)
    assert called is False
    assert repository.inspect(_RUN_ID) == reserved
    database.close()


def test_exact_receipt_reconciles_once_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    service = ManagedRunService(repository)
    possible = service.launch(reserved, lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN))
    observation = ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible))

    first = service.reconcile(reserved, observation)
    second = service.reconcile(reserved, observation)

    assert first == second
    assert first.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert first.launch_reconciled_at is not None
    assert first.application_state is ManagedApplicationState.UNOBSERVED
    assert first.cleanup_state is ManagedCleanupState.UNOBSERVED
    assert first.disposal_state is ManagedDisposalState.RETAINED
    database.close()


def test_not_sent_requires_exact_absence_before_closing_as_not_launched(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    service = ManagedRunService(repository)
    possible = service.launch(reserved, lambda _record: ManagedLaunchObservation(Dispatch.NOT_SENT))
    assert possible.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH

    no_effects = service.reconcile(
        reserved,
        ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible)),
    )
    assert no_effects.launch_state is ManagedLaunchState.NOT_LAUNCHED
    assert (
        service.reconcile(
            reserved,
            ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible)),
        )
        == no_effects
    )
    database.close()


@pytest.mark.parametrize("mismatch", ["target", "boot", "unit", "workload"])
def test_receipt_mismatch_refuses_without_changing_possible_dispatch(tmp_path: Path, mismatch: str) -> None:
    database = Database(tmp_path / f"{mismatch}.db")
    repository, reserved = _reserve(database)
    possible = ManagedRunService(repository).launch(
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    spec = possible.spec
    unit = possible.identity.unit_name
    if mismatch == "target":
        spec = replace(spec, target=replace(spec.target, name="vm-two"))
    elif mismatch == "boot":
        spec = replace(spec, target=replace(spec.target, boot_id="00000000-0000-4000-8000-000000000002"))
    elif mismatch == "unit":
        unit = ManagedRunIdentity("f" * 32).unit_name
    else:
        spec = replace(spec, workload=IdentityExpectation(1002, 1001, (1001, 1002)))
    observation = ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible, unit_name=unit, spec=spec))

    with pytest.raises(StateError, match="does not match"):
        ManagedRunService(repository).reconcile(reserved, observation)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_absence_target_mismatch_and_receipt_version_mismatch_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = ManagedRunService(repository).launch(
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    wrong_target = replace(possible.spec.target, incarnation=f"v1:{'b' * 64}")
    with pytest.raises(StateError, match="absence does not match"):
        repository.reconcile(
            reserved,
            ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible, target=wrong_target)),
        )
    with pytest.raises(ValidationError, match="protocol"):
        _absence(possible, receipt_protocol_version=2)
    with pytest.raises(ValidationError, match="profile revision"):
        replace(possible.spec, managed_profile_revision=2)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_shell_owner_and_lifetime_receipt_mismatch_refuse_without_new_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = ManagedRunService(repository).launch(
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    mismatches = (
        replace(possible.spec, shell=ManagedShellIdentity(Shell.SH, "/bin/sh")),
        replace(
            possible.spec,
            owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "3" * 32),
        ),
        replace(
            possible.spec,
            owner=ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
            lifetime=ManagedRunLifetime.INDEPENDENT,
        ),
    )
    for spec in mismatches:
        with pytest.raises(StateError, match="does not match"):
            repository.reconcile(
                reserved,
                ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible, spec=spec)),
            )

    with pytest.raises(ValidationError, match="profile revision"):
        replace(possible.spec, managed_profile_revision=2)
    with pytest.raises(ValidationError, match="protocol"):
        replace(possible.spec, receipt_protocol_version=2)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_concurrent_exact_reconciliation_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    repository, reserved = _reserve(database)
    possible = ManagedRunService(repository).launch(
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    observation = ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible))
    database.close()
    barrier = Barrier(2)

    def reconcile() -> ManagedLaunchState:
        concurrent = Database(path)
        try:
            barrier.wait(timeout=5)
            result = ManagedRunRepository(concurrent).reconcile(reserved, observation)
            return result.launch_state
        finally:
            concurrent.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = tuple(pool.map(lambda _index: reconcile(), range(2)))
    assert states == (ManagedLaunchState.RECEIPT_CONFIRMED,) * 2

    check = Database(path)
    persisted = ManagedRunRepository(check).inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    check.close()


@pytest.mark.parametrize("path", ["/", "/bin/../sh", "/bin//sh", "/bin/sh\n", "bin/sh"])
def test_resolved_shell_identity_requires_canonical_safe_absolute_path(path: str) -> None:
    with pytest.raises(ValidationError, match="shell executable"):
        ManagedShellIdentity(Shell.SH, path)
