"""Foreground EC2 startup observation and fresh-budget cleanup of an existing start."""

from __future__ import annotations

import pytest

from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import LimitExceededError, StateError
from agentworks.execution.carrier import Deadline
from tests.plugins.test_aws_native_access_sdk import native_aws as native_aws


@pytest.mark.parametrize("states", [("pending", "running"), ("stopped", "pending", "running")])
def test_sdk_foreground_start_observes_transitions_before_guest_or_route(native_aws, states):
    p = native_aws
    p.state = "stopped"
    remaining = list(states)
    observed = []

    def before(action):
        activation = p.access._activation
        if action == "DescribeInstances" and activation is not None and activation.payload.request_id is not None:
            if remaining:
                p.state = remaining.pop(0)
                observed.append(p.state)
                assert p.access._route is None and not p.preparations
            if p.state != "running":
                row = p.owner.inspect_lifecycle_obligation(activation.obligation_id)
                assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
                assert p.access._running is None
        elif action != "StartInstances" and action != "DescribeInstances":
            assert not remaining and p.state == "running"

    p.before_send = before
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert observed == list(states)
    assert len(p.pauses) == len(states) - 1
    assert sum(action == "StartInstances" for action, _ in p.calls) == 1
    row = p.owner.inspect_lifecycle_obligation(p.access._activation.obligation_id)
    assert row.state is LifecycleObligationState.RESOLVED
    assert p.access._running is not None and len(p.preparations) == 1
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("state", ["unknown", "shutting-down", ""])
def test_sdk_unsupported_start_observation_refuses_without_waiting(native_aws, state):
    p = native_aws
    p.state = "stopped"
    p.after_send = lambda action: setattr(p, "state", state) if action == "StartInstances" else None
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert not p.pauses and p.access._running is None
    assert not p.preparations and p.access._route is None
    row = p.owner.inspect_lifecycle_obligation(p.access._activation.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("late_cleanup", [False, True])
def test_sdk_fresh_cleanup_settles_existing_ack_without_reopening_preparation(native_aws, monkeypatch, late_cleanup):
    p = native_aws
    p.state = "stopped"
    p.after_send = lambda action: setattr(p, "state", "pending") if action == "StartInstances" else None
    resolutions = []
    resolve = p.repository.resolve_lifecycle_obligation

    def observed_resolve(*args, **kwargs):
        row = resolve(*args, **kwargs)
        resolutions.append(row.obligation_id)
        return row

    monkeypatch.setattr(p.repository, "resolve_lifecycle_obligation", observed_resolve)
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    activation = p.access._activation
    assert p.deadline.expired and activation.payload.request_id is not None
    obligation_id = activation.obligation_id
    assert not resolutions and p.access._running is None
    assert not p.preparations and p.access._route is None and p.access.binding is None

    p.state = "running"
    p.after_send = lambda action: None
    cleanup = Deadline.after(5)
    if late_cleanup:
        expires = cleanup.expires_at
        assert expires is not None
        p.after_send = lambda action: p.clock.__setitem__(0, expires + 1) if action == "DescribeInstances" else None
        with pytest.raises(LimitExceededError):
            p.access.settle(cleanup)
        assert p.access._running is None and not resolutions
        row = p.owner.inspect_lifecycle_obligation(obligation_id)
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        p.after_send = lambda action: None
        cleanup = Deadline.after(5)
    assert p.access.settle(cleanup)
    assert resolutions == [obligation_id]
    assert p.access.settle(cleanup)
    assert resolutions == [obligation_id]
    assert sum(action == "StartInstances" for action, _ in p.calls) == 1
    assert not p.preparations and p.access._route is None and p.access.binding is None
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, cleanup)


def test_sdk_fresh_cleanup_running_without_ack_remains_unresolved(native_aws):
    p = native_aws
    p.state = "stopped"

    def lose_ack(action):
        if action == "StartInstances":
            raise ConnectionError("lost start response")

    p.after_send = lose_ack
    with pytest.raises(ConnectionError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    p.clock[0] = p.deadline.expires_at + 1
    p.after_send = lambda action: None
    assert p.state == "running"
    assert not p.access.settle(Deadline.after(5))
    assert p.access._running is None
    assert not p.preparations and p.access._route is None
    row = p.owner.inspect_lifecycle_obligation(p.access._activation.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert sum(action == "StartInstances" for action, _ in p.calls) == 1
