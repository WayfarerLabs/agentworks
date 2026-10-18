"""Foreground EC2 startup observation and fresh-budget cleanup of an existing start."""

from __future__ import annotations

import pytest

from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import AuthorizationError, LimitExceededError, StateError, TokenRejectedError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws.network import EC2Error
from tests.plugins.test_aws_native_access_sdk import enable_role
from tests.plugins.test_aws_native_access_sdk import inherited_aws as inherited_aws
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
    from botocore.exceptions import EndpointConnectionError

    p = native_aws
    p.state = "stopped"

    def lose_ack(action):
        if action == "StartInstances":
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    p.after_send = lose_ack
    with pytest.raises(EC2Error):
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


@pytest.mark.parametrize("fault", ["expired", "control"])
def test_stored_activation_without_admission_can_settle(native_aws, monkeypatch, fault):
    p = native_aws
    p.state = "stopped"
    transition = p.access._active_transition
    control = KeyboardInterrupt("before admission")

    def refuse(action, deadline):
        assert p.access._activation is not None
        if fault == "control":
            raise control
        p.clock[0] = 106.0
        return transition(action, deadline)

    monkeypatch.setattr(p.access, "_active_transition", refuse)
    with pytest.raises(KeyboardInterrupt if fault == "control" else LimitExceededError) as caught:
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    if fault == "control":
        assert caught.value is control
    activation = p.access._activation
    assert not activation.closed_without_start_attempt
    monkeypatch.setattr(activation, "reconcile", lambda deadline: pytest.fail("never-admitted producer reconciled"))
    assert p.access.settle(Deadline.after(5))
    assert activation.closed_without_start_attempt
    with pytest.raises(StateError):
        activation.start(Deadline.after(5))
    assert not p.owner.list_pending_lifecycle_obligations()
    assert not any(action == "StartInstances" for action, _ in p.calls)


def test_stopped_read_close_debt_cannot_admit_start(native_aws):
    p = native_aws
    p.state = "stopped"
    p.close_error = OSError("read close")
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    original = p.access._read_client
    assert original is not None and p.access._activation is None
    p.close_error = None
    assert p.access.settle(p.deadline)
    assert p.closes[-1] is original
    assert not any(action == "StartInstances" for action, _ in p.calls)


@pytest.mark.parametrize("error", [ConnectionError("database"), StateError("database")])
def test_failed_start_registration_is_not_proven_absence_or_provider_error(native_aws, monkeypatch, error):
    p = native_aws
    p.state = "stopped"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(p.owner, "register_lifecycle_obligation", fail)
    with pytest.raises(type(error)) as caught:
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert caught.value is error
    assert not p.access._activation.closed_without_start_attempt
    with pytest.raises(StateError):
        p.access.settle(p.deadline)
    assert not p.access._activation.closed_without_start_attempt
    assert not any(action == "StartInstances" for action, _ in p.calls)


@pytest.mark.parametrize("fault", ["ordinary", "expired", "control"])
def test_settled_effects_retry_only_original_final_closure(native_aws, monkeypatch, fault):
    p = native_aws
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    originals = []
    control = KeyboardInterrupt("final close")
    if fault == "ordinary":

        def close_hook(client):
            originals.append(client)
            raise OSError("route cleanup close")

        p.close_hook = close_hook
        assert not p.access.settle(p.deadline)
        assert p.access._route._client is originals[0]
    else:
        close_clients = p.access._close_clients

        def final_close():
            close_clients()
            if p.access._effects_settled:
                if fault == "expired":
                    p.clock[0] = 106.0
                else:
                    raise control

        monkeypatch.setattr(p.access, "_close_clients", final_close)
        with pytest.raises(LimitExceededError if fault == "expired" else KeyboardInterrupt) as caught:
            p.access.settle(p.deadline)
        if fault == "control":
            assert caught.value is control
        monkeypatch.setattr(p.access, "_close_clients", close_clients)
    assert p.access._effects_settled and p.access._closed
    calls = list(p.calls)
    p.close_hook = lambda client: None
    monkeypatch.setattr(p.access._route, "settle", lambda deadline: pytest.fail("effect work replayed"))
    assert p.access.settle(Deadline.after(5))
    assert p.calls == calls
    if originals:
        assert p.closes[-1] is originals[0]


