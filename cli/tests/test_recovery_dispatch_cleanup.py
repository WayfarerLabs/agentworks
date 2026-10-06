"""Exact local custody cleanup after interrupted recovery open and close."""

from __future__ import annotations

import sys
from types import FrameType
from typing import Any

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.operations import OperationOwner, RecoveredLifecycleObligation, RecoveryDispatch

pytestmark = pytest.mark.windows


def _recovery(db: Database):
    predecessor = OperationOwner.acquire(
        db.operations, OperationScope(OperationResourceKind.VM, "cleanup-vm"), "cleanup"
    )
    owner = OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)
    row = owner.admit_recovery_support_obligation(
        "carrier-dispatch", payload_version=1, payload=b"", obligation_id="c" * 32
    )
    binding = _binding(owner, row.obligation_id)
    return owner, row, binding


def _binding(owner: OperationOwner, obligation_id: str) -> RecoveredLifecycleObligation:
    return owner.rebind_possible_effect_lifecycle_obligation(
        obligation_id, "carrier-dispatch", payload_version=1, payload=b"", payload_revision=0
    )


@pytest.mark.parametrize("activated", [False, True])
def test_unreturned_open_retains_exact_handle_before_activation(db: Database, activated: bool) -> None:
    owner, row, binding = _recovery(db)

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        local = binding._local_dispatch  # noqa: SLF001
        if (
            event == "line"
            and frame.f_code is RecoveredLifecycleObligation.open_dispatch.__code__
            and local is not None
            and (owner._active_recovery_dispatch is local) is activated  # noqa: SLF001
        ):
            raise KeyboardInterrupt
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            binding.open_dispatch()
    finally:
        sys.settrace(None)
    assert binding._local_dispatch is not None  # noqa: SLF001
    binding._close_retained_dispatch()  # noqa: SLF001
    assert owner._active_recovery_dispatch is None  # noqa: SLF001
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT


def test_partial_close_retry_finishes_only_same_settled_dispatch(db: Database) -> None:
    owner, row, binding = _recovery(db)
    dispatch = binding.open_dispatch()

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        if (
            event == "line"
            and frame.f_code is RecoveryDispatch.close.__code__
            and dispatch._close_started  # noqa: SLF001
            and owner._active_recovery_dispatch is None  # noqa: SLF001
            and not dispatch._closed  # noqa: SLF001
        ):
            raise KeyboardInterrupt
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            dispatch.close()
    finally:
        sys.settrace(None)
    dispatch.close()
    dispatch.close()
    assert owner._active_recovery_dispatch is None  # noqa: SLF001
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        dispatch.begin_attempt()


def test_cleanup_refuses_other_dispatcher_and_received_attempt(db: Database) -> None:
    owner, row, binding = _recovery(db)
    dispatch = binding.open_dispatch()
    other_binding = RecoveredLifecycleObligation(owner, row._persisted_obligation)  # noqa: SLF001
    other_dispatch = RecoveryDispatch(owner, row._persisted_obligation)  # noqa: SLF001
    for cleanup in (other_binding._close_retained_dispatch, other_dispatch.close):  # noqa: SLF001
        with pytest.raises(StateError):
            cleanup()
        assert owner._active_recovery_dispatch is dispatch  # noqa: SLF001
    attempt = dispatch.begin_attempt()
    for cleanup in (binding._close_retained_dispatch, dispatch.close):  # noqa: SLF001
        with pytest.raises(StateError):
            cleanup()
        assert owner._outstanding_attempt is attempt  # noqa: SLF001
        assert not dispatch._close_started  # noqa: SLF001
    attempt.settle()
    dispatch.close()
    replacement = _binding(owner, row.obligation_id).open_dispatch()
    replacement_attempt = replacement.begin_attempt()
    for cleanup in (binding._close_retained_dispatch, dispatch.close):  # noqa: SLF001
        with pytest.raises(StateError):
            cleanup()
        assert owner._active_recovery_dispatch is replacement  # noqa: SLF001
        assert owner._outstanding_attempt is replacement_attempt  # noqa: SLF001
    replacement.handoff_unresolved()
    with pytest.raises(StateError):
        dispatch.close()
    assert owner._outstanding_attempt is replacement_attempt  # noqa: SLF001


