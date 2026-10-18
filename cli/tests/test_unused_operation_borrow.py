"""Shutdown revokes only pristine local borrow authority under current ownership."""

from __future__ import annotations

import sqlite3
import sys
from types import FrameType
from typing import Any

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner, RecoveryDispatch

pytestmark = pytest.mark.windows


@pytest.fixture
def owner(db: Database) -> OperationOwner:
    scope = OperationScope(OperationResourceKind.VM, "unused-borrow-vm")
    return OperationOwner.acquire(db.operations, scope, "unused-borrow")


def test_interrupted_borrow_return_can_be_retired(
    owner: OperationOwner, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = owner.borrow
    retained: list[OperationBorrow] = []

    def lost_reply() -> OperationBorrow:
        retained.append(original())
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "borrow", lost_reply)
    with pytest.raises(KeyboardInterrupt):
        owner.borrow()
    before = db.operations.inspect(owner.ownership.scope)
    owner.stop_admission()
    owner.retire_unused_borrow()
    assert retained[0].closed
    assert db.operations.inspect(owner.ownership.scope) == before
    owner.close()
    assert db.operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("borrowed", [False, True])
def test_retirement_requires_close_intent(owner: OperationOwner, *, borrowed: bool) -> None:
    borrow = owner.borrow() if borrowed else None
    with pytest.raises(StateError):
        owner.retire_unused_borrow()
    if borrow is not None:
        assert not borrow.closed
        borrow.close()
    owner.close()


@pytest.mark.parametrize("debt", ["none", "registered", "possible-effect"])
def test_retirement_is_local_repeatable_and_preserves_other_debt(
    owner: OperationOwner, db: Database, debt: str
) -> None:
    if debt != "none":
        obligation = owner.register_lifecycle_obligation("other-adapter", payload_version=1, payload=b"receipt")
        if debt == "possible-effect":
            obligation.mark_possible_effect()
    borrow = owner.borrow()
    if debt == "possible-effect":
        owner._transition_uncertain = True  # noqa: SLF001
    before_claim = db.operations.inspect(owner.ownership.scope)
    before_ledger = owner.list_pending_lifecycle_obligations()
    before_changes = db._conn.total_changes  # noqa: SLF001
    owner.stop_admission()
    owner.retire_unused_borrow()
    owner.retire_unused_borrow()
    assert borrow.closed
    assert owner._active_borrow is None  # noqa: SLF001
    assert db._conn.total_changes == before_changes  # noqa: SLF001
    assert db.operations.inspect(owner.ownership.scope) == before_claim
    assert owner.list_pending_lifecycle_obligations() == before_ledger
    assert owner._transition_uncertain is (debt == "possible-effect")  # noqa: SLF001
    with pytest.raises(StateError):
        borrow.begin_attempt()
    with pytest.raises(StateError):
        borrow.install_dispatch_obligation("b" * 32, "late", payload_version=1, payload=b"")
    with pytest.raises(StateError):
        borrow.arm_dispatch_obligation()
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.register_lifecycle_obligation("late", payload_version=1, payload=b"")
    if debt == "possible-effect":
        with pytest.raises(StateError):
            owner.close()


