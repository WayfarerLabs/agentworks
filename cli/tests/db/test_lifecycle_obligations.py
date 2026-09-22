"""Durable lifecycle-obligation ledger behavior."""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agentworks.db import (
    MAX_LIFECYCLE_OBLIGATIONS,
    Database,
    LifecycleObligationState,
    OperationClaimState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError
from agentworks.operations import OperationOwner
from tests.database_support import build_schema

pytestmark = pytest.mark.windows


def _scope(name: str = "ledger-vm") -> OperationScope:
    return OperationScope(OperationResourceKind.VM, name)


def _registered(database: Database, ownership: OperationOwnership, kind: str = "platform-hold") -> str:
    return database.operations.register_lifecycle_obligation(ownership, kind, 1, b"prepared").obligation_id


def _resolve_all(database: Database, ownership: OperationOwnership) -> None:
    for obligation in database.operations.list_lifecycle_obligations(ownership):
        database.operations.resolve_lifecycle_obligation(ownership, obligation.obligation_id)


def _concurrent_admission_worker(path: Path, ownership: OperationOwnership, start: Any, outcomes: Any) -> None:
    database = Database(path)
    try:
        assert start.wait(timeout=10)
        obligation = database.operations.register_lifecycle_obligation(
            ownership,
            "concurrent-platform-hold",
            1,
            b"prepared",
        )
        admitted = database.operations.mark_lifecycle_obligation_possible_effect(ownership, obligation.obligation_id)
        outcomes.put(("admitted", admitted.obligation_id))
    except Exception as error:  # pragma: no cover - returned for parent assertion
        outcomes.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        database.close()


@pytest.mark.parametrize("state", ["reserved", "possible-dispatch", "resolved"])
def test_migration_adds_empty_ledger_without_changing_existing_claim(
    tmp_path: Path,
    state: str,
) -> None:
    path = tmp_path / "state.db"
    build_schema(path, 41)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO operation_claims "
        "(resource_kind, resource_name, operation_id, operation_kind, state, claimed_at, updated_at) "
        "VALUES ('vm', 'preserved', 'a' || substr('00000000000000000000000000000000', 2), "
        "'vm-reinitialize', ?, '2026-09-22T00:00:00Z', '2026-09-22T00:00:01Z')",
        (state,),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        claim = database.operations.inspect(_scope("preserved"))
        assert claim is not None
        assert claim.obligations_sealed_at is None
        assert claim.state.value == state
        assert claim.claimed_at == "2026-09-22T00:00:00Z"
        assert claim.updated_at == "2026-09-22T00:00:01Z"
        assert database.operations.list_lifecycle_obligations(claim.ownership) == ()
    finally:
        database.close()


def test_multiple_same_kind_obligations_are_independent_and_bounded(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    first = _registered(db, ownership)
    second = _registered(db, ownership)

    assert first != second
    obligations = db.operations.list_lifecycle_obligations(ownership)
    assert [obligation.obligation_kind for obligation in obligations] == ["platform-hold", "platform-hold"]
    assert all(obligation.state is LifecycleObligationState.REGISTERED for obligation in obligations)

    for index in range(MAX_LIFECYCLE_OBLIGATIONS - 2):
        _registered(db, ownership, f"nested-hold-{index}")
    with pytest.raises(StateError):
        _registered(db, ownership, "one-too-many")


def test_payload_publication_uses_cas_and_idempotent_retry(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    obligation_id = _registered(db, ownership)
    possible = db.operations.mark_lifecycle_obligation_possible_effect(ownership, obligation_id)

    published = db.operations.publish_lifecycle_obligation_payload(
        ownership,
        obligation_id,
        expected_revision=possible.payload_revision,
        payload_version=2,
        payload=b"exact-identity",
    )
    assert published.payload_revision == 1

    retry = db.operations.publish_lifecycle_obligation_payload(
        ownership,
        obligation_id,
        expected_revision=0,
        payload_version=2,
        payload=b"exact-identity",
    )
    assert retry == published

    with pytest.raises(StateError):
        db.operations.publish_lifecycle_obligation_payload(
            ownership,
            obligation_id,
            expected_revision=0,
            payload_version=2,
            payload=b"different-identity",
        )


def test_first_possible_effect_atomically_arms_claim_and_registered_can_resolve(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    no_effect = _registered(db, ownership, "prepared-route")
    effect = _registered(db, ownership, "platform-hold")

    db.operations.resolve_lifecycle_obligation(ownership, no_effect)
    possible = db.operations.mark_lifecycle_obligation_possible_effect(ownership, effect)
    claim = db.operations.inspect(ownership.scope)

    assert possible.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH


def test_sealing_and_every_resolution_are_required_for_whole_operation_release(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    first = _registered(db, ownership)
    second = _registered(db, ownership)
    db.operations.mark_lifecycle_obligation_possible_effect(ownership, first)

    with pytest.raises(StateError):
        db.operations.record_effects_resolved(ownership)

    db.operations.seal_lifecycle_obligations(ownership)
    with pytest.raises(StateError):
        _registered(db, ownership, "late-effect")
    with pytest.raises(StateError):
        db.operations.record_effects_resolved(ownership)

    db.operations.resolve_lifecycle_obligation(ownership, first)
    db.operations.resolve_lifecycle_obligation(ownership, second)
    assert db.operations.record_effects_resolved(ownership).state is OperationClaimState.RESOLVED

    db.operations.release_resolved(ownership)
    assert db.operations.inspect(ownership.scope) is None


def test_stale_and_mismatched_owners_cannot_mutate_obligations(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    obligation_id = _registered(db, ownership)
    stale = OperationOwnership(ownership.scope, "0" * 32)

    with pytest.raises(StateError):
        db.operations.mark_lifecycle_obligation_possible_effect(stale, obligation_id)
    with pytest.raises(StateError):
        db.operations.resolve_lifecycle_obligation(ownership, "0" * 32)


def test_corrupt_obligation_is_not_treated_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    ownership = database.operations.claim(_scope(), "vm-reinitialize")
    obligation_id = _registered(database, ownership)
    database.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE lifecycle_obligations SET state = 'invented' WHERE obligation_id = ?",
        (obligation_id,),
    )
    connection.commit()
    connection.close()

    reopened = Database(path)
    try:
        with pytest.raises(StateError) as raised:
            reopened.operations.list_lifecycle_obligations(ownership)
        assert raised.value.entity_kind == "database"
    finally:
        reopened.close()


def test_independent_connections_see_registration_and_admit_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = Database(path)
    second = Database(path)
    try:
        ownership = first.operations.claim(_scope(), "vm-reinitialize")
        first_id = _registered(first, ownership, "first-hold")
        second_id = _registered(second, ownership, "second-hold")
        assert first_id != second_id

        second.operations.mark_lifecycle_obligation_possible_effect(ownership, second_id)
        visible = first.operations.list_lifecycle_obligations(ownership)
        assert {obligation.obligation_id for obligation in visible} == {first_id, second_id}
        assert first.operations.inspect(ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    finally:
        second.close()
        first.close()


def test_one_repository_serializes_concurrent_registration_and_admission(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    start = Event()
    outcomes: list[tuple[str, str | None]] = []

    def register_and_admit() -> None:
        try:
            assert start.wait(timeout=10)
            obligation = db.operations.register_lifecycle_obligation(
                ownership,
                "threaded-platform-hold",
                1,
                b"prepared",
            )
            db.operations.mark_lifecycle_obligation_possible_effect(ownership, obligation.obligation_id)
            outcomes.append(("admitted", obligation.obligation_id))
        except BaseException as error:  # pragma: no cover - reported below
            outcomes.append((type(error).__name__, None))

    threads = [Thread(target=register_and_admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert [outcome for outcome, _obligation_id in outcomes] == ["admitted", "admitted"]
    obligations = db.operations.list_lifecycle_obligations(ownership)
    assert len(obligations) == 2
    assert all(obligation.state is LifecycleObligationState.POSSIBLE_EFFECT for obligation in obligations)


def test_concurrent_registration_and_admission_preserve_both_obligations(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    ownership = database.operations.claim(_scope(), "vm-reinitialize")
    database.close()

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(target=_concurrent_admission_worker, args=(path, ownership, start, outcomes)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        observed = [outcomes.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
        assert all(not process.is_alive() for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(outcome == "admitted" for outcome, _obligation_id in observed), observed
    reopened = Database(path)
    try:
        obligations = reopened.operations.list_lifecycle_obligations(ownership)
        assert len(obligations) == 2
        assert all(obligation.state is LifecycleObligationState.POSSIBLE_EFFECT for obligation in obligations)
        assert reopened.operations.inspect(ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    finally:
        reopened.close()


def test_owner_requires_sealed_resolved_ledger_and_releases_atomically(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "vm-reinitialize")
    obligation = owner.register_lifecycle_obligation("platform-hold", payload_version=1, payload=b"prepared")
    obligation.mark_possible_effect()
    obligation.resolve()

    with pytest.raises(StateError):
        owner.record_effects_resolved()

    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(owner.ownership.scope) is None