def test_closed_local_dispatch_cannot_resolve_after_takeover(db: Database) -> None:
    owner, row, binding = _recovery(db)
    dispatch = binding.open_dispatch()
    dispatch.close()
    successor = OperationOwner.recover(db.operations, owner.ownership, "d" * 32)
    successor_dispatch = _binding(successor, row.obligation_id).open_dispatch()
    binding._close_retained_dispatch()  # noqa: SLF001
    with pytest.raises(StateError):
        row.resolve()
    assert successor._active_recovery_dispatch is successor_dispatch  # noqa: SLF001
    assert successor.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


def test_reused_binding_failed_open_preserves_earlier_callers_dispatch(db: Database) -> None:
    owner, _, binding = _recovery(db)
    earlier = binding.open_dispatch()
    with pytest.raises(StateError):
        binding.open_dispatch()
    with pytest.raises(StateError):
        binding._close_retained_dispatch()  # noqa: SLF001
    attempt = earlier.begin_attempt()
    with pytest.raises(StateError):
        binding._close_retained_dispatch()  # noqa: SLF001
    attempt.settle()
    earlier.close()


@pytest.mark.parametrize("phase", ["before", "intent", "attempt", "active", "closed"])
def test_unreturned_abort_retries_across_exact_local_mutations(db: Database, phase: str) -> None:
    owner, row, binding = _recovery(db)
    dispatch = binding.open_dispatch()
    attempt = dispatch.begin_attempt()
    error = KeyboardInterrupt()

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        if frame.f_code is RecoveryDispatch._abort_unreturned_attempt.__code__:
            states = {
                "before": not dispatch._close_started,
                "intent": dispatch._close_started and owner._outstanding_attempt is attempt,
                "attempt": dispatch._close_started
                and owner._outstanding_attempt is None
                and owner._active_recovery_dispatch is dispatch,
                "active": owner._active_recovery_dispatch is None and not dispatch._closed,
                "closed": dispatch._closed,
            }
            boundary = event == ("return" if phase == "closed" else "line")
            if boundary and states[phase]:
                raise error
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            dispatch._abort_unreturned_attempt()
        assert caught.value is error
    finally:
        sys.settrace(None)
    dispatch._abort_unreturned_attempt()
    dispatch._abort_unreturned_attempt()
    assert owner._active_recovery_dispatch is None and owner._outstanding_attempt is None
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    replacement = _binding(owner, row.obligation_id).open_dispatch()
    replacement.close()


@pytest.mark.parametrize("handoff", [False, True])
def test_abort_retry_refuses_conflicting_dispatch_and_attempt(db: Database, handoff: bool) -> None:
    owner, row, binding = _recovery(db)
    dispatch = binding.open_dispatch()
    dispatch.begin_attempt()
    dispatch._abort_unreturned_attempt()
    replacement = _binding(owner, row.obligation_id).open_dispatch()
    attempt = replacement.begin_attempt()
    if handoff:
        replacement.handoff_unresolved()
    with pytest.raises(StateError):
        dispatch._abort_unreturned_attempt()
    assert owner._outstanding_attempt is attempt
    assert owner._active_recovery_dispatch is (None if handoff else replacement)
    if not handoff:
        attempt.settle()
        replacement.close()


def test_abort_cannot_release_handed_off_effect_without_exact_close_intent(db: Database) -> None:
    owner, _, binding = _recovery(db)
    dispatch = binding.open_dispatch()
    attempt = dispatch.begin_attempt()
    dispatch.handoff_unresolved()
    with pytest.raises(StateError):
        dispatch._abort_unreturned_attempt()
    assert owner._outstanding_attempt is attempt and not dispatch._close_started
