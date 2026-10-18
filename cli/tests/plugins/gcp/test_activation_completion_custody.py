"""Uncertain completion bookkeeping, unused close and control boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import LimitExceededError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation import ActivationState, GCEActivation
from tests.plugins.gcp.test_activation_completion import completion, receipt
from tests.plugins.gcp.test_activation_sdk import isolated_config as isolated_config
from tests.plugins.gcp.test_activation_sdk import sdk as sdk


def test_unused_settle_requires_closed_admission_and_never_uses_ledger_or_auth(sdk, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unused settlement touched ledger or authentication")

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", forbidden)
    monkeypatch.setattr(OperationOwner, "register_lifecycle_obligation", forbidden)
    monkeypatch.setattr(sdk.auth, "begin", forbidden)
    assert not sdk.adapter.settle(Deadline.after(5))
    assert sdk.adapter.close(Deadline.after(5))
    assert sdk.adapter.settle(Deadline.after(5))
    assert sdk.adapter.settle(Deadline.after(5))
    for action in (sdk.adapter.start, sdk.adapter.observe):
        with pytest.raises(StateError):
            action(Deadline.after(5))
    assert not sdk.sessions and sdk.adapter.state is ActivationState.UNKNOWN


def test_unused_settlement_waits_for_actual_original_retirement(sdk):
    def fail():
        raise OSError("not retired")

    original = SimpleNamespace(close=fail)
    sdk.adapter._response = original
    assert not sdk.adapter.close(Deadline.after(5))
    assert not sdk.adapter.settle(Deadline.after(5))
    assert sdk.adapter._response is original and not sdk.adapter._settled
    original.close = lambda: None
    assert sdk.adapter.settle(Deadline.after(5))
    assert sdk.adapter._response is None


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_ack_publication_can_be_repaired_before_one_get(sdk, monkeypatch, committed, error_type):
    original = LifecycleObligation.publish_payload

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise error_type("lost ACK publication")

    monkeypatch.setattr(LifecycleObligation, "publish_payload", fail)
    with pytest.raises(error_type):
        sdk.adapter.start(Deadline.after(5))
    assert sdk.adapter.state is ActivationState.PENDING
    assert sdk.adapter.payload.operation_name == "Operation_1"
    sdk.data = json.dumps(completion(sdk.adapter.payload)).encode()
    if not committed:
        with pytest.raises(error_type):
            sdk.adapter.observe(Deadline.after(5))
        assert len(sdk.service_calls) == 1
    monkeypatch.setattr(LifecycleObligation, "publish_payload", original)
    assert sdk.adapter.observe(Deadline.after(5)) is ActivationState.SUCCEEDED
    assert receipt(sdk).payload_revision == 1
    assert sdk.adapter.settle(Deadline.after(5))
    assert [method for method, _ in sdk.service_calls] == ["POST", "GET"]


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_validated_terminal_survives_settlement_ledger_failure(sdk, monkeypatch, error_type):
    sdk.adapter.start(Deadline.after(5))
    sdk.data = json.dumps(completion(sdk.adapter.payload)).encode()
    original = OperationOwner.inspect_lifecycle_obligation
    primary = error_type("terminal settlement bookkeeping")

    def inspect(owner, obligation_id):
        if sdk.adapter.state is ActivationState.SUCCEEDED:
            raise primary
        return original(owner, obligation_id)

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", inspect)
    assert sdk.adapter.observe(Deadline.after(5)) is ActivationState.SUCCEEDED
    with pytest.raises(error_type) as caught:
        sdk.adapter.settle(Deadline.after(5))
    assert caught.value is primary and sdk.adapter.state is ActivationState.SUCCEEDED
    assert not sdk.adapter.cleanup_incomplete
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", original)
    assert sdk.adapter.settle(Deadline.after(5))
    assert len(sdk.service_calls) == 2


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
def test_get_original_control_survives_other_cleanup_controls(sdk, primary_type):
    sdk.adapter.start(Deadline.after(5))
    sdk.data = json.dumps(completion(sdk.adapter.payload)).encode()
    primary = primary_type("GET body read")

    def read(data):
        raise primary

    def fail(label, original, normal):
        raise (KeyboardInterrupt if primary_type is SystemExit else SystemExit)("cleanup")

    sdk.before_close = fail
    sdk.read_callback = read
    with pytest.raises(primary_type) as caught:
        sdk.adapter.observe(Deadline.after(5))
    assert caught.value is primary
    assert sdk.adapter._response is sdk.responses[-1]
    assert sdk.adapter._service_session is sdk.service_session
    assert not sdk.adapter._lock.locked() and not sdk.auth.closed
    assert sdk.adapter.state is ActivationState.PENDING
    sdk.before_close = None
    assert not sdk.adapter.settle(Deadline.after(5))
    assert not sdk.adapter.cleanup_incomplete and len(sdk.service_calls) == 2


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_after_ack_payload_retention_preserves_observable_pending(sdk, monkeypatch, primary_type):
    primary = primary_type("after ACK payload retention")
    fired: list[bool] = []

    def retain_then_interrupt(self, name, value):
        object.__setattr__(self, name, value)
        if self is sdk.adapter and name == "_payload" and value.operation_name is not None and not fired:
            fired.append(True)
            raise primary

    with monkeypatch.context() as patch:
        patch.setattr(GCEActivation, "__setattr__", retain_then_interrupt)
        with pytest.raises(primary_type) as caught:
            sdk.adapter.start(Deadline.after(5))
    assert caught.value is primary and fired == [True]
    assert sdk.adapter.payload.operation_name == "Operation_1"
    assert sdk.adapter.state is ActivationState.PENDING and not sdk.adapter.cleanup_incomplete
    sdk.adapter.reconcile(Deadline.after(5))
    sdk.data = json.dumps(completion(sdk.adapter.payload)).encode()
    assert sdk.adapter.observe(Deadline.after(5)) is ActivationState.SUCCEEDED
    assert sdk.adapter.settle(Deadline.after(5))
    with pytest.raises(StateError):
        sdk.adapter.start(Deadline.after(5))
    assert [method for method, _ in sdk.service_calls] == ["POST", "GET"]


def test_close_pending_ack_bars_get_and_preserves_debt(sdk):
    sdk.adapter.start(Deadline.after(5))
    assert sdk.adapter.close(Deadline.after(5))
    with pytest.raises(StateError):
        sdk.adapter.observe(Deadline.after(5))
    assert not sdk.adapter.settle(Deadline.after(5))
    assert receipt(sdk).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert sdk.adapter.state is ActivationState.PENDING and len(sdk.service_calls) == 1


def test_expired_observation_or_settlement_does_not_dispatch(sdk):
    sdk.adapter.start(Deadline.after(5))
    for action in (sdk.adapter.observe, sdk.adapter.settle):
        with pytest.raises(LimitExceededError):
            action(Deadline.after(0))
    assert len(sdk.service_calls) == 1 and sdk.adapter.state is ActivationState.PENDING


def test_fence_takeover_refuses_get_and_terminal_settlement(sdk):
    sdk.adapter.start(Deadline.after(5))
    OperationOwner.recover(sdk.owner._repository, sdk.owner.ownership, "b" * 32)
    for action in (sdk.adapter.observe, sdk.adapter.settle):
        with pytest.raises(StateError):
            action(Deadline.after(5))
    assert len(sdk.service_calls) == 1 and sdk.adapter.state is ActivationState.PENDING
