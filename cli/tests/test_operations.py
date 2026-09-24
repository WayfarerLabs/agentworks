"""Core operation owner lifecycle and serial-use behavior."""

from __future__ import annotations

import gc
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from agentworks.db import (
    Database,
    LifecycleObligationState,
    OperationClaimState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
)
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError
from agentworks.operations import (
    LifecycleObligation,
    OperationBorrow,
    OperationOwner,
    _PreRegistrationClosingRefusal,
    release_borrow_after_custody,
)

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

    def commit_then_return(*args: Any, **kwargs: Any) -> Any:
        nonlocal returned
        result = original(*args, **kwargs)
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


def _admit_resolved_effect(owner: OperationOwner) -> None:
    obligation = owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"")
    obligation.mark_possible_effect()
    obligation.resolve()


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


def test_recovery_rotates_only_generation_and_preserves_the_sealed_ledger(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
        obligation_id="a" * 32,
    )
    obligation.mark_possible_effect()
    obligation.publish_payload(expected_revision=0, payload_version=2, payload=b"published")
    before = db.operations.list_lifecycle_obligations(predecessor.ownership)[0]

    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    claim = db.operations.inspect(_scope())
    after = db.operations.list_lifecycle_obligations(recovered.ownership)[0]

    assert claim is not None
    assert recovered.ownership.operation_id == predecessor.ownership.operation_id
    assert recovered.ownership.generation_id == "b" * 32
    assert claim.obligations_sealed_at is not None
    assert (
        after.obligation_id,
        after.obligation_kind,
        after.state,
        after.payload_version,
        after.payload,
        after.payload_revision,
        after.registered_at,
        after.updated_at,
    ) == (
        before.obligation_id,
        before.obligation_kind,
        before.state,
        before.payload_version,
        before.payload,
        before.payload_revision,
        before.registered_at,
        before.updated_at,
    )

    retry = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    assert retry.ownership == recovered.ownership
    wrong_predecessor = OperationOwnership(
        predecessor.ownership.scope,
        predecessor.ownership.operation_id,
        "0" * 32,
    )
    with pytest.raises(StateError):
        OperationOwner.recover(db.operations, wrong_predecessor, "b" * 32)
    with pytest.raises(StateError):
        OperationOwner.recover(db.operations, predecessor.ownership, "c" * 32)

    rebound = recovered.rebind_lifecycle_obligation(
        "a" * 32, "adapter-dispatch", payload_version=2, payload=b"published"
    )
    assert rebound.obligation_id == "a" * 32
    rebound.publish_payload(
        expected_revision=rebound.payload_revision,
        payload_version=3,
        payload=b"recovery-published",
    )
    with pytest.raises(StateError):
        recovered.register_lifecycle_obligation("new-adapter", payload_version=1, payload=b"")
    with pytest.raises(StateError):
        recovered.record_effects_resolved()
    rebound.resolve()
    recovered.record_effects_resolved()
    recovered.close()


def test_exact_recovery_retry_shares_live_custody_across_repository_facades(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "file-call", payload_version=1, payload=b"prepared", obligation_id="a" * 32
    )
    obligation.mark_possible_effect()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    retry = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    assert retry is recovered

    bound = recovered.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"prepared",
        payload_revision=0,
    )
    rebound = retry.rebind_lifecycle_obligation(
        obligation.obligation_id, "file-call", payload_version=1, payload=b"prepared"
    )
    dispatch = bound.open_dispatch()
    dispatch.begin_attempt()

    with pytest.raises(StateError):
        rebound.publish_payload(expected_revision=0, payload_version=2, payload=b"competing")
    with pytest.raises(StateError):
        retry.record_effects_resolved()
    with pytest.raises(StateError):
        retry.close()

    dispatch.handoff_unresolved()
    with pytest.raises(StateError):
        rebound.publish_payload(expected_revision=0, payload_version=2, payload=b"competing")
    assert db.operations.list_lifecycle_obligations(recovered.ownership)[0].payload == b"prepared"


def test_exact_recovery_retry_recreates_unreferenced_owner(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    reference = weakref.ref(recovered)
    del recovered
    gc.collect()
    assert reference() is None

    recreated = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    assert recreated.ownership.generation_id == "b" * 32
    assert recreated is OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)


def test_concurrent_exact_recovery_retries_share_one_live_owner(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    repositories = (db.operations, db.operations)
    ready = threading.Barrier(2)

    def recover(repository: OperationRepository) -> OperationOwner:
        ready.wait()
        return OperationOwner.recover(repository, predecessor.ownership, "b" * 32)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(recover, repositories))
    assert first is second


