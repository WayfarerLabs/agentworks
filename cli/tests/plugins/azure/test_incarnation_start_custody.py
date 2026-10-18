"""Fenced v2 publication and no-effect settlement edge cases."""

from __future__ import annotations

import time

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState
from agentworks.errors import ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure._activation import StartAcknowledgment
from agentworks.plugins.azure._incarnation_start_payload import (
    IncarnationStartPayload,
    StartState,
    encode_start_payload,
)
from tests.plugins.azure.test_activation_sdk import deny
from tests.plugins.azure.test_incarnation_start import OPERATION, RESOURCE, VM_ID, Control, owned, row, start_async

__all__ = ["owned"]


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_terminal_publication_reply_loss_does_not_republish_ack(owned, monkeypatch, committed, error_type):
    original = owned.repository.publish_lifecycle_obligation_payload
    calls = []
    primary = error_type("lost terminal publication")

    def publish(*args, **kwargs):
        calls.append(kwargs["expected_revision"])
        if kwargs["expected_revision"] == 1:
            if committed:
                original(*args, **kwargs)
            raise primary
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", publish)
    owned.transport.replies.append((200, {}, deny))
    with pytest.raises(error_type) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    assert row(owned).payload_revision == (2 if committed else 1)
    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", original)
    assert owned.adapter.settle(Deadline.after(5))
    assert row(owned).payload_revision == 2 and row(owned).state is LifecycleObligationState.RESOLVED
    assert calls == [0, 1] and len(owned.transport.calls) == 1


def test_late_registration_never_marked_resolves_without_provider_work(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    original = owned.repository.register_lifecycle_obligation

    def late(*args, **kwargs):
        registered = original(*args, **kwargs)
        clock[0] = 102.0
        return registered

    monkeypatch.setattr(owned.repository, "register_lifecycle_obligation", late)
    with pytest.raises(TimeoutError):
        owned.adapter.start(Deadline.after(1))
    assert row(owned).state is LifecycleObligationState.REGISTERED
    assert owned.adapter.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.RESOLVED
    assert owned.transport.calls == [] and owned.constructed == []


@pytest.mark.parametrize("status", [202, 204, 201, True])
def test_async_monitor_requires_exact_http_200(owned, status):
    start_async(owned)
    owned.transport.replies.append((status, {}, [b'{"status":"Succeeded"}']))
    with pytest.raises(ValidationError):
        owned.adapter.observe(Deadline.after(5))
    assert owned.transport.responses[-1].iterations == 0
    assert not owned.adapter.settle(Deadline.after(5))


def test_v2_ack_reserves_capacity_for_terminal_fact(owned):
    ack = StartAcknowledgment(202, location=OPERATION)
    base = IncarnationStartPayload(RESOURCE, VM_ID, ack)
    padding = MAX_LIFECYCLE_PAYLOAD_BYTES - len(encode_start_payload(base)) - 1
    endpoint = OPERATION + "x" * padding
    owned.transport.replies.append((202, {"Location": endpoint}, deny))
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(5))
    assert owned.adapter.state is StartState.UNKNOWN
    assert not owned.adapter.settle(Deadline.after(5))