@pytest.mark.parametrize("entry", ["install", "begin"])
@pytest.mark.parametrize("committed", [False, True])
def test_lost_registration_reply_is_never_pristine(
    owner: OperationOwner, db: Database, monkeypatch: pytest.MonkeyPatch, entry: str, *, committed: bool
) -> None:
    borrow = owner.borrow()
    repository = owner._repository  # noqa: SLF001
    original = repository.register_lifecycle_obligation

    def lost_reply(*args: Any, **kwargs: Any) -> Any:
        if committed:
            original(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(repository, "register_lifecycle_obligation", lost_reply)
        with pytest.raises(KeyboardInterrupt):
            if entry == "install":
                borrow.install_dispatch_obligation("a" * 32, "adapter", payload_version=1, payload=b"receipt")
            else:
                borrow.begin_attempt()
    before_claim = db.operations.inspect(owner.ownership.scope)
    before_ledger = owner.list_pending_lifecycle_obligations()
    owner.stop_admission()
    owner.retire_unused_borrow()
    assert not borrow.closed
    assert owner._active_borrow is borrow  # noqa: SLF001
    assert owner._transition_uncertain  # noqa: SLF001
    assert db.operations.inspect(owner.ownership.scope) == before_claim
    assert owner.list_pending_lifecycle_obligations() == before_ledger
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize("phase", ["installed", "armed", "outstanding", "settled"])
def test_admitted_work_is_never_retired(owner: OperationOwner, phase: str) -> None:
    borrow = owner.borrow()
    borrow.install_dispatch_obligation("a" * 32, "adapter", payload_version=1, payload=b"receipt")
    if phase == "armed":
        borrow.arm_dispatch_obligation()
    if phase in ("outstanding", "settled"):
        attempt = borrow.begin_attempt()
        if phase == "settled":
            attempt.settle()
    before = owner.list_pending_lifecycle_obligations()
    owner.stop_admission()
    owner.retire_unused_borrow()
    assert not borrow.closed
    assert owner.list_pending_lifecycle_obligations() == before
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize(
    "fact",
    ["attempt", "obligation", "identifier", "supplied", "armed", "outstanding", "recovery"],
)
def test_each_nonpristine_fact_independently_preserves_custody(owner: OperationOwner, db: Database, fact: str) -> None:
    # Model individually published facts at interruption boundaries.
    row = db.operations.register_lifecycle_obligation(owner.ownership, "adapter", 1, b"receipt")
    borrow = owner.borrow()
    if fact == "attempt":
        borrow._attempt_started = True  # noqa: SLF001
    elif fact == "obligation":
        borrow._dispatch_obligation = row  # noqa: SLF001
    elif fact == "identifier":
        borrow._dispatch_obligation_id = row.obligation_id  # noqa: SLF001
    elif fact == "supplied":
        borrow._supplied_dispatch = (row.obligation_id, "adapter", 1, b"receipt")  # noqa: SLF001
    elif fact == "armed":
        borrow._dispatch_armed = True  # noqa: SLF001
    elif fact == "outstanding":
        owner._outstanding_attempt = OperationAttempt(borrow)  # noqa: SLF001
    else:
        owner._active_recovery_dispatch = RecoveryDispatch(owner, row)  # noqa: SLF001
    owner.stop_admission()
    before_changes = db._conn.total_changes  # noqa: SLF001
    owner.retire_unused_borrow()
    assert not borrow.closed
    assert owner._active_borrow is borrow  # noqa: SLF001
    assert db._conn.total_changes == before_changes  # noqa: SLF001
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize("replacement", ["generation", "missing", "operation", "unreadable"])
def test_retirement_requires_exact_readable_current_ownership(
    owner: OperationOwner, db: Database, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    borrow = owner.borrow()
    owner.stop_admission()
    # Even uncertain release reconciliation must not authorize local revocation.
    owner._transition_uncertain = True  # noqa: SLF001
    owner._release_may_have_committed = True  # noqa: SLF001
    if replacement == "generation":
        db.operations.recover_takeover(owner.ownership, "b" * 32)
    elif replacement in ("missing", "operation"):
        db.operations.abandon_reserved(owner.ownership)
        if replacement == "operation":
            db.operations.claim(owner.ownership.scope, "replacement")
    else:

        def unreadable(scope: OperationScope) -> None:
            del scope
            raise sqlite3.OperationalError("unreadable claim")

        monkeypatch.setattr(owner._repository, "inspect", unreadable)  # noqa: SLF001
    before_changes = db._conn.total_changes  # noqa: SLF001
    with pytest.raises((StateError, sqlite3.OperationalError)):
        owner.retire_unused_borrow()
    assert not borrow.closed
    assert owner._active_borrow is borrow  # noqa: SLF001
    assert owner._transition_uncertain  # noqa: SLF001
    assert not owner._released  # noqa: SLF001
    assert db._conn.total_changes == before_changes  # noqa: SLF001


def test_interrupted_local_revocation_can_retry(owner: OperationOwner) -> None:
    borrow = owner.borrow()
    owner.stop_admission()
    interrupted = False

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        nonlocal interrupted
        if (
            event == "line"
            and frame.f_code is owner.retire_unused_borrow.__code__
            and borrow._closed  # noqa: SLF001
            and owner._active_borrow is borrow  # noqa: SLF001
        ):
            interrupted = True
            raise KeyboardInterrupt
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            owner.retire_unused_borrow()
    finally:
        sys.settrace(None)
    assert interrupted
    owner.retire_unused_borrow()
    assert borrow.closed
    owner.close()
