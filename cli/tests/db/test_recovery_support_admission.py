"""Bounded durable custody for recovery support effects."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks.db import (
    MAX_LIFECYCLE_OBLIGATIONS,
    Database,
    LifecycleObligationState,
    OperationClaimState,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError

pytestmark = pytest.mark.windows


def test_support_admission_fences_generations_preserves_debts_and_blocks_finalization(db: Database) -> None:
    repository = db.operations
    scope = OperationScope(OperationResourceKind.VM, "support-vm")
    predecessor = repository.claim(scope, "file-upload")
    old = repository.register_lifecycle_obligation(predecessor, "application", 1, b"old")
    original = repository.mark_lifecycle_obligation_possible_effect(predecessor, old.obligation_id)
    recovered = repository.recover_takeover(predecessor, "b" * 32).ownership

    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(predecessor, "support", 1, b"new", obligation_id="c" * 32)
    support = repository.admit_recovery_support_obligation(recovered, "support", 1, b"new", obligation_id="c" * 32)
    claim = repository.inspect(scope)
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    assert claim.obligations_sealed_at is not None
    assert support.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert replace(original, ownership=recovered) in repository.list_pending_lifecycle_obligations(recovered)

    successor = repository.recover_takeover(recovered, "d" * 32).ownership
    assert {row.obligation_id for row in repository.list_pending_lifecycle_obligations(successor)} == {
        old.obligation_id,
        support.obligation_id,
    }
    for transition in (
        lambda: repository.admit_recovery_support_obligation(
            recovered, "support", 1, b"new", obligation_id=support.obligation_id
        ),
        lambda: repository.publish_lifecycle_obligation_payload(
            recovered, support.obligation_id, expected_revision=0, payload_version=2, payload=b"receipt"
        ),
        lambda: repository.resolve_lifecycle_obligation(recovered, support.obligation_id),
        lambda: repository.record_effects_resolved(recovered),
    ):
        with pytest.raises(StateError):
            transition()
    repository.resolve_lifecycle_obligation(successor, old.obligation_id)
    with pytest.raises(StateError):
        repository.record_effects_resolved(successor)
    repository.resolve_lifecycle_obligation(successor, support.obligation_id)
    repository.record_effects_resolved(successor)
    repository.release_resolved(successor)
    assert repository.inspect(scope) is None


def test_support_admission_requires_recovery_and_exact_possible_retry(db: Database) -> None:
    repository = db.operations
    scope = OperationScope(OperationResourceKind.VM, "support-vm")
    predecessor = repository.claim(scope, "file-upload")
    old = repository.register_lifecycle_obligation(predecessor, "support", 1, b"new", obligation_id="a" * 32)
    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(predecessor, "support", 1, b"new", obligation_id="c" * 32)
    recovered = repository.recover_takeover(predecessor, "b" * 32).ownership
    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(recovered, "support", 1, b"new", obligation_id=old.obligation_id)
    with pytest.raises(StateError):
        repository.mark_lifecycle_obligation_possible_effect(recovered, old.obligation_id)
    with pytest.raises(StateError):
        repository.register_lifecycle_obligation(recovered, "support", 1, b"new")
    admitted = repository.admit_recovery_support_obligation(recovered, "support", 1, b"new", obligation_id="c" * 32)
    assert (
        repository.admit_recovery_support_obligation(
            recovered, "support", 1, b"new", obligation_id=admitted.obligation_id
        )
        == admitted
    )
    for kind, version, payload in (("other", 1, b"new"), ("support", 2, b"new"), ("support", 1, b"other")):
        with pytest.raises(StateError):
            repository.admit_recovery_support_obligation(
                recovered, kind, version, payload, obligation_id=admitted.obligation_id
            )
    assert replace(old, ownership=recovered) in repository.list_pending_lifecycle_obligations(recovered)
    repository.resolve_lifecycle_obligation(recovered, admitted.obligation_id)
    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(
            recovered, "support", 1, b"new", obligation_id=admitted.obligation_id
        )
    repository.resolve_lifecycle_obligation(recovered, old.obligation_id)
    repository.record_effects_resolved(recovered)
    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(recovered, "support", 1, b"new", obligation_id="d" * 32)


def test_support_admission_rolls_back_row_and_claim_together(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = db.operations
    scope = OperationScope(OperationResourceKind.VM, "support-vm")
    predecessor = repository.claim(scope, "file-upload")
    before = repository.recover_takeover(predecessor, "b" * 32)
    original_load = repository._load_obligation  # noqa: SLF001

    def interrupt_load(*args: object) -> None:
        del args
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(repository, "_load_obligation", interrupt_load)
        with pytest.raises(KeyboardInterrupt):
            repository.admit_recovery_support_obligation(before.ownership, "support", 1, b"new", obligation_id="c" * 32)
    assert repository.inspect(scope) == before
    assert repository.list_pending_lifecycle_obligations(before.ownership) == ()
    admitted = repository.admit_recovery_support_obligation(
        before.ownership, "support", 1, b"new", obligation_id="c" * 32
    )
    assert original_load(before.ownership, admitted.obligation_id) == admitted
    claim = repository.inspect(scope)
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH


def test_support_admission_retains_payload_and_row_bounds(db: Database) -> None:
    repository = db.operations
    predecessor = repository.claim(OperationScope(OperationResourceKind.VM, "support-vm"), "file-upload")
    for index in range(MAX_LIFECYCLE_OBLIGATIONS - 1):
        repository.register_lifecycle_obligation(predecessor, "application", 1, b"", obligation_id=f"{index:032x}")
    recovered = repository.recover_takeover(predecessor, "b" * 32).ownership
    with pytest.raises(ValueError):
        repository.admit_recovery_support_obligation(recovered, "support", 1, b"x" * 8193, obligation_id="c" * 32)
    admitted = repository.admit_recovery_support_obligation(
        recovered, "support", 1, b"x" * 8192, obligation_id="c" * 32
    )
    assert (
        repository.admit_recovery_support_obligation(
            recovered, "support", 1, b"x" * 8192, obligation_id=admitted.obligation_id
        )
        == admitted
    )
    with pytest.raises(StateError):
        repository.admit_recovery_support_obligation(recovered, "support", 1, b"", obligation_id="d" * 32)
    assert len(repository.list_pending_lifecycle_obligations(recovered)) == MAX_LIFECYCLE_OBLIGATIONS
