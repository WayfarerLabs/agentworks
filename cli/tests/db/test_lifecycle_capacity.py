"""Bound unfinished debt independently of immutable completed receipt history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationOwner, _is_pre_registration_refusal, _PreRegistrationRefusal

pytestmark = pytest.mark.windows


@pytest.fixture
def owned(tmp_path):
    database = Database(tmp_path / "capacity.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "capacity-vm"), "test")
    try:
        yield database, owner
    finally:
        database.close()


def test_completed_receipts_do_not_consume_pending_capacity_or_rearm(owned, monkeypatch):
    database, owner = owned
    receipts = []
    for index in range(300):
        obligation = owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=str(index).encode())
        obligation.resolve()
        receipts.append(owner.inspect_lifecycle_obligation(obligation.obligation_id))
    assert owner.list_pending_lifecycle_obligations() == ()
    first = receipts[0]
    assert first is not None
    assert (
        database.operations.register_lifecycle_obligation(
            owner.ownership,
            first.obligation_kind,
            first.payload_version,
            first.payload,
            obligation_id=first.obligation_id,
        )
        == first
    )
    assert database.operations.resolve_lifecycle_obligation(owner.ownership, first.obligation_id) == first
    with pytest.raises(StateError):
        database.operations.mark_lifecycle_obligation_possible_effect(owner.ownership, first.obligation_id)
    with pytest.raises(StateError):
        database.operations.publish_lifecycle_obligation_payload(
            owner.ownership,
            first.obligation_id,
            expected_revision=first.payload_revision,
            payload_version=1,
            payload=b"new",
        )
    original = type(database.operations)._decode_obligation

    def reject_history(row, ownership):
        assert row["state"] != LifecycleObligationState.RESOLVED
        return original(row, ownership)

    monkeypatch.setattr(type(database.operations), "_decode_obligation", staticmethod(reject_history))
    pending = owner.register_lifecycle_obligation("current-effect", payload_version=1, payload=b"pending")
    assert len(owner.list_pending_lifecycle_obligations()) == 1
    monkeypatch.setattr(type(database.operations), "_decode_obligation", staticmethod(original))
    pending.resolve()
    monkeypatch.setattr(type(database.operations), "_decode_obligation", staticmethod(reject_history))
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert database.operations.inspect(owner.ownership.scope) is None


def test_actual_pending_queries_use_migrated_partial_index(owned):
    database, owner = owned
    for _ in range(300):
        obligation = owner.register_lifecycle_obligation("history", payload_version=1, payload=b"closed")
        obligation.resolve()
    statements: list[str] = []
    database.operations._connection.set_trace_callback(statements.append)
    obligation = owner.register_lifecycle_obligation("pending", payload_version=1, payload=b"current")
    owner.list_pending_lifecycle_obligations()
    owner.seal_lifecycle_obligations()
    with pytest.raises(StateError):
        owner.record_effects_resolved()
    database.operations._connection.set_trace_callback(None)
    queries = [sql for sql in statements if "lifecycle_obligations" in sql and "state IS NOT 'resolved'" in sql]
    assert any(sql.startswith("INSERT") for sql in queries)
    assert any(sql.startswith("SELECT obligations") for sql in queries)
    assert any(sql.startswith("SELECT 1") for sql in queries)
    for sql in queries:
        plan = database._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        assert any("lifecycle_obligations_pending" in row[3] for row in plan)
    obligation.resolve()
    owner.record_effects_resolved()
    owner.close()


def test_exact_lookup_missing_requires_current_owner_and_closed_rows_prevent_abandonment(owned):
    database, owner = owned
    missing = "c" * 32
    assert owner.inspect_lifecycle_obligation(missing) is None
    obligation = owner.register_lifecycle_obligation("history", payload_version=1, payload=b"closed")
    obligation.resolve()
    with pytest.raises(StateError):
        database.operations.abandon_reserved(owner.ownership)
    predecessor = owner.ownership
    recovered = database.operations.recover_takeover(predecessor, "b" * 32)
    assert database.operations.inspect_lifecycle_obligation(recovered.ownership, obligation.obligation_id) is not None
    with pytest.raises(StateError):
        database.operations.inspect_lifecycle_obligation(predecessor, missing)
    with pytest.raises(StateError):
        database.operations.list_pending_lifecycle_obligations(predecessor)


def test_upgrade_41_preserves_receipts_and_adds_partial_index(tmp_path: Path):
    path = tmp_path / "upgrade.db"
    database = Database(path)
    ownership = database.operations.claim(OperationScope(OperationResourceKind.VM, "upgrade-vm"), "test")
    receipt = database.operations.register_lifecycle_obligation(ownership, "history", 1, b"receipt")
    database.operations.resolve_lifecycle_obligation(ownership, receipt.obligation_id)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX lifecycle_obligations_pending")
        connection.execute("DELETE FROM schema_version WHERE version = 42")
    upgraded = Database(path)
    try:
        kept = upgraded.operations.inspect_lifecycle_obligation(ownership, receipt.obligation_id)
        assert kept is not None and kept.payload == receipt.payload and kept.state is LifecycleObligationState.RESOLVED
        assert upgraded._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 42
        assert (
            upgraded._conn.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'lifecycle_obligations_pending'"
            ).fetchone()
            is not None
        )
    finally:
        upgraded.close()


@pytest.mark.parametrize("failure", ["absent", "committed", "mismatched", "unreadable", "control", "exit"])
def test_failed_registration_only_cleans_exact_fenced_absence(owned, monkeypatch, failure):
    database, owner = owned
    identifier = "f" * 32
    original = type(database.operations).register_lifecycle_obligation
    control = KeyboardInterrupt() if failure == "control" else SystemExit() if failure == "exit" else OSError()
    original_cause = ValueError()
    control.__cause__ = original_cause

    def refuse(repository, ownership, kind, version, payload, *, obligation_id=None):
        if failure in {"committed", "mismatched"}:
            original(
                repository,
                ownership,
                kind,
                version,
                b"other" if failure == "mismatched" else payload,
                obligation_id=obligation_id,
            )
        raise control

    monkeypatch.setattr(type(database.operations), "register_lifecycle_obligation", refuse)
    if failure == "unreadable":

        def unreadable(*args):
            raise OSError()

        monkeypatch.setattr(type(database.operations), "inspect_lifecycle_obligation", unreadable)
    with pytest.raises(BaseException) as caught:
        owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"own", obligation_id=identifier)
    if failure == "absent":
        assert caught.value is control and isinstance(control.__cause__, _PreRegistrationRefusal)
        assert control.__cause__.__cause__ is original_cause
        assert _is_pre_registration_refusal(control)
    else:
        assert caught.value is control and control.__cause__ is original_cause


def test_true_pending_cap_is_unchanged_and_never_dispatched_borrow_can_close(owned):
    database, owner = owned
    obligations = [
        owner.register_lifecycle_obligation("unfinished", payload_version=1, payload=b"pending") for _ in range(128)
    ]
    borrow = owner.borrow()
    with pytest.raises(StateError) as caught:
        borrow.install_dispatch_obligation("e" * 32, "file-call", payload_version=1, payload=b"new")
    assert _is_pre_registration_refusal(caught.value)
    assert owner.inspect_lifecycle_obligation("e" * 32) is None
    borrow.close()
    assert len(owner.list_pending_lifecycle_obligations()) == 128
    for obligation in obligations:
        obligation.resolve()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_reused_error_does_not_reuse_an_earlier_absence_proof(owned, monkeypatch):
    database, owner = owned
    original = type(database.operations).register_lifecycle_obligation
    primary, previous = OSError(), ValueError()
    primary.__cause__ = previous
    committed = False
    identifier = "e" * 32

    def fail(repository, ownership, kind, version, payload, *, obligation_id=None):
        if committed:
            original(repository, ownership, kind, version, payload, obligation_id=obligation_id)
        raise primary

    monkeypatch.setattr(type(database.operations), "register_lifecycle_obligation", fail)
    with pytest.raises(OSError) as caught:
        owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"same", obligation_id=identifier)
    assert caught.value is primary and _is_pre_registration_refusal(primary)
    assert primary.__cause__ is not None and primary.__cause__.__cause__ is previous
    committed = True
    with pytest.raises(OSError) as caught:
        owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"same", obligation_id=identifier)
    assert caught.value is primary and primary.__cause__ is previous
    assert not _is_pre_registration_refusal(primary)
    receipt = owner.inspect_lifecycle_obligation(identifier)
    assert receipt is not None and receipt.state is LifecycleObligationState.REGISTERED