@pytest.mark.parametrize("state", ["stopped", "stopping", "unknown", ""])
@pytest.mark.parametrize("stage", ["initial", "endpoint", "after-start"])
def test_fresh_nonrunning_state_blocks_route_and_guest(native_aws, state, stage):
    p = native_aws
    power = VMStatus.STOPPED if stage == "after-start" else VMStatus.RUNNING
    p.state = "stopped" if power is VMStatus.STOPPED else "running"
    reads = [0]

    def before(action):
        if action == "DescribeInstances":
            reads[0] += 1
            if reads[0] == {"initial": 1, "endpoint": 2, "after-start": 3}[stage]:
                p.state = state

    p.before_send = before
    with pytest.raises(StateError):
        p.access.prepare(power, p.deadline)
    assert not p.preparations and p.access._route is None
    assert not any(action == "AuthorizeSecurityGroupIngress" for action, _ in p.calls)
    assert sum(action == "StartInstances" for action, _ in p.calls) == (stage == "after-start")
    assert p.access.settle(p.deadline)


def test_bound_guest_observer_refuses_read_close_debt_before_guest(native_aws):
    p = native_aws

    def after(action):
        if action == "DescribeInstances" and p.access._route is not None:
            p.close_error = OSError("guest observer close")

    p.after_send = after
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access._read_client is not None and not p.preparations
    p.close_error = None
    p.after_send = lambda action: None
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("role", [False, True])
def test_poisoned_inherited_profile_endpoints_and_configs_are_ignored(native_aws, monkeypatch, role):
    p = native_aws
    if role:
        enable_role(p, monkeypatch)
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.settle(p.deadline)
    assert sum(action == "AssumeRole" for action, _ in p.calls) == int(role)


@pytest.mark.parametrize(
    "action",
    [
        "StartInstances",
        "DescribeSecurityGroups",
        "DescribeSecurityGroupRules",
        "RevokeSecurityGroupIngress",
        "AuthorizeSecurityGroupIngress",
    ],
)
@pytest.mark.parametrize(
    "code,kind", [("UnauthorizedOperation", AuthorizationError), ("InvalidClientTokenId", TokenRejectedError)]
)
def test_active_sdk_errors_have_public_domain_types_without_replay(native_aws, action, code, kind):
    p = native_aws
    p.api_error = action, code
    power = VMStatus.STOPPED if action == "StartInstances" else VMStatus.RUNNING
    p.state = "stopped" if power is VMStatus.STOPPED else "running"
    with pytest.raises(kind):
        p.access.prepare(power, p.deadline)
    before = sum(sent == action for sent, _ in p.calls)
    assert before == 1 and not p.preparations
    p.api_error = None
    assert p.access.settle(p.deadline) is (action not in ("StartInstances", "AuthorizeSecurityGroupIngress"))
    assert sum(sent == action for sent, _ in p.calls) == before


@pytest.mark.parametrize(
    "code,kind", [("UnauthorizedOperation", AuthorizationError), ("InvalidClientTokenId", TokenRejectedError)]
)
def test_cleanup_sdk_denial_keeps_owned_rule_retryable(native_aws, code, kind):
    p = native_aws
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    p.api_error = "RevokeSecurityGroupIngress", code
    with pytest.raises(kind):
        p.access.settle(p.deadline)
    assert not p.access._effects_settled
    p.api_error = None
    assert p.access.settle(p.deadline)
    assert sum(action == "AuthorizeSecurityGroupIngress" for action, _ in p.calls) == 1