def test_recovery_cannot_admit_registered_obligations_or_reregister_them(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
        obligation_id="7" * 32,
    )
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "8" * 32)
    rebound = recovered.rebind_lifecycle_obligation(
        obligation.obligation_id,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )

    with pytest.raises(StateError):
        rebound.mark_possible_effect()
    with pytest.raises(StateError):
        rebound.publish_payload(expected_revision=0, payload_version=1, payload=b"recovery-published")
    with pytest.raises(StateError):
        db.operations.mark_lifecycle_obligation_possible_effect(recovered.ownership, obligation.obligation_id)
    with pytest.raises(StateError):
        db.operations.publish_lifecycle_obligation_payload(
            recovered.ownership,
            obligation.obligation_id,
            expected_revision=0,
            payload_version=1,
            payload=b"recovery-published",
        )
    with pytest.raises(StateError):
        db.operations.register_lifecycle_obligation(
            recovered.ownership,
            "adapter-dispatch",
            1,
            b"prepared",
            obligation_id=obligation.obligation_id,
        )

    db.operations.resolve_lifecycle_obligation(recovered.ownership, obligation.obligation_id)
    recovered.record_effects_resolved()
    recovered.close()


def test_recovery_fences_every_predecessor_repository_transition(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation("adapter-dispatch", payload_version=1, payload=b"")
    obligation.mark_possible_effect()
    obligation.resolve()
    predecessor.seal_lifecycle_obligations()
    predecessor.record_effects_resolved()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "d" * 32)

    with pytest.raises(StateError):
        predecessor.register_lifecycle_obligation("new-adapter", payload_version=1, payload=b"")
    with pytest.raises(StateError):
        obligation.mark_possible_effect()
    with pytest.raises(StateError):
        obligation.publish_payload(expected_revision=0, payload_version=1, payload=b"")
    with pytest.raises(StateError):
        obligation.resolve()
    with pytest.raises(StateError):
        predecessor.seal_lifecycle_obligations()
    with pytest.raises(StateError):
        predecessor.record_effects_resolved()
    with pytest.raises(StateError):
        predecessor.close()

    recovered.close()
    assert db.operations.inspect(_scope()) is None


def test_recovery_reserved_claim_requires_explicit_sealed_resolution(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "e" * 32)

    with pytest.raises(StateError):
        recovered.close()
    recovered.record_effects_resolved()
    recovered.close()
    assert db.operations.inspect(_scope()) is None


def test_recovery_resolved_claim_can_release(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    predecessor.seal_lifecycle_obligations()
    predecessor.record_effects_resolved()

    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "f" * 32)
    recovered.close()
    assert db.operations.inspect(_scope()) is None


def test_recovery_refuses_ordinary_borrow_but_rebinds_exact_supplied_obligation(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = predecessor.borrow()
    obligation = borrow.install_dispatch_obligation(
        "c" * 32,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )
    attempt = borrow.begin_attempt()
    attempt.settle()
    borrow.handoff_retained_effect()

    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "d" * 32)
    with pytest.raises(StateError):
        recovered.borrow()

    rebound = recovered.rebind_lifecycle_obligation(
        obligation.obligation_id,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )
    rebound.resolve()
    recovered.record_effects_resolved()
    recovered.close()


def test_recovery_dispatch_revalidates_exact_possible_effect_without_auto_resolution(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"first",
        obligation_id="e" * 32,
    )
    obligation.mark_possible_effect()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "f" * 32)

    bound = recovered.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"first",
        payload_revision=0,
    )
    dispatch = bound.open_dispatch()
    attempt = dispatch.begin_attempt()

    with pytest.raises(StateError):
        recovered.rebind_lifecycle_obligation(
            obligation.obligation_id,
            "file-call",
            payload_version=1,
            payload=b"first",
        )
    with pytest.raises(StateError):
        dispatch.close()

    attempt.settle()
    dispatch.close()
    persisted = db.operations.list_lifecycle_obligations(recovered.ownership)
    assert persisted[0].state is LifecycleObligationState.POSSIBLE_EFFECT

    changed = recovered.rebind_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"first",
    )
    changed.publish_payload(expected_revision=0, payload_version=2, payload=b"second")
    stale = bound.open_dispatch()
    with pytest.raises(StateError):
        stale.begin_attempt()
    stale.close()

    changed.resolve()
    recovered.record_effects_resolved()
    recovered.close()


