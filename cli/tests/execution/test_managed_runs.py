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
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunReceiptAbsent,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
    launch_managed_run,
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
        launch_managed_run(repository, stale, boundary)
    assert called is False
    assert repository.inspect(_RUN_ID) == reserved
    database.close()


def test_possible_dispatch_is_durable_before_boundary_and_ambiguous_launch_is_not_replayed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    calls = 0

    def boundary(record: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal calls
        calls += 1
        observed = repository.inspect(record.identity)
        assert observed is not None and observed.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
        assert observed.possible_dispatch_at is not None
        return ManagedLaunchObservation(Dispatch.UNKNOWN)

    possible = launch_managed_run(repository, reserved, boundary)
    assert possible.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        launch_managed_run(repository, reserved, boundary)
    assert calls == 1
    database.close()


def test_boundary_exception_retains_possible_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)

    def boundary(_record: ManagedRunRecord) -> ManagedLaunchObservation:
        raise RuntimeError("lost acknowledgment")

    with pytest.raises(RuntimeError):
        launch_managed_run(repository, reserved, boundary)
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

    with pytest.raises(StateError):
        launch_managed_run(repository, reserved, boundary)
    assert called is False
    assert repository.inspect(_RUN_ID) == reserved
    database.close()


def test_exact_receipt_reconciles_once_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = launch_managed_run(repository, reserved, lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN))
    observation = ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible))

    first = repository.reconcile(reserved, observation)
    second = repository.reconcile(reserved, observation)

    assert first == second
    assert first.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert first.launch_reconciled_at is not None
    database.close()


def test_not_sent_requires_exact_absence_before_closing_as_not_launched(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = launch_managed_run(repository, reserved, lambda _record: ManagedLaunchObservation(Dispatch.NOT_SENT))
    assert possible.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH

    no_effects = repository.reconcile(
        reserved,
        ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible)),
    )
    assert no_effects.launch_state is ManagedLaunchState.NOT_LAUNCHED
    assert (
        repository.reconcile(
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
    possible = launch_managed_run(
        repository,
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

    with pytest.raises(StateError):
        repository.reconcile(reserved, observation)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_absence_target_mismatch_and_receipt_version_mismatch_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = launch_managed_run(
        repository,
        reserved,
        lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN),
    )
    wrong_target = replace(possible.spec.target, incarnation=f"v1:{'b' * 64}")
    with pytest.raises(StateError):
        repository.reconcile(
            reserved,
            ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible, target=wrong_target)),
        )
    with pytest.raises(ValidationError):
        _absence(possible, receipt_protocol_version=2)
    with pytest.raises(ValidationError):
        replace(possible.spec, managed_profile_revision=2)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


@pytest.mark.parametrize("field", ["managed_profile_revision", "receipt_protocol_version"])
@pytest.mark.parametrize("value", [True, 1.0])
def test_spec_revisions_require_exact_integer_types(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        replace(_spec(), **{field: value})


def test_shell_owner_and_lifetime_receipt_mismatch_refuse_without_new_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = launch_managed_run(
        repository,
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
        with pytest.raises(StateError):
            repository.reconcile(
                reserved,
                ManagedLaunchObservation(Dispatch.UNKNOWN, _receipt(possible, spec=spec)),
            )

    with pytest.raises(ValidationError):
        replace(possible.spec, managed_profile_revision=2)
    with pytest.raises(ValidationError):
        replace(possible.spec, receipt_protocol_version=2)
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_concurrent_exact_reconciliation_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    repository, reserved = _reserve(database)
    possible = launch_managed_run(
        repository,
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


@pytest.mark.parametrize(
    "path", ["/", "/bin/../sh", "/bin//sh", "/bin/sh\n", "bin/sh", "/bin/\U0001fae8", "/bin/\u202e", "/bin/\u2028"]
)
def test_resolved_shell_identity_requires_canonical_safe_absolute_path(path: str) -> None:
    with pytest.raises(ValidationError):
        ManagedShellIdentity(Shell.SH, path)


@pytest.mark.parametrize("protocol_version", [True, 1.0])
def test_absence_protocol_version_type_confusion_cannot_reconcile(tmp_path: Path, protocol_version: int) -> None:
    database = Database(tmp_path / "state.db")
    repository, reserved = _reserve(database)
    possible = launch_managed_run(repository, reserved, lambda _record: ManagedLaunchObservation(Dispatch.UNKNOWN))
    with pytest.raises(ValidationError):
        repository.reconcile(
            reserved,
            ManagedLaunchObservation(Dispatch.NOT_SENT, _absence(possible, receipt_protocol_version=protocol_version)),
        )
    persisted = repository.inspect(_RUN_ID)
    assert persisted is not None and persisted.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    database.close()


def test_managed_run_string_boundaries_require_exact_str() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(ValidationError):
        ManagedRunIdentity(StringSubclass("1" * 32))
    for name, incarnation, boot_id in (
        (StringSubclass("vm-one"), _INCARNATION, _BOOT_ID),
        ("vm-one", StringSubclass(_INCARNATION), _BOOT_ID),
        ("vm-one", _INCARNATION, StringSubclass(_BOOT_ID)),
    ):
        with pytest.raises(ValidationError):
            ManagedTargetIdentity(ManagedTargetKind.VM, name, incarnation, boot_id)
    with pytest.raises(ValidationError):
        ManagedShellIdentity(Shell.SH, StringSubclass("/bin/sh"))
    with pytest.raises(ValidationError):
        ManagedRunOwner(ManagedRunOwnerKind.OPERATION, StringSubclass(_OPERATION_ID))
    with pytest.raises(ValidationError):
        ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, StringSubclass("session-7"))
    with pytest.raises(ValidationError):
        replace(_spec(), receipt_namespace=StringSubclass(MANAGED_RECEIPT_NAMESPACE))
    with pytest.raises(ValidationError):
        ManagedRunReceipt(_RUN_ID, StringSubclass(_RUN_ID.unit_name), _spec())
