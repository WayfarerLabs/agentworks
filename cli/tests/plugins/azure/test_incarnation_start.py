"""Exact-incarnation start with real SQLite and the locked public Azure pipeline."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.transport import HttpTransport

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _activation_client
from agentworks.plugins.azure._activation import (
    ActivationPayload,
    decode_activation_payload,
    encode_activation_payload,
    start_url,
)
from agentworks.plugins.azure._incarnation_start import AzureIncarnationStart
from agentworks.plugins.azure._incarnation_start_payload import (
    StartState,
    decode_start_payload,
    encode_start_payload,
)
from tests.plugins.azure.test_activation_sdk import Credential, Response, deny

SUBSCRIPTION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/gr:oup/providers/Microsoft.Compute/virtualMachines/vm"
VM_ID = "01234567-89ab-0cde-0123-456789abcdef"
OPERATION = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/operations/one?api-version=2026-04-01"
LOCATION = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/operations/two"
LOCATOR = ProviderLocator(f"azure-vm:v2:{VM_ID}:{RESOURCE}")


class Control(BaseException):
    pass


class MonitorResponse(Response):
    def __init__(self, request, status, headers, body) -> None:
        super().__init__(request, status, headers)
        self.body = body
        self.close_errors: list[BaseException] = []
        self.close_calls = 0
        self.iterations = 0
        self.foreign: object = None

    def iter_bytes(self, **kwargs):
        self.iterations += 1
        if isinstance(self.body, BaseException):
            raise self.body
        yield from self.body

    def close(self) -> None:
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        super().close()


class Transport(HttpTransport):
    def __init__(self) -> None:
        self.replies: list[Any] = []
        self.calls: list[tuple[str, str]] = []
        self.responses: list[MonitorResponse] = []
        self.close_errors: list[BaseException] = []
        self.close_calls = 0
        self.hook = None

    def open(self):
        pass

    def close(self) -> None:
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        assert kwargs["stream"] is True and request.content is None
        assert 0 < kwargs["connection_timeout"] <= 5
        self.calls.append((request.method, request.url))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, headers, body = reply
        response = MonitorResponse(request, status, headers, body)
        self.responses.append(response)
        if self.hook is not None:
            self.hook(response)
        return response


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    transport = Transport()

    class RetainedCredential(Credential):
        close = deny

    credential = RetainedCredential()
    original = _activation_client.compute_start_client
    constructed = []

    def construct(selected, subscription):
        assert selected is credential and subscription == SUBSCRIPTION
        client = original(selected, subscription, transport=transport)
        constructed.append(client)
        return client

    monkeypatch.setattr(_activation_client, "compute_start_client", construct)
    with closing(Database(tmp_path / "start.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        adapter = AzureIncarnationStart(owner, "vm", credential, RESOURCE, VM_ID.upper(), LOCATOR)
        probe = SimpleNamespace(
            adapter=adapter,
            owner=owner,
            repository=repository,
            transport=transport,
            credential=credential,
            constructed=constructed,
        )
        yield probe
        assert database.operations.inspect(owner.ownership.scope) is not None


def row(probe) -> Any:
    return probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)


def start_async(probe, *, location_only=False, status=202) -> None:
    headers = {"Location": LOCATION}
    if not location_only:
        headers["Azure-AsyncOperation"] = OPERATION
    probe.transport.replies.append((status, headers, ServiceRequestError("secret POST body")))
    probe.adapter.start(Deadline.after(5))


def test_passive_and_closed_never_admitted(owned):
    assert owned.constructed == [] and row(owned) is None
    assert owned.adapter.state is StartState.UNKNOWN
    assert owned.adapter.close(Deadline.after(5))
    assert owned.adapter.closed_without_start_attempt
    assert owned.adapter.settle(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.observe(Deadline.after(5))
    assert owned.transport.calls == [] and row(owned) is None


@pytest.mark.parametrize("kind", ["name", "resource", "v1", "uuid", "case"])
def test_construction_refuses_inexact_identity(owned, kind):
    name, resource, vm_id, locator = "vm", RESOURCE, VM_ID, LOCATOR
    if kind == "name":
        name = "other"
    elif kind == "resource":
        resource += "/foreign"
    elif kind == "v1":
        locator = ProviderLocator(f"azure-vm:{RESOURCE}")
    elif kind == "uuid":
        vm_id = "bad"
    else:
        locator = ProviderLocator(f"azure-vm:v2:{VM_ID.upper()}:{RESOURCE}")
    with pytest.raises(ValidationError):
        AzureIncarnationStart(owned.owner, name, owned.credential, resource, vm_id, locator)
    assert owned.constructed == []


def test_v1_codec_stays_separate_and_v2_roundtrips(owned):
    legacy = ActivationPayload(RESOURCE)
    encoded = encode_activation_payload(legacy)
    assert decode_activation_payload(encoded) == legacy
    with pytest.raises(ValidationError):
        decode_start_payload(encoded)
    assert decode_start_payload(encode_start_payload(owned.adapter.payload)) == owned.adapter.payload


def test_synchronous_200_resolves_only_after_originals_retire(owned):
    owned.transport.replies.append((200, {}, ServiceRequestError("secret")))
    owned.adapter.start(Deadline.after(5))
    assert owned.adapter.state is StartState.SUCCEEDED
    assert row(owned).state is LifecycleObligationState.RESOLVED
    assert row(owned).payload_revision == 2
    assert owned.transport.calls == [("POST", start_url(RESOURCE))]
    assert owned.transport.responses[0].iterations == 0
    assert owned.adapter.observe(Deadline.after(5)) is StartState.SUCCEEDED
    assert len(owned.constructed) == 1


@pytest.mark.parametrize("post_status", [200, 202])
@pytest.mark.parametrize("status", ["Succeeded", "Failed", "Canceled", "Running", "Accepted", "succeeded"])
def test_exact_async_monitor_and_distinct_terminal_states(owned, post_status, status):
    start_async(owned, status=post_status)
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    owned.transport.replies.append((200, {}, [json.dumps({"status": status, "error": "secret"}).encode()]))
    observed = owned.adapter.observe(Deadline.after(5))
    terminal = status in {"Succeeded", "Failed", "Canceled"}
    assert observed == (StartState(status) if terminal else StartState.PENDING)
    assert row(owned).state is (
        LifecycleObligationState.RESOLVED if terminal else LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert owned.transport.calls == [("POST", start_url(RESOURCE)), ("GET", OPERATION)]
    assert b"secret" not in row(owned).payload
    assert decode_start_payload(row(owned).payload) == owned.adapter.payload


@pytest.mark.parametrize("status", [202, 200, 204])
def test_location_monitor_does_not_read_body(owned, status):
    start_async(owned, location_only=True)
    owned.transport.replies.append((status, {}, ServiceRequestError("secret")))
    assert owned.adapter.observe(Deadline.after(5)) is (StartState.PENDING if status == 202 else StartState.SUCCEEDED)
    assert owned.transport.calls[-1] == ("GET", LOCATION)
    assert owned.transport.responses[-1].iterations == 0


@pytest.mark.parametrize("stage", ["post", "get"])
@pytest.mark.parametrize("status", [401, 307, 409, 429, 500])
def test_error_response_is_not_replayed_or_read(owned, stage, status):
    if stage == "get":
        start_async(owned)
    owned.transport.replies.append((status, {"Location": OPERATION, "WWW-Authenticate": "Bearer secret"}, deny))
    with pytest.raises(ValidationError):
        (owned.adapter.start if stage == "post" else owned.adapter.observe)(Deadline.after(5))
    assert len(owned.transport.calls) == (1 if stage == "post" else 2)
    assert owned.transport.responses[-1].iterations == 0
    assert not owned.adapter.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "body",
    [
        [b'{"status":"Succeeded","status":"Failed"}'],
        [b"[]"],
        [b"{}"],
        [b'{"status":1}'],
        [b'{"status":""}'],
        [b'{"status":"Bad Value"}'],
        [b"x" * 16385],
        [b"x" * 16000, b"x" * 500],
        [b'{"status":"Succeeded"'],
        ["not bytes"],
    ],
)
def test_invalid_bounded_monitor_body_never_settles(owned, body):
    start_async(owned)
    owned.transport.replies.append((200, {}, body))
    with pytest.raises(ValidationError):
        owned.adapter.observe(Deadline.after(5))
    assert owned.adapter.state is StartState.PENDING
    assert not owned.adapter.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Location": "https://foreign.invalid/status"},
        {"Azure-AsyncOperation": OPERATION.replace(SUBSCRIPTION, "foreign")},
        {"Location": OPERATION, "location": OPERATION},
        {"Location": OPERATION + "&token=secret"},
    ],
)
def test_missing_or_bad_ack_cannot_replay(owned, headers):
    owned.transport.replies.append((202, headers, deny))
    with pytest.raises(ValidationError):
        owned.adapter.start(Deadline.after(5))
    assert owned.adapter.state is StartState.UNKNOWN
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.observe(Deadline.after(5))
    assert not owned.adapter.settle(Deadline.after(5))
    assert len(owned.transport.calls) == 1


@pytest.mark.parametrize("method,url", [("POST", OPERATION), ("GET", LOCATION), ("GET", OPERATION + "&secret=1")])
def test_monitor_response_must_bind_exact_request(owned, method, url):
    start_async(owned)
    owned.transport.hook = lambda response: setattr(response, "_request", SimpleNamespace(method=method, url=url))
    owned.transport.replies.append((200, {}, [b'{"status":"Succeeded"}']))
    with pytest.raises(ValidationError):
        owned.adapter.observe(Deadline.after(5))
    assert owned.transport.responses[-1].iterations == 0


@pytest.mark.parametrize("stage", ["send", "body"])
@pytest.mark.parametrize("error_type", [ServiceRequestError, TimeoutError, KeyboardInterrupt, SystemExit, Control])
def test_sdk_failures_detached_controls_preserved(owned, stage, error_type):
    start_async(owned)
    primary = error_type("secret SDK diagnostic")
    owned.transport.replies.append(primary if stage == "send" else (200, {}, primary))
    if issubclass(error_type, Exception):
        with pytest.raises(StateError) as caught:
            owned.adapter.observe(Deadline.after(5))
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "secret" not in str(caught.value)
    else:
        with pytest.raises(error_type) as caught:
            owned.adapter.observe(Deadline.after(5))
        assert caught.value is primary
    assert not owned.adapter.cleanup_incomplete
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("stage", ["register", "mark", "publish", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_ledger_reply_loss_reconciles_without_provider_replay(owned, monkeypatch, stage, committed, error_type):
    method = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }[stage]
    original = getattr(owned.repository, method)
    primary = error_type("lost ledger reply")

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, method, fail)
    owned.transport.replies.append((200, {}, deny))
    with pytest.raises(error_type) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, method, original)
    resolved = owned.adapter.settle(Deadline.after(5))
    assert resolved is (stage != "mark")
    assert len(owned.transport.calls) == int(stage in {"publish", "resolve"})
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))


@pytest.mark.parametrize("stage", ["post", "get"])
def test_late_complete_evidence_retained_then_settled_under_fresh_deadline(owned, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    if stage == "get":
        start_async(owned, location_only=True)
    owned.transport.hook = lambda response: clock.__setitem__(0, 102.0)
    owned.transport.replies.append((200, {}, deny))
    with pytest.raises(TimeoutError):
        (owned.adapter.start if stage == "post" else owned.adapter.observe)(Deadline.after(1))
    assert owned.adapter.state is StartState.SUCCEEDED and owned.adapter.cleanup_incomplete
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert owned.adapter.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.RESOLVED


def test_deadline_midbody_keeps_pending_and_cleanup_debt(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    start_async(owned)

    def chunks() -> Iterator[bytes]:
        clock[0] = 102.0
        yield b'{"status":"Succeeded"}'
        raise AssertionError("must not continue body after deadline")

    owned.transport.replies.append((200, {}, chunks()))
    with pytest.raises(TimeoutError):
        owned.adapter.observe(Deadline.after(1))
    assert owned.adapter.state is StartState.PENDING
    assert owned.adapter.cleanup_incomplete
    assert not owned.adapter.settle(Deadline.after(5))


@pytest.mark.parametrize("handle", ["response", "client"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit, Control])
def test_cleanup_debt_blocks_requests_and_retries_only_original(owned, handle, error_type):
    def hook(response):
        if handle == "response":
            response.close_errors.append(error_type("secret close"))

    owned.transport.hook = hook
    if handle == "client":
        owned.transport.close_errors.append(error_type("secret close"))
    owned.transport.replies.append((200, {}, deny))
    if issubclass(error_type, Exception):
        owned.adapter.start(Deadline.after(5))
    else:
        with pytest.raises(error_type):
            owned.adapter.start(Deadline.after(5))
    assert owned.adapter.cleanup_incomplete
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        owned.adapter.observe(Deadline.after(5))
    assert owned.adapter.close(Deadline.after(5))
    assert owned.adapter.settle(Deadline.after(5))
    assert len(owned.constructed) == len(owned.transport.calls) == 1


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, Control])
@pytest.mark.parametrize("secondary_type", [OSError, KeyboardInterrupt, SystemExit, Control])
def test_original_control_precedes_all_cleanup_failures(owned, primary_type, secondary_type):
    start_async(owned)
    primary = primary_type("primary")
    owned.transport.hook = lambda response: response.close_errors.append(secondary_type("secondary"))
    owned.transport.replies.append((200, {}, primary))
    with pytest.raises(primary_type) as caught:
        owned.adapter.observe(Deadline.after(5))
    assert caught.value is primary
    assert owned.adapter.cleanup_incomplete
    assert owned.adapter.close(Deadline.after(5))
    assert not owned.adapter.settle(Deadline.after(5))


def test_late_complete_async_terminal_retained_after_iterator_exhaustion(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    start_async(owned)

    def chunks() -> Iterator[bytes]:
        yield b'{"status":"Failed"}'
        clock[0] = 102.0

    owned.transport.replies.append((200, {}, chunks()))
    with pytest.raises(TimeoutError):
        owned.adapter.observe(Deadline.after(1))
    assert owned.adapter.state is StartState.FAILED
    assert owned.adapter.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.RESOLVED


def test_foreign_cas_revision_never_resolves_or_admits_more_work(owned):
    start_async(owned)
    foreign = replace(owned.adapter.payload, vm_id="11111111-2222-3333-4444-555555555555")
    owned.adapter.obligation.publish_payload(
        expected_revision=1, payload_version=2, payload=encode_start_payload(foreign)
    )
    with pytest.raises(StateError):
        owned.adapter.settle(Deadline.after(5))
    with pytest.raises(StateError):
        owned.adapter.observe(Deadline.after(5))
    assert len(owned.transport.calls) == 1


@pytest.mark.parametrize("expired", [True, False])
def test_original_deadline_or_nonfinite_refuses_before_admission(owned, expired):
    deadline = Deadline.after(0) if expired else Deadline(None)
    with pytest.raises(TimeoutError if expired else ValidationError):
        owned.adapter.start(deadline)
    assert owned.transport.calls == [] and row(owned) is None


def test_core_domain_exception_preserved_and_owner_stays_owned(owned):
    primary = StateError("core domain")
    owned.transport.replies.append(primary)
    with pytest.raises(StateError) as caught:
        owned.adapter.start(Deadline.after(5))
    assert caught.value is primary
    assert not owned.adapter.settle(Deadline.after(5))
    assert owned.adapter.close(Deadline.after(5))
    assert owned.repository.inspect(owned.owner.ownership.scope) is not None
