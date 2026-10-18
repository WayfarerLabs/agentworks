"""No-entry stage proof survives an interrupted serial-borrow return."""

from __future__ import annotations

import pytest

from agentworks.errors import StateError
from agentworks.execution._prepared_stage_closing import PreparedStageCleanup
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationBorrow
from tests.execution.test_prepared_helper_stage import pending, stage
from tests.execution.test_prepared_stage_closing import owned as owned

# Borrow revocation and interrupted SQLite bookkeeping are host-sensitive.
pytestmark = pytest.mark.windows


@pytest.mark.parametrize("phase", ["begin", "chunk"])
def test_lost_borrow_reply_retains_no_entry_proof_until_core_retires_pristine_borrow(
    owned, monkeypatch, phase: str
) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    cleanup = PreparedStageCleanup(prepared)
    if phase == "chunk":
        assert prepared.begin()
    calls = len(carrier.requests)
    original = owner.borrow
    control = KeyboardInterrupt()
    orphan: list[OperationBorrow] = []

    def lost_reply():
        orphan.append(original())
        raise control

    monkeypatch.setattr(owner, "borrow", lost_reply)
    with pytest.raises(KeyboardInterrupt) as caught:
        if phase == "begin":
            prepared.begin()
        else:
            prepared.append(b"abc")
    assert caught.value is control and len(carrier.requests) == calls
    assert prepared.helper_uncertain and prepared.failed and prepared._active is None
    with pytest.raises(StateError):
        prepared.observe_cleanup(Deadline.after(30))
    assert not prepared.cleaned and len(carrier.requests) == calls
    owner.stop_admission()
    owner.retire_unused_borrow()
    assert orphan[0].closed
    prepared.observe_cleanup(Deadline.after(30))
    assert not prepared.helper_uncertain and prepared.failed
    assert len(carrier.requests) == calls
    if phase == "begin":
        assert prepared.cleaned and pending(owner) == []
    else:
        assert not prepared.cleaned and prepared.cleanup_debt is not None
        assert cleanup.finish(Deadline.after(30))
        assert len(carrier.requests) == calls + 1 and pending(owner) == []


@pytest.mark.parametrize("committed", [False, True])
def test_known_no_entry_resolution_lost_reply_never_needs_remote_absence_inference(
    owned, monkeypatch, committed: bool
) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    original = owner.borrow

    def lost_reply():
        original()
        raise KeyboardInterrupt()

    monkeypatch.setattr(owner, "borrow", lost_reply)
    with pytest.raises(KeyboardInterrupt):
        prepared.begin()
    owner.stop_admission()
    owner.retire_unused_borrow()
    assert prepared._obligation is not None
    obligation = prepared._obligation
    resolve = LifecycleObligation.resolve
    control = KeyboardInterrupt()
    interrupted = False

    def interrupted_resolution(self):
        nonlocal interrupted
        if self is obligation and not interrupted:
            interrupted = True
            if committed:
                resolve(self)
            raise control
        return resolve(self)

    monkeypatch.setattr(LifecycleObligation, "resolve", interrupted_resolution)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.observe_cleanup(Deadline.after(30))
    assert caught.value is control
    assert prepared.cleaned and prepared.cleanup_resolution_uncertain
    prepared.observe_cleanup(Deadline.after(30))
    assert prepared.cleaned and not prepared.cleanup_resolution_uncertain and not prepared.helper_uncertain
    assert carrier.requests == [] and pending(owner) == []