def test_recovery_dispatch_uncertain_handoff_retains_owner_and_effect(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"prepared",
        obligation_id="a" * 32,
    )
    obligation.mark_possible_effect()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    bound = recovered.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"prepared",
        payload_revision=0,
    )
    dispatch = bound.open_dispatch()
    dispatch.begin_attempt()
    dispatch.handoff_unresolved()

    with pytest.raises(StateError):
        recovered.close()
    with pytest.raises(StateError):
        recovered.rebind_lifecycle_obligation(
            obligation.obligation_id,
            "file-call",
            payload_version=1,
            payload=b"prepared",
        )
    persisted = db.operations.list_lifecycle_obligations(recovered.ownership)
    assert persisted[0].state is LifecycleObligationState.POSSIBLE_EFFECT


def test_recovery_dispatch_blocks_rebound_payload_publication_while_active_or_retained(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    obligation = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"prepared",
        obligation_id="c" * 32,
    )
    obligation.mark_possible_effect()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "d" * 32)
    bound = recovered.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"prepared",
        payload_revision=0,
    )
    stale = recovered.rebind_lifecycle_obligation(
        obligation.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"prepared",
    )
    dispatch = bound.open_dispatch()

    with pytest.raises(StateError):
        stale.publish_payload(expected_revision=0, payload_version=2, payload=b"competing")

    dispatch.begin_attempt()
    with pytest.raises(StateError):
        stale.publish_payload(expected_revision=0, payload_version=2, payload=b"competing")

    dispatch.handoff_unresolved()
    with pytest.raises(StateError):
        stale.publish_payload(expected_revision=0, payload_version=2, payload=b"competing")

    assert db.operations.list_lifecycle_obligations(recovered.ownership)[0].payload == b"prepared"


def test_recovery_dispatch_refuses_nonpossible_rows_and_changed_exact_identity(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    registered = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"registered",
        obligation_id="c" * 32,
    )
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "d" * 32)
    with pytest.raises(StateError):
        recovered.rebind_possible_effect_lifecycle_obligation(
            registered.obligation_id,
            "file-call",
            payload_version=1,
            payload=b"registered",
            payload_revision=0,
        )

    predecessor = OperationOwner.acquire(
        db.operations,
        OperationScope(OperationResourceKind.VM, "other-vm"),
        "file-upload",
    )
    resolved = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"resolved",
        obligation_id="e" * 32,
    )
    resolved.mark_possible_effect()
    resolved.resolve()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "f" * 32)
    with pytest.raises(StateError):
        recovered.rebind_possible_effect_lifecycle_obligation(
            resolved.obligation_id,
            "file-call",
            payload_version=1,
            payload=b"resolved",
            payload_revision=0,
        )

    predecessor = OperationOwner.acquire(
        db.operations,
        OperationScope(OperationResourceKind.VM, "third-vm"),
        "file-upload",
    )
    possible = predecessor.register_lifecycle_obligation(
        "file-call",
        payload_version=1,
        payload=b"first",
        obligation_id="a" * 32,
    )
    possible.mark_possible_effect()
    recovered = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    with pytest.raises(StateError):
        recovered.rebind_possible_effect_lifecycle_obligation(
            possible.obligation_id,
            "file-call",
            payload_version=1,
            payload=b"first",
            payload_revision=1,
        )
    bound = recovered.rebind_possible_effect_lifecycle_obligation(
        possible.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"first",
        payload_revision=0,
    )
    changed = recovered.rebind_lifecycle_obligation(
        possible.obligation_id,
        "file-call",
        payload_version=1,
        payload=b"first",
    )
    changed.publish_payload(expected_revision=0, payload_version=2, payload=b"changed")
    stale = bound.open_dispatch()
    with pytest.raises(StateError):
        stale.begin_attempt()
    stale.close()


