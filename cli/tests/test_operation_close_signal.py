"""Close intent while lifecycle admission waits on the operation connection."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationOwner

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation, OperationOwnership

# Real SQLite connection serialization and thread teardown must also run on Windows.
pytestmark = pytest.mark.windows


@pytest.mark.parametrize("closing", ["stop", "close"])
@pytest.mark.parametrize("repeated", [False, True], ids=["first-fence", "repeated-fence"])
def test_close_intent_does_not_wait_for_lifecycle_repository_lock(
    db: Database, monkeypatch: pytest.MonkeyPatch, closing: str, *, repeated: bool
) -> None:
    repository = db.operations
    scope = OperationScope(OperationResourceKind.VM, "close-signal-vm")
    owner = OperationOwner.acquire(repository, scope, "file-upload")
    admitted = owner.register_lifecycle_obligation("admitted", payload_version=1, payload=b"prepared")
    pending = owner.register_lifecycle_obligation("pending", payload_version=1, payload=b"prepared")
    borrow = owner.borrow()
    if repeated:
        admitted.mark_possible_effect()
    before_claim = repository.inspect(scope)
    before_ledger = repository.list_lifecycle_obligations(owner.ownership)
    entered = threading.Event()
    stopped = threading.Event()
    fence_finished = threading.Event()
    fence_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    original = repository.mark_lifecycle_obligation_possible_effect

    def entered_repository(ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        entered.set()
        return original(ownership, obligation_id)

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", entered_repository)

    def fence() -> None:
        try:
            admitted.mark_possible_effect()
        except BaseException as error:
            fence_errors.append(error)
        finally:
            fence_finished.set()

    def request_close() -> None:
        try:
            if closing == "stop":
                owner.stop_admission()
            else:
                owner.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            stopped.set()

    worker = threading.Thread(target=fence)
    closer = threading.Thread(target=request_close)
    connection_lock = repository._connection_lock  # noqa: SLF001
    connection_lock.acquire()
    try:
        worker.start()
        assert entered.wait(5)
        guard_acquired = owner._guard.acquire(blocking=False)  # noqa: SLF001
        if guard_acquired:
            owner._guard.release()  # noqa: SLF001
        assert not guard_acquired
        assert not fence_finished.is_set()
        closer.start()
        if closing == "stop":
            assert stopped.wait(5)
        else:
            assert owner._close_requested.wait(5)  # noqa: SLF001
            assert not stopped.is_set()
        assert not fence_finished.is_set()
    finally:
        connection_lock.release()
        if worker.ident is not None:
            worker.join(5)
        if closer.ident is not None:
            closer.join(5)
        assert not worker.is_alive()
        assert not closer.is_alive()

    assert not fence_errors
    if closing == "close":
        assert len(close_errors) == 1 and isinstance(close_errors[0], StateError)
    else:
        assert not close_errors
    assert admitted.state is LifecycleObligationState.POSSIBLE_EFFECT
    after_claim = repository.inspect(scope)
    after_ledger = repository.list_lifecycle_obligations(owner.ownership)
    assert after_claim is not None and after_claim.state is OperationClaimState.POSSIBLE_DISPATCH
    assert {item.obligation_id for item in after_ledger} == {item.obligation_id for item in before_ledger}
    if repeated:
        assert after_claim == before_claim
        assert after_ledger == before_ledger

    owner.stop_admission()
    with pytest.raises(StateError):
        admitted.mark_possible_effect()
    with pytest.raises(StateError):
        pending.mark_possible_effect()
    with pytest.raises(StateError):
        borrow.begin_attempt()
    borrow.close()
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.register_lifecycle_obligation("late", payload_version=1, payload=b"")
    assert repository.inspect(scope) == after_claim
    assert repository.list_lifecycle_obligations(owner.ownership) == after_ledger
    admitted.resolve()
    pending.resolve()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert repository.inspect(scope) is None


@pytest.mark.parametrize("committed", [False, True], ids=["supplied", "installed"])
def test_closing_retains_prior_dispatch_registration_uncertainty(
    db: Database, monkeypatch: pytest.MonkeyPatch, *, committed: bool
) -> None:
    repository = db.operations
    scope = OperationScope(OperationResourceKind.VM, "closing-registration-vm")
    owner = OperationOwner.acquire(repository, scope, "file-upload")
    borrow = owner.borrow()
    original = repository.register_lifecycle_obligation
    obligation_id = "a" * 32

    def lost_registration(
        ownership: OperationOwnership,
        obligation_kind: str,
        payload_version: int,
        payload: bytes,
        *,
        obligation_id: str | None = None,
    ) -> LifecycleObligation:
        if committed:
            original(ownership, obligation_kind, payload_version, payload, obligation_id=obligation_id)
        raise RuntimeError("registration reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "register_lifecycle_obligation", lost_registration)
        with pytest.raises(RuntimeError):
            borrow.install_dispatch_obligation(
                obligation_id, "adapter-dispatch", payload_version=1, payload=b"prepared"
            )
    before = repository.list_lifecycle_obligations(owner.ownership)
    assert len(before) == int(committed)
    owner.stop_admission()
    with pytest.raises(StateError) as refused:
        borrow.install_dispatch_obligation(obligation_id, "adapter-dispatch", payload_version=1, payload=b"prepared")
    assert type(refused.value) is StateError
    assert repository.list_lifecycle_obligations(owner.ownership) == before
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()
    assert repository.list_lifecycle_obligations(owner.ownership) == before
