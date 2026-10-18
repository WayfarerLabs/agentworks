"""One-shot EC2 acknowledgment custody against the actual SQLite ledger."""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import (
    MAX_LIFECYCLE_PAYLOAD_BYTES,
    LifecycleObligation,
    LifecycleObligationState,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.aws._activation import (
    OBLIGATION_KIND,
    ActivationPayload,
    EC2Activation,
    decode_activation_payload,
    encode_activation_payload,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
INSTANCE = "i-0123456789abcdef0"
REQUEST = "offline-provider-request-id"
LOCATOR = ProviderLocator(f"aws-ec2:{ACCOUNT}:{REGION}:{INSTANCE}")


def response() -> dict[str, Any]:
    return {
        "StartingInstances": [{"InstanceId": INSTANCE}],
        "ResponseMetadata": {"RequestId": REQUEST, "HTTPStatusCode": 200, "RetryAttempts": 0},
    }


@pytest.fixture
def owned(tmp_path):
    with closing(Database(tmp_path / "activation.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        calls = []

        def send(**kwargs):
            assert kwargs == {"InstanceIds": [INSTANCE]}
            assert row(adapter).state is LifecycleObligationState.POSSIBLE_EFFECT
            calls.append("start")
            return response()

        client = SimpleNamespace(start_instances=send, close=lambda: calls.append("close"))

        def construct(service, **kwargs):
            assert service == "ec2" and kwargs["region_name"] == REGION
            config = kwargs["config"]
            assert 0 < config.connect_timeout <= 5 and config.read_timeout == config.connect_timeout
            assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
            assert row(adapter).state is LifecycleObligationState.REGISTERED
            calls.append("client")
            return client

        session = SimpleNamespace(client=construct)
        adapter = EC2Activation(owner, "vm-one", session, ACCOUNT, REGION, INSTANCE, LOCATOR)
        yield SimpleNamespace(
            repository=repository, owner=owner, adapter=adapter, session=session, client=client, calls=calls
        )


def row(adapter: EC2Activation) -> LifecycleObligation:
    result = adapter._owner.inspect_lifecycle_obligation(adapter.obligation_id)
    assert result is not None
    return result


def test_passive_constructor_one_start_and_no_settlement(owned):
    adapter = owned.adapter
    assert owned.calls == [] and adapter.obligation is None
    assert not adapter.cleanup_incomplete
    assert adapter.start(Deadline.after(5)) == REQUEST
    assert not adapter.cleanup_incomplete
    persisted = row(adapter)
    assert persisted.obligation_kind == OBLIGATION_KIND and persisted.payload_version == 1
    assert persisted.payload_revision == 1
    assert decode_activation_payload(persisted.payload) == replace(adapter.payload, request_id=REQUEST)
    owned.owner.stop_admission()
    adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert row(adapter).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert owned.calls == ["client", "start", "close"]
    assert owned.repository.inspect(owned.owner.ownership.scope) is not None


@pytest.mark.parametrize("stage", ["register", "mark", "publish"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_ledger_reply_never_replays(owned, monkeypatch, stage, committed, exception_type):
    method = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
    }[stage]
    original = getattr(owned.repository, method)
    primary = exception_type("injected ledger interruption")

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, method, fail)
    with pytest.raises(exception_type) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, method, original)
    if stage == "register" and not committed:
        with pytest.raises(StateError):
            owned.adapter.reconcile(Deadline.after(5))
    else:
        owned.adapter.reconcile(Deadline.after(5))
        expected = LifecycleObligationState.POSSIBLE_EFFECT
        if stage == "register":
            expected = LifecycleObligationState.RESOLVED
        elif stage == "mark" and not committed:
            expected = LifecycleObligationState.REGISTERED
        assert row(owned.adapter).state is expected
        assert row(owned.adapter).payload_revision == int(stage == "publish")
        if stage == "publish":
            assert owned.adapter.payload.request_id == REQUEST
            assert decode_activation_payload(row(owned.adapter).payload).request_id == REQUEST
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.calls.count("start") == int(stage == "publish")
    assert owned.calls.count("close") == int(stage != "register")


@pytest.mark.parametrize("stage", ["client", "start"])
@pytest.mark.parametrize("exception_type", [OSError, TimeoutError, KeyboardInterrupt, SystemExit])
def test_provider_exception_is_not_rejection(owned, monkeypatch, stage, exception_type):
    primary = exception_type("untrusted provider error")

    def fail(*args, **kwargs):
        owned.calls.append(stage)
        raise primary

    monkeypatch.setattr(
        owned.session if stage == "client" else owned.client, stage if stage == "client" else "start_instances", fail
    )
    with pytest.raises(exception_type) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    owned.adapter.reconcile(Deadline.after(5))
    expected = LifecycleObligationState.RESOLVED if stage == "client" else LifecycleObligationState.POSSIBLE_EFFECT
    assert row(owned.adapter).state is expected
    assert owned.adapter.payload.request_id is None
    assert owned.calls.count("close") == int(stage == "start")
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_interruption_immediately_after_stored_client_closes_without_dispatch(owned, exception_type):
    primary = exception_type("post-return interruption")
    start_code = owned.adapter.start.__func__.__code__
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is start_code
            and frame.f_locals.get("client") is owned.client
            and not fired
        ):
            fired.append(True)
            raise primary
        return interrupt

    previous_trace = sys.gettrace()
    sys.settrace(interrupt)
    try:
        with pytest.raises(exception_type) as caught:
            owned.adapter.start(Deadline.after(5))
        assert caught.value is primary
    finally:
        sys.settrace(previous_trace)
    assert fired == [True]
    assert owned.calls == ["client", "close"]
    assert row(owned.adapter).state is LifecycleObligationState.REGISTERED
    owned.adapter.reconcile(Deadline.after(5))
    assert row(owned.adapter).state is LifecycleObligationState.RESOLVED
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.calls == ["client", "close"] and owned.adapter.payload.request_id is None


@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("failed", [False, True])
def test_client_close_preserves_results_and_escaping_controls(owned, monkeypatch, exception_type, failed):
    close_error = exception_type("close interruption")

    def close():
        owned.calls.append("close")
        raise close_error

    monkeypatch.setattr(owned.client, "close", close)
    if failed:
        primary = KeyboardInterrupt("original SDK interruption")

        def send(**kwargs):
            owned.calls.append("start")
            raise primary

        monkeypatch.setattr(owned.client, "start_instances", send)
        with pytest.raises(KeyboardInterrupt) as caught:
            owned.adapter.start(Deadline.after(5))
        assert caught.value is primary
    elif exception_type is OSError:
        assert owned.adapter.start(Deadline.after(5)) == REQUEST
    else:
        with pytest.raises(exception_type) as caught:
            owned.adapter.start(Deadline.after(5))
        assert caught.value is close_error
        assert owned.adapter.payload.request_id == REQUEST and row(owned.adapter).payload_revision == 1
        owned.adapter.reconcile(Deadline.after(5))
        with pytest.raises(StateError):
            owned.adapter.start(Deadline.after(5))
    assert owned.calls == ["client", "start", "close"]
    assert row(owned.adapter).state is LifecycleObligationState.POSSIBLE_EFFECT
    close_error.__traceback__ = None
    if failed:
        primary.__traceback__ = None
    assert owned.adapter.cleanup_incomplete
    assert owned.adapter._client is owned.client
    owned.adapter.reconcile(Deadline.after(5))
    assert owned.calls == ["client", "start", "close"]


@pytest.mark.parametrize(
    "change",
    [
        None,
        [],
        {"StartingInstances": []},
        {"StartingInstances": [{"InstanceId": INSTANCE}, {"InstanceId": INSTANCE}]},
        {"StartingInstances": [{"InstanceId": "i-fffffffffffffffff"}]},
        {"StartingInstances": [None]},
        {"StartingInstances": [{}]},
        {"ResponseMetadata": None},
        {"RequestId": None},
        {"RequestId": ""},
        {"RequestId": "\0"},
        {"RequestId": "é" * 129},
        {"RequestId": "\ud800"},
        {"RetryAttempts": True},
        {"RetryAttempts": 1},
        {"RetryAttempts": None},
        {"HTTPStatusCode": True},
        {"HTTPStatusCode": 500},
        {"HTTPStatusCode": "200"},
    ],
)
def test_invalid_acknowledgment_retains_possible_effect(owned, monkeypatch, change):
    result = response()
    if type(change) is not dict:
        result = change
    elif set(change) & {"StartingInstances", "ResponseMetadata"}:
        result.update(change)
    else:
        result["ResponseMetadata"].update(change)
    monkeypatch.setattr(owned.client, "start_instances", lambda **kwargs: owned.calls.append("start") or result)
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(5))
    owned.adapter.reconcile(Deadline.after(5))
    assert row(owned.adapter).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row(owned.adapter).payload_revision == 0 and owned.adapter.payload.request_id is None
    assert owned.calls == ["client", "start", "close"]


def test_unused_state_fields_and_extra_external_metadata_are_not_interpreted(owned, monkeypatch):
    result = response()
    result["StartingInstances"][0].update(CurrentState={"Code": "future", "Name": None}, PreviousState=object())
    result["ResponseMetadata"]["RequestId"] = "é" * 128
    result["ResponseMetadata"]["unused"] = object()
    monkeypatch.setattr(owned.client, "start_instances", lambda **kwargs: result)
    assert owned.adapter.start(Deadline.after(5)) == "é" * 128
    assert row(owned.adapter).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("stage", ["register", "client", "mark", "start", "publish", "close"])
def test_deadline_at_each_boundary_retains_exact_custody(owned, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    target, method = {
        "register": (owned.repository, "register_lifecycle_obligation"),
        "client": (owned.session, "client"),
        "mark": (owned.repository, "mark_lifecycle_obligation_possible_effect"),
        "start": (owned.client, "start_instances"),
        "publish": (owned.repository, "publish_lifecycle_obligation_payload"),
        "close": (owned.client, "close"),
    }[stage]
    original = getattr(target, method)

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 0.03
        return result

    monkeypatch.setattr(target, method, late)
    with pytest.raises(TimeoutError):
        owned.adapter.start(Deadline.after(0.02))
    owned.adapter.reconcile(Deadline.after(5))
    expected = (
        LifecycleObligationState.RESOLVED
        if stage in {"register", "client"}
        else LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert row(owned.adapter).state is expected
    assert owned.calls.count("start") == int(stage in {"start", "publish", "close"})
    assert owned.adapter.payload.request_id == (REQUEST if stage in {"start", "publish", "close"} else None)
    assert row(owned.adapter).payload_revision == int(stage in {"start", "publish", "close"})


def test_invalid_or_expired_deadline_consumes_no_attempt(owned):
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(None))
    with pytest.raises(TimeoutError):
        owned.adapter.start(Deadline.after(0))
    assert owned.calls == [] and owned.owner.list_pending_lifecycle_obligations() == ()
    assert owned.adapter.start(Deadline.after(5)) == REQUEST


def test_stopped_owner_admission_prevents_client_construction(owned):
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.calls == [] and owned.owner.list_pending_lifecycle_obligations() == ()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_no_effect_resolution_reply_reconciles_without_client(owned, monkeypatch, committed, exception_type):
    def setup_failure(*args, **kwargs):
        raise OSError("setup failure")

    monkeypatch.setattr(owned.session, "client", setup_failure)
    with pytest.raises(OSError):
        owned.adapter.start(Deadline.after(5))
    original = owned.repository.resolve_lifecycle_obligation
    primary = exception_type("lost no-effect reply")

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, "resolve_lifecycle_obligation", fail)
    with pytest.raises(exception_type) as caught:
        owned.adapter.reconcile(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, "resolve_lifecycle_obligation", original)
    owned.adapter.reconcile(Deadline.after(5))
    assert row(owned.adapter).state is LifecycleObligationState.RESOLVED and owned.calls == []


@pytest.mark.parametrize("during", ["client", "start", "after"])
def test_fence_takeover_prevents_dispatch_or_publication(owned, monkeypatch, during):
    def takeover() -> OperationOwner:
        return OperationOwner.recover(owned.repository, owned.owner.ownership, "b" * 32)

    if during == "after":
        owned.adapter.start(Deadline.after(5))
        successor = takeover()
    else:
        target, method = (owned.session, "client") if during == "client" else (owned.client, "start_instances")
        original = getattr(target, method)
        successors = []

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            successors.append(takeover())
            return result

        monkeypatch.setattr(target, method, changed)
        with pytest.raises(StateError):
            owned.adapter.start(Deadline.after(5))
        successor = successors[0]
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))
    persisted = successor.inspect_lifecycle_obligation(owned.adapter.obligation_id)
    assert persisted is not None
    assert owned.calls.count("start") == int(during != "client")
    assert persisted.payload_revision == int(during == "after")
    assert owned.adapter.payload.request_id == (None if during == "client" else REQUEST)


