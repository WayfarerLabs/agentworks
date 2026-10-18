"""Fenced v2 publication and no-effect settlement edge cases."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure._activation import StartAcknowledgment
from agentworks.plugins.azure._incarnation_start_payload import (
    IncarnationStartPayload,
    StartState,
    encode_start_payload,
)
from tests.plugins.azure.test_activation_sdk import deny
from tests.plugins.azure.test_incarnation_start import (
    OPERATION,
    RESOURCE,
    VM_ID,
    Control,
    MonitorResponse,
    owned,
    row,
    start_async,
)

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


def test_late_async_headers_do_not_create_body_iterator(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    start_async(owned)
    iterations = []

    def forbidden_iterator(response):
        iterations.append(response)
        raise AssertionError("body iterator admitted after deadline")

    monkeypatch.setattr(MonitorResponse, "iter_bytes", forbidden_iterator)
    owned.transport.hook = lambda response: clock.__setitem__(0, 102.0)
    owned.transport.replies.append((200, {}, deny))
    with pytest.raises(TimeoutError):
        owned.adapter.observe(Deadline.after(1))
    assert iterations == [] and owned.adapter.state is StartState.PENDING
    assert owned.adapter.cleanup_incomplete and len(owned.transport.calls) == 2
    assert not owned.adapter.settle(Deadline.after(5))
    assert not owned.adapter.cleanup_incomplete and len(owned.transport.calls) == 2
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_late_iterator_creation_does_not_admit_first_next(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    start_async(owned)
    advanced = []

    def body() -> Iterator[bytes]:
        advanced.append(True)
        yield b'{"status":"Succeeded"}'

    def late_iterator(response):
        clock[0] = 102.0
        return body()

    monkeypatch.setattr(MonitorResponse, "iter_bytes", late_iterator)
    owned.transport.replies.append((200, {}, deny))
    with pytest.raises(TimeoutError):
        owned.adapter.observe(Deadline.after(1))
    assert advanced == [] and owned.adapter.state is StartState.PENDING
    assert not owned.adapter.settle(Deadline.after(5))
    assert len(owned.transport.calls) == 2


@pytest.mark.parametrize("revision", [0, 1])
def test_resolved_row_must_match_highest_retained_resolution_intent(owned, monkeypatch, revision):
    owned.transport.replies.append((200, {}, deny))
    owned.adapter.start(Deadline.after(5))
    resolved = row(owned)
    assert resolved.state is LifecycleObligationState.RESOLVED and resolved.payload_revision == 2
    payload = replace(owned.adapter.payload, terminal_status=None)
    if revision == 0:
        payload = replace(payload, acknowledgment=None)
    inconsistent = replace(resolved, payload_revision=revision, payload=encode_start_payload(payload))
    monkeypatch.setattr(owned.owner, "inspect_lifecycle_obligation", lambda obligation_id: inconsistent)
    with pytest.raises(StateError):
        owned.adapter.settle(Deadline.after(5))
    assert len(owned.transport.calls) == 1


def test_premature_matching_resolution_is_not_our_resolution_intent(owned):
    start_async(owned)
    owned.adapter.obligation.resolve()
    with pytest.raises(StateError):
        owned.adapter.settle(Deadline.after(5))
