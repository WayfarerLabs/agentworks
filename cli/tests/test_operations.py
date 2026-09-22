"""Core operation owner lifecycle and serial-use behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationOwner

pytestmark = pytest.mark.windows


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "owned-upload-vm")


def _interrupt_after_repository_return(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: object,
    method_name: str,
    owner_method: Any,
) -> None:
    original = getattr(repository, method_name)
    returned = False
    interrupted = False

    def commit_then_return(ownership: Any) -> Any:
        nonlocal returned
        result = original(ownership)
        returned = True
        return result

    def interrupt_owner(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        nonlocal interrupted
        if event == "line" and frame.f_code is owner_method.__code__ and returned and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return interrupt_owner

    monkeypatch.setattr(repository, method_name, commit_then_return)
    sys.settrace(interrupt_owner)


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
    owner.record_effects_resolved()
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
    with pytest.raises(StateError):
        owner.close()
    owner.record_effects_resolved()
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
    owner.record_effects_resolved()
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
    owner.record_effects_resolved()
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
    owner.record_effects_resolved()
    original = repository.release_resolved

    def release_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "release_resolved", release_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.close()

    monkeypatch.setattr(repository, "release_resolved", original)
    with pytest.raises(StateError):
        owner.record_effects_resolved()
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


def test_arm_permits_later_sequential_child_borrows(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    owner.arm()

    first = owner.borrow()
    first_attempt = first.begin_attempt()
    first_attempt.settle()
    first.close()

    second = owner.borrow()
    second_attempt = second.begin_attempt()
    second_attempt.settle()
    second.close()

    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_interrupted_arm_retains_possible_dispatch_until_explicit_resolution(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    original = repository.mark_possible_dispatch

    def mark_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "mark_possible_dispatch", mark_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.arm()

    claim = repository.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()

    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


def test_interruption_after_arm_commit_blocks_dispatch_until_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    _interrupt_after_repository_return(
        monkeypatch,
        repository=repository,
        method_name="mark_possible_dispatch",
        owner_method=OperationOwner.arm,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            owner.arm()
    finally:
        sys.settrace(None)

    with pytest.raises(StateError):
        owner.borrow()
    owner.arm()
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


def test_close_refuses_settled_child_attempts_until_whole_operation_is_resolved(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()
    attempt.settle()
    borrow.close()

    with pytest.raises(StateError):
        owner.close()

    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_resolution_requires_no_active_borrow_or_outstanding_attempt(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    owner.arm()
    borrow = owner.borrow()

    with pytest.raises(StateError):
        owner.record_effects_resolved()

    attempt = borrow.begin_attempt()
    with pytest.raises(StateError):
        owner.record_effects_resolved()

    attempt.settle()
    borrow.close()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_resolution_retries_after_interruption_with_fenced_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    owner.arm()
    original = repository.record_effects_resolved

    def resolve_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "record_effects_resolved", resolve_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.record_effects_resolved()

    monkeypatch.setattr(repository, "record_effects_resolved", original)
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


def test_interruption_after_resolution_commit_blocks_dispatch_until_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    owner.arm()
    _interrupt_after_repository_return(
        monkeypatch,
        repository=repository,
        method_name="record_effects_resolved",
        owner_method=OperationOwner.record_effects_resolved,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            owner.record_effects_resolved()
    finally:
        sys.settrace(None)

    with pytest.raises(StateError):
        owner.borrow()
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


@pytest.mark.parametrize("resolved", [False, True])
def test_interrupted_release_accepts_a_replacement_fence_without_deleting_it(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved: bool,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    method_name = "abandon_reserved"
    if resolved:
        owner.arm()
        owner.record_effects_resolved()
        method_name = "release_resolved"
    original = getattr(repository, method_name)

    def release_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, method_name, release_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.close()
    monkeypatch.setattr(repository, method_name, original)

    replacement = OperationOwner.acquire(repository, _scope(), "file-upload")
    owner.close()
    claim = repository.inspect(_scope())
    assert claim is not None and claim.ownership == replacement.ownership
    replacement.close()


def test_close_abandons_a_reserved_owner(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")

    owner.close()
    assert db.operations.inspect(_scope()) is None