def test_conflicting_payload_and_revision(owned):
    owned.adapter.start(Deadline.after(5))
    obligation = owned.adapter.obligation
    assert obligation is not None
    obligation.publish_payload(
        expected_revision=1,
        payload_version=1,
        payload=encode_activation_payload(replace(owned.adapter.payload, request_id="foreign")),
    )
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))
    assert owned.calls == ["client", "start", "close"]


def test_premature_resolution_refused(owned):
    owned.adapter.start(Deadline.after(5))
    assert owned.adapter.obligation is not None
    owned.adapter.obligation.resolve()
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))


@pytest.mark.parametrize(
    "field,value", [("obligation_kind", "foreign"), ("payload_version", 2), ("payload_revision", 2)]
)
def test_foreign_ledger_identity_refused(owned, monkeypatch, field, value):
    owned.adapter.start(Deadline.after(5))
    persisted = replace(row(owned.adapter), **{field: value})
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", lambda self, identifier: persisted)
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))


def test_concurrent_start_and_lock_deadline_send_once(owned, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = owned.client.start_instances

    def blocked(**kwargs):
        entered.set()
        assert release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(owned.client, "start_instances", blocked)
    errors = []

    def invoke():
        try:
            owned.adapter.start(Deadline.after(5))
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=invoke)
    first.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError):
            owned.adapter.reconcile(Deadline.after(0.01))
        second = threading.Thread(target=invoke)
        second.start()
    finally:
        release.set()
        first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StateError)
    assert owned.calls == ["client", "start", "close"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "١" * 12),
        ("account_id", 123456789012),
        ("region", "x" * 65),
        ("region", "US/east"),
        ("instance_id", "i-123"),
        ("instance_id", "i-ABCDEF12"),
        ("request_id", ""),
        ("request_id", "x" * 257),
        ("request_id", True),
        ("request_id", "\ud800"),
        ("version", 1),
    ],
)
def test_persisted_payload_rejects_invalid_fields(field, value):
    data = json.loads(encode_activation_payload(ActivationPayload(ACCOUNT, REGION, INSTANCE)))
    data[field] = value
    with pytest.raises(ValidationError):
        decode_activation_payload(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii"))


@pytest.mark.parametrize(
    "data", [b"[]", b"null", b"{", b"\xff", b"x" * (MAX_LIFECYCLE_PAYLOAD_BYTES + 1), bytearray(b"{}")]
)
def test_persisted_payload_rejects_invalid_encoding_and_capacity(data):
    with pytest.raises(ValidationError):
        decode_activation_payload(data)


def test_payload_canonicality_and_bound():
    payload = ActivationPayload(ACCOUNT, REGION, INSTANCE, "é" * 128)
    encoded = encode_activation_payload(payload)
    assert decode_activation_payload(encoded) == payload
    assert len(encoded) <= MAX_LIFECYCLE_PAYLOAD_BYTES
    assert set(json.loads(encoded)) == {"account_id", "region", "instance_id", "request_id"}
    for invalid in [b" " + encoded, encoded.replace(b'"account_id":', b'"account_id":"foreign","account_id":')]:
        with pytest.raises(ValidationError):
            decode_activation_payload(invalid)


@pytest.mark.parametrize(
    "kind,name,locator",
    [
        (OperationResourceKind.VM, "other", LOCATOR),
        (OperationResourceKind.VM, "vm-one", ProviderLocator("foreign")),
        (OperationResourceKind.PLATFORM_HOST, "vm-one", LOCATOR),
    ],
)
def test_selected_owner_and_locator_boundary(tmp_path, kind, name, locator):
    with closing(Database(tmp_path / "owner.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(kind, name), "proof")
        with pytest.raises(ValidationError):
            EC2Activation(owner, "vm-one", object(), ACCOUNT, REGION, INSTANCE, locator)
        assert owner.list_pending_lifecycle_obligations() == ()
