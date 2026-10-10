"""Actual locked SDK public streaming dispatch, without sockets or real credentials."""

from __future__ import annotations

import socket
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.core.pipeline.transport import HttpTransport
from azure.core.rest import HttpResponse

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _activation_client
from agentworks.plugins.azure._activation import AzureVMActivation, decode_activation_payload, start_url

SUBSCRIPTION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
OPERATION = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Compute/locations/eastus/operations/op?api-version=2026-04-01"


@pytest.fixture(autouse=True)
def isolated_migration_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent-config.toml")


def deny(*args, **kwargs):
    raise AssertionError("unexpected body read or socket access")


class Response(HttpResponse):
    def __init__(self, request: Any, status: object, headers: dict[str, str]) -> None:
        self._request, self._status, self._headers = request, status, headers
        self.closed = False

    request = property(lambda self: self._request)
    status_code = property(lambda self: self._status)
    headers = property(lambda self: self._headers)
    content_type = property(lambda self: "application/json")
    url = property(lambda self: self._request.url)
    reason = property(lambda self: "offline")
    content = property(deny)
    encoding = property(lambda self: "utf-8")
    is_closed = property(lambda self: self.closed)
    is_stream_consumed = property(lambda self: False)
    read = deny
    text = deny
    json = deny
    iter_bytes = deny
    iter_raw = deny
    raise_for_status = deny

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class ScriptedTransport(HttpTransport):
    def __init__(self) -> None:
        self.replies: list[Any] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.responses: list[Response] = []
        self.closed = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> ScriptedTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def send(self, request: Any, **kwargs: Any) -> Response:
        assert kwargs["stream"] is True and request.content is None
        assert request.method == "POST" and request.url == start_url(RESOURCE)
        assert 0 < kwargs["connection_timeout"] <= 5
        assert kwargs["read_timeout"] == kwargs["connection_timeout"]
        self.calls.append((request.method, request.url, kwargs))
        assert self.replies, "unexpected extra dispatch"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, headers, foreign = reply
        response = Response(request, status, headers)
        if foreign:
            response._request = SimpleNamespace(method="POST", url=start_url(RESOURCE) + "&foreign=1")
        self.responses.append(response)
        return response


class Credential:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        assert scopes == ("https://management.azure.com/.default",)
        self.calls += 1
        return AccessToken("synthetic-offline-token", 4102444800)


@pytest.fixture
def sdk_owned(tmp_path, monkeypatch):
    for target, method in [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ]:
        monkeypatch.setattr(target, method, deny)
    transport = ScriptedTransport()
    credential = Credential()
    original = _activation_client.compute_start_client

    def construct(selected, subscription):
        assert selected is credential and subscription == SUBSCRIPTION
        return original(selected, subscription, transport=transport)

    monkeypatch.setattr(_activation_client, "compute_start_client", construct)
    with closing(Database(tmp_path / "s.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        adapter = AzureVMActivation(owner, "vm", credential, RESOURCE, ProviderLocator(f"azure-vm:{RESOURCE}"))
        yield SimpleNamespace(adapter=adapter, owner=owner, transport=transport, credential=credential)
    assert transport.closed
    assert all(response.closed for response in transport.responses)
    assert not adapter.cleanup_incomplete
    print("dispatch accounting:", len(transport.calls), "POST, 0 GET; responses closed:", len(transport.responses))


@pytest.mark.parametrize(
    "status,headers",
    [
        (200, {}),
        (200, {"x-ms-request-id": "offline-request"}),
        (202, {"Location": OPERATION}),
        (202, {"aZuRe-AsYnCoPeRaTiOn": OPERATION, "X-MS-Request-ID": "offline-request"}),
        (202, {"Location": OPERATION, "Azure-AsyncOperation": OPERATION}),
    ],
)
def test_public_streaming_success_is_one_ack(sdk_owned, status, headers):
    probe = sdk_owned
    probe.transport.replies.append((status, headers, False))
    ack = probe.adapter.start(Deadline.after(5))
    assert ack.status_code == status
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 1
    assert decode_activation_payload(row.payload).acknowledgment == ack
    probe.adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        probe.adapter.start(Deadline.after(5))
    assert len(probe.transport.calls) == probe.credential.calls == 1


@pytest.mark.parametrize(
    "status,headers,foreign",
    [
        (401, {"WWW-Authenticate": 'Bearer error="insufficient_claims", claims="eyJ4Ijp0cnVlfQ"'}, False),
        (307, {"Location": OPERATION}, False),
        (308, {"Location": OPERATION}, False),
        (409, {"x-ms-error-code": "MissingSubscriptionRegistration"}, False),
        (429, {"Retry-After": "1"}, False),
        (500, {}, False),
        (503, {}, False),
        (True, {}, False),
        (202, {}, False),
        (200, {}, True),
        (202, {"Location": "https://different.invalid/status"}, False),
        (202, {"Location": OPERATION.replace(SUBSCRIPTION, "foreign")}, False),
        (202, {"Location": OPERATION + "&token=secret"}, False),
        (202, {"Location": OPERATION + "#fragment"}, False),
        (202, {"Location": OPERATION.replace("management.azure.com", "user@management.azure.com")}, False),
        (202, {"Location": OPERATION.replace("management.azure.com", "management.azure.com:443")}, False),
        (202, {"Location": OPERATION.replace("https:", "http:")}, False),
        (202, {"Location": "x" * 8193}, False),
        (200, {"x-ms-request-id": "x" * 257}, False),
        (200, {"x-ms-request-id": ""}, False),
        (200, {"x-ms-request-id": "\r\nforeign"}, False),
        (200, {"x-ms-request-id": "first", "X-MS-Request-ID": "second"}, False),
        (202, {"Location": OPERATION, "location": OPERATION}, False),
        (202, {"Azure-AsyncOperation": 123}, False),
        (200, {"x-ms-request-id": "first,second"}, False),
        (200, {"other": "first", "Other": "second"}, False),
        (200, {"bad header": "value"}, False),
        (202, {"Location": OPERATION.replace("/operations/", "/../operations/")}, False),
        (202, {"Location": OPERATION.replace("/operations/", "/%2e%2e/operations/")}, False),
        (202, {"Location": OPERATION.replace("/operations/", "/%GG/operations/")}, False),
    ],
)
def test_bad_or_foreign_ack_no_replay_body_read_or_poll(sdk_owned, status, headers, foreign):
    probe = sdk_owned
    probe.transport.replies.append((status, headers, foreign))
    with pytest.raises(ValidationError):
        probe.adapter.start(Deadline.after(5))
    probe.adapter.reconcile(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 0
    assert probe.adapter.payload.acknowledgment is None
    assert len(probe.transport.calls) == probe.credential.calls == 1


@pytest.mark.parametrize("exception_type", [ServiceRequestError, ServiceResponseError, KeyboardInterrupt, SystemExit])
def test_transport_failure_or_control_no_replay(sdk_owned, exception_type):
    probe = sdk_owned
    primary = exception_type("offline dispatch failure")
    probe.transport.replies.append(primary)
    with pytest.raises(exception_type) as caught:
        probe.adapter.start(Deadline.after(5))
    assert caught.value is primary
    probe.adapter.reconcile(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert len(probe.transport.calls) == probe.credential.calls == 1
