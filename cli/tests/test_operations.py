"""Core operation owner lifecycle and serial-use behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationOwner

pytestmark = pytest.mark.windows


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "owned-upload-vm")


def test_owner_claim_conflicts_across_connections_before_borrow(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = Database(path)
    second = Database(path)
    try:
        owner = OperationOwner.acquire(first.operations, _scope(), "file-upload")

        with pytest.raises(StateError):
            OperationOwner.acquire(second.operations, _scope(), "file-upload")

        claim = second.operations.inspect(_scope())
        assert claim is not None
        assert claim.ownership == owner.ownership
        assert claim.state is OperationClaimState.RESERVED
        owner.close()
    finally:
        second.close()
        first.close()


def test_shared_owner_refuses_overlapping_borrows_without_waiting(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    first = owner.borrow()

    with pytest.raises(StateError):
        owner.borrow()

    first.close()
    second = owner.borrow()
    second.close()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_first_attempt_marks_possible_once_and_later_attempts_rearm_only_memory(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    calls = 0
    original = repository.mark_possible_dispatch

    def mark_possible_dispatch(ownership):
        nonlocal calls
        calls += 1
        return original(ownership)

    monkeypatch.setattr(repository, "mark_possible_dispatch", mark_possible_dispatch)

    first = borrow.begin_attempt()
    claim = db.operations.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    first.settle()
    second = borrow.begin_attempt()
    second.settle()

    assert calls == 1
    borrow.close()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_closing_stops_new_dispatch_and_requires_later_explicit_close(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()

    with pytest.raises(StateError):
        owner.close()
    with pytest.raises(StateError):
        borrow.begin_attempt()

    attempt.settle()
    borrow.close()
    owner.close()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_relinquished_borrow_leaves_attempt_permanently_unsettled(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()
    borrow.close()

    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()

    with pytest.raises(StateError):
        attempt.settle()
    claim = db.operations.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH


def test_stale_attempt_cannot_settle_a_later_attempt(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    stale = borrow.begin_attempt()
    stale.settle()
    current = borrow.begin_attempt()

    with pytest.raises(StateError):
        stale.settle()

    with pytest.raises(StateError):
        owner.close()
    current.settle()
    borrow.close()
    owner.close()


def test_normal_attempt_path_does_not_probe_persisted_state(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()

    def unexpected_inspect(scope):
        del scope
        raise AssertionError("normal attempt admission must not probe the claim")

    monkeypatch.setattr(repository, "inspect", unexpected_inspect)
    first = borrow.begin_attempt()
    first.settle()
    second = borrow.begin_attempt()
    second.settle()
    borrow.close()
    owner.close()


def test_interrupted_safe_release_accepts_absent_row_on_repeated_close(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()
    attempt.settle()
    borrow.close()
    original = repository.release_resolved

    def release_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "release_resolved", release_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.close()

    monkeypatch.setattr(repository, "release_resolved", original)
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_failed_first_durable_mark_never_returns_dispatch_permission(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    original = repository.mark_possible_dispatch

    def mark_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "mark_possible_dispatch", mark_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        borrow.begin_attempt()
    borrow.close()

    claim = db.operations.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        owner.close()
    with pytest.raises(StateError):
        owner.borrow()