def test_ordinary_owner_cannot_rebind_an_obligation_even_after_sealing(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")

    with pytest.raises(StateError):
        owner.rebind_lifecycle_obligation("a" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")

    owner.seal_lifecycle_obligations()
    with pytest.raises(StateError):
        owner.rebind_lifecycle_obligation("a" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")
    owner.record_effects_resolved()
    owner.close()


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


def test_each_borrow_attempt_revalidates_its_generic_obligation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    calls = 0
    original = repository.mark_lifecycle_obligation_possible_effect

    def mark_possible_effect(ownership, obligation_id):
        nonlocal calls
        calls += 1
        return original(ownership, obligation_id)

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", mark_possible_effect)

    first = borrow.begin_attempt()
    claim = db.operations.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    first.settle()
    second = borrow.begin_attempt()
    second.settle()

    assert calls == 2
    borrow.close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_takeover_fences_a_later_attempt_from_an_already_armed_borrow(db: Database) -> None:
    predecessor = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = predecessor.borrow()
    first = borrow.begin_attempt()
    first.settle()

    OperationOwner.recover(db.operations, predecessor.ownership, "a" * 32)

    with pytest.raises(StateError):
        borrow.begin_attempt()
    assert borrow.has_outstanding_attempt


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
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_relinquished_borrow_leaves_attempt_permanently_unsettled(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()
    borrow.handoff_unresolved()

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
    owner.seal_lifecycle_obligations()
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
    owner.seal_lifecycle_obligations()
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
    owner.seal_lifecycle_obligations()
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
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


@pytest.mark.parametrize("committed", [False, True], ids=["before-mark", "after-mark"])
def test_interrupted_first_durable_mark_never_returns_dispatch_permission(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    original = repository.mark_lifecycle_obligation_possible_effect

    def mark_then_interrupt(ownership, obligation_id):
        if committed:
            original(ownership, obligation_id)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", mark_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        borrow.begin_attempt()
    assert borrow.has_outstanding_attempt
    with pytest.raises(StateError):
        borrow.begin_attempt()
    borrow.handoff_unresolved()

    claim = db.operations.inspect(_scope())
    assert claim is not None
    assert claim.state is (OperationClaimState.POSSIBLE_DISPATCH if committed else OperationClaimState.RESERVED)
    with pytest.raises(StateError):
        owner.close()
    with pytest.raises(StateError):
        owner.borrow()


def test_interrupted_registration_before_attempt_retries_with_durable_admission(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    _interrupt_after_repository_return(
        monkeypatch,
        repository=repository,
        method_name="register_lifecycle_obligation",
        owner_method=OperationBorrow.begin_attempt,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            borrow.begin_attempt()
    finally:
        sys.settrace(None)

    attempt = borrow.begin_attempt()
    claim = repository.inspect(_scope())
    obligations = repository.list_lifecycle_obligations(owner.ownership)
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    assert any(obligation.state is LifecycleObligationState.POSSIBLE_EFFECT for obligation in obligations)

    attempt.settle()
    borrow.close()
    assert [obligation.state for obligation in repository.list_lifecycle_obligations(owner.ownership)] == [
        LifecycleObligationState.RESOLVED
    ]


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_interrupted_borrow_close_blocks_admission_until_retry(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    attempt = borrow.begin_attempt()
    attempt.settle()
    original = repository.resolve_lifecycle_obligation

    if committed:
        _interrupt_after_repository_return(
            monkeypatch,
            repository=repository,
            method_name="resolve_lifecycle_obligation",
            owner_method=OperationBorrow.close,
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                borrow.close()
        finally:
            sys.settrace(None)
    else:

        def resolve_then_interrupt(ownership, obligation_id):
            del ownership, obligation_id
            raise KeyboardInterrupt

        monkeypatch.setattr(repository, "resolve_lifecycle_obligation", resolve_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            borrow.close()

    obligations = repository.list_lifecycle_obligations(owner.ownership)
    assert obligations[0].state is (
        LifecycleObligationState.RESOLVED if committed else LifecycleObligationState.POSSIBLE_EFFECT
    )
    with pytest.raises(StateError):
        borrow.begin_attempt()
    with pytest.raises(StateError):
        borrow.handoff_unresolved()
    with pytest.raises(StateError):
        owner.borrow()

    monkeypatch.setattr(repository, "resolve_lifecycle_obligation", original)
    borrow.close()
    assert repository.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_admitted_effect_permits_later_sequential_child_borrows(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    _admit_resolved_effect(owner)

    first = owner.borrow()
    first_attempt = first.begin_attempt()
    first_attempt.settle()
    first.close()

    second = owner.borrow()
    second_attempt = second.begin_attempt()
    second_attempt.settle()
    second.close()

    owner.seal_lifecycle_obligations()

    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_interrupted_effect_admission_retains_possible_dispatch_until_explicit_resolution(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    obligation = owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"")
    original = repository.mark_lifecycle_obligation_possible_effect

    def mark_then_interrupt(ownership, obligation_id):
        original(ownership, obligation_id)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", mark_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        obligation.mark_possible_effect()

    claim = repository.inspect(_scope())
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", original)
    obligation.resolve()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


def test_interruption_after_effect_admission_commit_blocks_dispatch_until_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    obligation = owner.register_lifecycle_obligation("test-effect", payload_version=1, payload=b"")
    _interrupt_after_repository_return(
        monkeypatch,
        repository=repository,
        method_name="mark_lifecycle_obligation_possible_effect",
        owner_method=LifecycleObligation.mark_possible_effect,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            obligation.mark_possible_effect()
    finally:
        sys.settrace(None)

    with pytest.raises(StateError):
        owner.borrow()
    obligation.resolve()
    owner.seal_lifecycle_obligations()
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

    owner.seal_lifecycle_obligations()

    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_resolution_requires_no_active_borrow_or_outstanding_attempt(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()

    with pytest.raises(StateError):
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()

    attempt = borrow.begin_attempt()
    with pytest.raises(StateError):
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()

    attempt.settle()
    borrow.close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_resolution_retries_after_interruption_with_fenced_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    _admit_resolved_effect(owner)
    original = repository.record_effects_resolved

    def resolve_then_interrupt(ownership):
        original(ownership)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "record_effects_resolved", resolve_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()

    monkeypatch.setattr(repository, "record_effects_resolved", original)
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(_scope()) is None


def test_interruption_after_resolution_commit_blocks_dispatch_until_reconciliation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    _admit_resolved_effect(owner)
    _interrupt_after_repository_return(
        monkeypatch,
        repository=repository,
        method_name="record_effects_resolved",
        owner_method=OperationOwner.record_effects_resolved,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            owner.seal_lifecycle_obligations()
            owner.record_effects_resolved()
    finally:
        sys.settrace(None)

    with pytest.raises(StateError):
        owner.borrow()
    owner.seal_lifecycle_obligations()
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
        _admit_resolved_effect(owner)
        owner.seal_lifecycle_obligations()
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


def test_interrupted_release_does_not_treat_same_operation_takeover_as_success(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    recovered: OperationOwner | None = None

    def take_over_then_interrupt(ownership):
        nonlocal recovered
        recovered = OperationOwner.recover(repository, ownership, "b" * 32)
        raise KeyboardInterrupt

    original = repository.release_resolved
    monkeypatch.setattr(repository, "release_resolved", take_over_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.close()

    with pytest.raises(StateError):
        owner.close()
    assert recovered is not None
    monkeypatch.setattr(repository, "release_resolved", original)
    recovered.close()


def test_close_abandons_a_reserved_owner(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")

    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_supplied_dispatch_obligation_replaces_carrier_row_across_attempts(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    obligation_id = "1" * 32
    borrow.install_dispatch_obligation(
        obligation_id,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )

    for _ in range(3):
        attempt = borrow.begin_attempt()
        attempt.settle()

    obligations = db.operations.list_lifecycle_obligations(owner.ownership)
    assert [(obligation.obligation_id, obligation.obligation_kind, obligation.state) for obligation in obligations] == [
        (obligation_id, "adapter-dispatch", LifecycleObligationState.POSSIBLE_EFFECT)
    ]

    borrow.close()
    assert db.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED


def test_close_requested_before_supplied_install_proves_no_registration_started(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    with pytest.raises(StateError):
        owner.close()

    with pytest.raises(_PreRegistrationClosingRefusal):
        borrow.install_dispatch_obligation(
            "9" * 32,
            "adapter-dispatch",
            payload_version=1,
            payload=b"prepared",
        )

    assert db.operations.list_lifecycle_obligations(owner.ownership) == ()
    borrow.close()
    owner.close()
    assert db.operations.inspect(_scope()) is None


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_interrupted_supplied_installation_retries_the_same_row(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed: bool,
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    obligation_id = "2" * 32
    original = repository.register_lifecycle_obligation

    if committed:
        _interrupt_after_repository_return(
            monkeypatch,
            repository=repository,
            method_name="register_lifecycle_obligation",
            owner_method=OperationBorrow.install_dispatch_obligation,
        )
    else:

        def interrupt_before_commit(*args: Any, **kwargs: Any) -> object:
            del args, kwargs
            raise KeyboardInterrupt

        monkeypatch.setattr(repository, "register_lifecycle_obligation", interrupt_before_commit)

    try:
        with pytest.raises(KeyboardInterrupt):
            borrow.install_dispatch_obligation(
                obligation_id,
                "adapter-dispatch",
                payload_version=1,
                payload=b"prepared",
            )
    finally:
        sys.settrace(None)

    monkeypatch.setattr(repository, "register_lifecycle_obligation", original)
    borrow.install_dispatch_obligation(
        obligation_id,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )
    obligations = repository.list_lifecycle_obligations(owner.ownership)
    assert [obligation.obligation_id for obligation in obligations] == [obligation_id]


def test_borrow_refuses_a_second_or_late_supplied_dispatch_obligation(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    borrow.install_dispatch_obligation("3" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")

    with pytest.raises(StateError):
        borrow.install_dispatch_obligation("4" * 32, "other-dispatch", payload_version=1, payload=b"prepared")

    attempt = borrow.begin_attempt()
    attempt.settle()
    with pytest.raises(StateError):
        borrow.install_dispatch_obligation("3" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")


def test_retained_supplied_effect_refuses_outer_resolution_until_adapter_cleanup(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    obligation_id = "5" * 32
    obligation = borrow.install_dispatch_obligation(
        obligation_id,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )
    attempt = borrow.begin_attempt()
    attempt.settle()

    release_borrow_after_custody(borrow, retain_effect=True)
    obligations = db.operations.list_lifecycle_obligations(owner.ownership)
    assert obligations[0].state is LifecycleObligationState.POSSIBLE_EFFECT

    owner.seal_lifecycle_obligations()
    with pytest.raises(StateError):
        owner.record_effects_resolved()
    with pytest.raises(StateError):
        db.operations.release_resolved(owner.ownership)

    obligation.resolve()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(_scope()) is None


def test_retained_effect_handoff_requires_an_armed_supplied_obligation(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    with pytest.raises(StateError):
        release_borrow_after_custody(borrow, retain_effect=True)
    borrow.close()


def test_prearmed_adapter_effect_hands_off_without_carrier_attempt(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    assert not borrow.has_installed_dispatch_obligation
    assert not borrow.dispatch_obligation_may_be_armed
    borrow.install_dispatch_obligation("6" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")
    assert borrow.has_installed_dispatch_obligation
    assert not borrow.dispatch_obligation_may_be_armed

    borrow.arm_dispatch_obligation()
    assert borrow.dispatch_obligation_may_be_armed
    release_borrow_after_custody(borrow, retain_effect=True)

    row = db.operations.list_lifecycle_obligations(owner.ownership)[0]
    claim = db.operations.inspect(_scope())
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        owner.close()


def test_prearmed_adapter_effect_composes_with_later_attempt(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    borrow.install_dispatch_obligation("6" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")
    borrow.arm_dispatch_obligation()

    attempt = borrow.begin_attempt()
    attempt.settle()
    release_borrow_after_custody(borrow, retain_effect=True)

    rows = db.operations.list_lifecycle_obligations(owner.ownership)
    assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_interrupted_prearming_allows_conservative_handoff(
    db: Database, monkeypatch: pytest.MonkeyPatch, *, committed: bool
) -> None:
    repository = db.operations
    owner = OperationOwner.acquire(repository, _scope(), "file-upload")
    borrow = owner.borrow()
    borrow.install_dispatch_obligation("6" * 32, "adapter-dispatch", payload_version=1, payload=b"prepared")
    original = repository.mark_lifecycle_obligation_possible_effect

    def interrupted(*args: Any, **kwargs: Any) -> object:
        if committed:
            original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", interrupted)
    with pytest.raises(KeyboardInterrupt):
        borrow.arm_dispatch_obligation()
    assert borrow.dispatch_obligation_may_be_armed
    release_borrow_after_custody(borrow, retain_effect=True)

    rows = db.operations.list_lifecycle_obligations(owner.ownership)
    assert len(rows) == 1
    assert rows[0].state is (
        LifecycleObligationState.POSSIBLE_EFFECT if committed else LifecycleObligationState.REGISTERED
    )
    with pytest.raises(StateError):
        owner.close()


def test_prepared_supplied_payload_stays_on_the_row_armed_for_dispatch(db: Database) -> None:
    owner = OperationOwner.acquire(db.operations, _scope(), "file-upload")
    borrow = owner.borrow()
    obligation = borrow.install_dispatch_obligation(
        "8" * 32,
        "adapter-dispatch",
        payload_version=1,
        payload=b"prepared",
    )

    obligation.publish_payload(
        expected_revision=obligation.payload_revision,
        payload_version=2,
        payload=b"child-token",
    )
    attempt = borrow.begin_attempt()
    row = db.operations.list_lifecycle_obligations(owner.ownership)[0]

    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row.payload_version == 2
    assert row.payload == b"child-token"
    assert row.payload_revision == 1
    attempt.settle()
    borrow.close()
