"""One-shot ARM acknowledgment custody against the actual SQLite ledger."""

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
from agentworks.plugins.azure import _activation_client
from agentworks.plugins.azure._activation import (
    ActivationPayload,
    AzureVMActivation,
    StartAcknowledgment,
    decode_activation_payload,
    encode_activation_payload,
    start_url,
)

SUBSCRIPTION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
OPERATION = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Compute/locations/eastus/operations/op?api-version=2026-04-01"
LOCATOR = ProviderLocator(f"azure-vm:{RESOURCE}")
ACK = StartAcknowledgment(202, "offline-request", OPERATION)


@pytest.fixture(autouse=True)
def isolated_migration_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent-config.toml")


def row(owned: Any) -> LifecycleObligation:
    owner: OperationOwner = owned.owner
    result = owner.inspect_lifecycle_obligation(owned.adapter.obligation_id)
    assert result is not None
    return result


@pytest.fixture
def owned(tmp_path, monkeypatch):
    with closing(Database(tmp_path / "a.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        calls = []
        response = SimpleNamespace(
            request=SimpleNamespace(method="POST", url=start_url(RESOURCE)),
            status_code=202,
            headers={"X-MS-Request-ID": "offline-request", "Location": OPERATION},
            closed=False,
        )

        def close_response():
            calls.append("response-close")
            response.closed = True

        response.close = close_response
        client = SimpleNamespace(closed=False)

        def close_client():
            calls.append("client-close")
            client.closed = True

        client.close = close_client

        def send(request, **options):
            assert request.method == "POST" and request.url == start_url(RESOURCE)
            assert request.content is None
            assert options["stream"] is True
            assert 0 < options["connection_timeout"] <= 5
            assert options["read_timeout"] == options["connection_timeout"]
            assert row(result).state is LifecycleObligationState.POSSIBLE_EFFECT
            calls.append("send")
            return response

        client.send_request = send

        def construct(credential, subscription):
            assert subscription == SUBSCRIPTION
            assert row(result).state is LifecycleObligationState.REGISTERED
            calls.append("client")
            return client

        monkeypatch.setattr(_activation_client, "compute_start_client", construct)
        adapter = AzureVMActivation(owner, "vm", object(), RESOURCE, LOCATOR)
        result = SimpleNamespace(
            adapter=adapter, owner=owner, repository=repository, calls=calls, client=client, response=response
        )
        yield result


def test_passive_once_never_settles_or_closes_owner(owned):
    assert owned.calls == [] and owned.adapter.obligation is None
    assert owned.adapter.start(Deadline.after(5)) == ACK
    owned.owner.stop_admission()
    owned.adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row(owned).payload_revision == 1
    assert decode_activation_payload(row(owned).payload) == owned.adapter.payload
    assert owned.calls == ["client", "send", "response-close", "client-close"]
    assert owned.response.closed and owned.client.closed and not owned.adapter.cleanup_incomplete
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
    primary = exception_type("offline lost ledger reply")

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
        assert row(owned).state is expected
        assert row(owned).payload_revision == int(stage == "publish")
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.calls.count("send") == int(stage == "publish")
    assert owned.calls.count("client-close") == int(stage != "register")
    assert owned.calls.count("response-close") == int(stage == "publish")


@pytest.mark.parametrize("stage", ["register", "client", "mark", "send", "publish", "response-close", "client-close"])
def test_deadline_each_boundary_retains_late_ack(owned, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    target, method = {
        "register": (owned.repository, "register_lifecycle_obligation"),
        "client": (_activation_client, "compute_start_client"),
        "mark": (owned.repository, "mark_lifecycle_obligation_possible_effect"),
        "send": (owned.client, "send_request"),
        "publish": (owned.repository, "publish_lifecycle_obligation_payload"),
        "response-close": (owned.response, "close"),
        "client-close": (owned.client, "close"),
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
    dispatched = stage in {"send", "publish", "response-close", "client-close"}
    assert owned.adapter.payload.acknowledgment == (ACK if dispatched else None)
    assert row(owned).payload_revision == int(dispatched)
    assert owned.calls.count("send") == int(dispatched)
    assert row(owned).state is (
        LifecycleObligationState.RESOLVED
        if stage in {"register", "client"}
        else LifecycleObligationState.POSSIBLE_EFFECT
    )


@pytest.mark.parametrize("handle", ["response", "client"])
@pytest.mark.parametrize("closed_first", [False, True])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_close_failure_retains_actual_handle_and_ack(owned, monkeypatch, handle, closed_first, exception_type):
    target = getattr(owned, handle)
    primary = exception_type("offline close interruption")

    def fail():
        target.closed = closed_first
        raise primary

    monkeypatch.setattr(target, "close", fail)
    if exception_type is OSError:
        assert owned.adapter.start(Deadline.after(5)) == ACK
    else:
        with pytest.raises(exception_type) as caught:
            owned.adapter.start(Deadline.after(5))
        assert caught.value is primary
    assert owned.adapter.cleanup_incomplete
    assert owned.adapter._unclosed_handles == (target,)
    assert target.closed is closed_first
    assert getattr(owned, "client" if handle == "response" else "response").closed
    assert owned.adapter.payload.acknowledgment == ACK
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_original_control_wins_and_both_failed_handles_retained(owned, monkeypatch, exception_type):
    primary = KeyboardInterrupt("original control")

    def send(*args, **kwargs):
        raise primary

    def close():
        raise exception_type("cleanup error")

    monkeypatch.setattr(owned.client, "send_request", send)
    monkeypatch.setattr(owned.client, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    assert owned.adapter._unclosed_handles == (owned.client,)
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("handle", ["client", "response"])
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_after_positive_handle_return_closes(owned, handle, exception_type):
    primary = exception_type("after returned handle")
    code = owned.adapter.start.__func__.__code__
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is code
            and frame.f_locals.get(handle) is getattr(owned, handle)
            and not fired
        ):
            fired.append(True)
            raise primary
        return interrupt

    previous = sys.gettrace()
    sys.settrace(interrupt)
    try:
        with pytest.raises(exception_type) as caught:
            owned.adapter.start(Deadline.after(5))
        assert caught.value is primary
    finally:
        sys.settrace(previous)
    assert fired and owned.client.closed
    assert owned.response.closed is (handle == "response")
    assert not owned.adapter.cleanup_incomplete


@pytest.mark.parametrize("stage", ["client", "send", "after"])
def test_fence_takeover_refuses_effect_or_publication(owned, monkeypatch, stage):
    successors = []

    def takeover() -> None:
        successors.append(OperationOwner.recover(owned.repository, owned.owner.ownership, "b" * 32))

    if stage == "after":
        owned.adapter.start(Deadline.after(5))
        takeover()
    else:
        target, method = (
            (_activation_client, "compute_start_client") if stage == "client" else (owned.client, "send_request")
        )
        original = getattr(target, method)

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            takeover()
            return result

        monkeypatch.setattr(target, method, changed)
        with pytest.raises(StateError):
            owned.adapter.start(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))
    persisted = successors[0].inspect_lifecycle_obligation(owned.adapter.obligation_id)
    assert persisted is not None and persisted.payload_revision == int(stage == "after")
    assert owned.calls.count("send") == int(stage != "client")
    assert owned.adapter.payload.acknowledgment == (None if stage == "client" else ACK)


@pytest.mark.parametrize(
    "field,value", [("obligation_kind", "foreign"), ("payload_version", 2), ("payload_revision", 2), ("payload", b"{}")]
)
def test_corrupt_or_foreign_ledger_refused(owned, monkeypatch, field, value):
    owned.adapter.start(Deadline.after(5))
    persisted = replace(row(owned), **{field: value})
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", lambda *args: persisted)
    with pytest.raises((StateError, ValidationError)):
        owned.adapter.reconcile(Deadline.after(5))


def test_premature_resolution_and_foreign_ack_refused(owned):
    owned.adapter.start(Deadline.after(5))
    owned.adapter.obligation.publish_payload(
        expected_revision=1,
        payload_version=1,
        payload=encode_activation_payload(ActivationPayload(RESOURCE, replace(ACK, request_id="foreign"))),
    )
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))
    owned.adapter.obligation.resolve()
    with pytest.raises(StateError):
        owned.adapter.reconcile(Deadline.after(5))


@pytest.mark.parametrize(
    "data", [b"[]", b"null", b"{", b"\xff", b"x" * (MAX_LIFECYCLE_PAYLOAD_BYTES + 1), bytearray(b"{}")]
)
def test_corrupt_recovery_payload(data):
    with pytest.raises(ValidationError):
        decode_activation_payload(data)


def test_canonical_payload_and_combined_capacity():
    encoded = encode_activation_payload(ActivationPayload(RESOURCE, ACK))
    assert decode_activation_payload(encoded) == ActivationPayload(RESOURCE, ACK)
    assert len(encoded) <= MAX_LIFECYCLE_PAYLOAD_BYTES
    for data in [b" " + encoded, encoded.replace(b'"resource_id":', b'"resource_id":"foreign","resource_id":')]:
        with pytest.raises(ValidationError):
            decode_activation_payload(data)
    long_url = OPERATION.split("?")[0] + "/" + "x" * 7900
    with pytest.raises(ValidationError):
        encode_activation_payload(ActivationPayload(RESOURCE, StartAcknowledgment(202, location=long_url)))
    value = json.loads(encoded)
    value["acknowledgment"]["status_code"] = True
    with pytest.raises(ValidationError):
        decode_activation_payload(json.dumps(value).encode())


def test_literal_resource_components_are_quoted():
    resource = RESOURCE.replace("/group/", "/gr%2Foup?#/").replace("/vm", "/vm%2F?#é")
    assert encode_activation_payload(ActivationPayload(resource))
    url = start_url(resource)
    assert "/gr%252Foup%3F%23/" in url and "/vm%252F%3F%23%C3%A9/start?" in url
    assert url.count("?") == 1 and "#" not in url


@pytest.mark.parametrize(
    "kind,name,locator",
    [
        (OperationResourceKind.VM, "foreign", LOCATOR),
        (OperationResourceKind.PLATFORM_HOST, "vm", LOCATOR),
        (OperationResourceKind.VM, "vm", ProviderLocator("azure-vm:" + RESOURCE.upper())),
    ],
)
def test_exact_owner_and_locator(tmp_path, kind, name, locator):
    with closing(Database(tmp_path / "o.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(kind, name), "proof")
        with pytest.raises(ValidationError):
            AzureVMActivation(owner, "vm", object(), RESOURCE, locator)
        assert owner.list_pending_lifecycle_obligations() == ()


def test_bad_deadline_and_stopped_admission_have_no_effects(owned):
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(None))
    with pytest.raises(TimeoutError):
        owned.adapter.start(Deadline.after(0))
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.calls == [] and owned.owner.list_pending_lifecycle_obligations() == ()


@pytest.mark.parametrize("stage", ["client", "send"])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_setup_or_dispatch_failure_is_not_remote_rejection(owned, monkeypatch, stage, exception_type):
    primary = exception_type("offline setup or dispatch failure")

    def fail(*args, **kwargs):
        raise primary

    target, method = (
        (_activation_client, "compute_start_client") if stage == "client" else (owned.client, "send_request")
    )
    monkeypatch.setattr(target, method, fail)
    with pytest.raises(exception_type) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    owned.adapter.reconcile(Deadline.after(5))
    assert row(owned).state is (
        LifecycleObligationState.RESOLVED if stage == "client" else LifecycleObligationState.POSSIBLE_EFFECT
    )
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert owned.client.closed is (stage == "send")


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_no_effect_resolution_reply(owned, monkeypatch, committed, exception_type):
    def setup_failure(*args, **kwargs):
        raise OSError("offline setup failure")

    monkeypatch.setattr(_activation_client, "compute_start_client", setup_failure)
    with pytest.raises(OSError):
        owned.adapter.start(Deadline.after(5))
    original = owned.repository.resolve_lifecycle_obligation
    primary = exception_type("offline lost resolution reply")

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
    assert row(owned).state is LifecycleObligationState.RESOLVED and owned.calls == []


@pytest.mark.parametrize("value", [None, "200", True, 204])
def test_nonordinary_http_status_at_plugin_boundary(owned, value):
    owned.response.status_code = value
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(5))
    assert owned.response.closed and owned.client.closed
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_both_close_controls_preserve_original_publication_control(owned, monkeypatch):
    primary = KeyboardInterrupt("original publication control")

    def fail_publish(*args, **kwargs):
        raise primary

    def fail_close():
        raise SystemExit("close control")

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", fail_publish)
    monkeypatch.setattr(owned.response, "close", fail_close)
    monkeypatch.setattr(owned.client, "close", fail_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    assert owned.adapter._unclosed_handles == (owned.response, owned.client)
    assert not owned.response.closed and not owned.client.closed
    assert owned.adapter.payload.acknowledgment == ACK


def test_fake_owner_boundary_refuses_before_registration():
    with pytest.raises(ValidationError):
        AzureVMActivation(SimpleNamespace(), "vm", object(), RESOURCE, LOCATOR)


def test_concurrent_admission_and_lock_deadline_send_once(owned, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = owned.client.send_request
    errors: list[BaseException] = []

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    def invoke() -> None:
        try:
            owned.adapter.start(Deadline.after(5))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(owned.client, "send_request", blocked)
    first, second = threading.Thread(target=invoke), threading.Thread(target=invoke)
    first.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError):
            owned.adapter.reconcile(Deadline.after(0.01))
        second.start()
    finally:
        release.set()
        first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StateError)
    assert owned.calls == ["client", "send", "response-close", "client-close"]
