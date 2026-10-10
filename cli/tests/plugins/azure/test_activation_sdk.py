"""Actual locked SDK public streaming dispatch, without sockets or real credentials."""

from __future__ import annotations

import ast
import gc
import inspect
import socket
import sys
import weakref
from contextlib import closing
from textwrap import dedent
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
        (200, {"other": "first", "Other": "second"}),
        (200, {"bad header": "value"}),
        (202, {"Location": OPERATION, "unrelated": None, 123: "\r\nforeign"}),
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
        (202, {"Azure-AsyncOperation": OPERATION, "azure-asyncoperation": OPERATION}, False),
        (202, {"Azure-AsyncOperation": 123}, False),
        (200, {"x-ms-request-id": "first,second"}, False),
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


@pytest.mark.parametrize("header", ["x-ms-request-id", "Location", "Azure-AsyncOperation"])
@pytest.mark.parametrize("value", [None, 123])
def test_selected_header_nonstring_is_not_absence(sdk_owned, header, value):
    probe = sdk_owned
    probe.transport.replies.append((200, {header: value}, False))
    with pytest.raises(ValidationError):
        probe.adapter.start(Deadline.after(5))
    assert probe.adapter.payload.acknowledgment is None
    probe.adapter.reconcile(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 0
    assert len(probe.transport.calls) == probe.credential.calls == 1


@pytest.fixture
def sdk_custody(tmp_path, monkeypatch):
    """Actual originals for deliberate cleanup uncertainty, without live resources."""
    transport, credential = ScriptedTransport(), Credential()
    clients = []
    original = _activation_client.compute_start_client

    def construct(selected, subscription):
        assert selected is credential and subscription == SUBSCRIPTION
        client = original(selected, subscription, transport=transport)
        clients.append(client)
        return client

    monkeypatch.setattr(_activation_client, "compute_start_client", construct)
    with closing(Database(tmp_path / "custody.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        adapter = AzureVMActivation(owner, "vm", credential, RESOURCE, ProviderLocator(f"azure-vm:{RESOURCE}"))
        yield SimpleNamespace(adapter=adapter, owner=owner, transport=transport, credential=credential, clients=clients)
    # Retained originals are inert scripted fixtures, never cleanup-retried by teardown.
    assert transport.closed or any(handle is clients[0] for handle in adapter._unclosed_handles)
    assert all(
        response.closed or any(handle is response for handle in adapter._unclosed_handles)
        for response in transport.responses
    )


def assert_ack_custody(probe: Any, expected: tuple[Any, ...]) -> None:
    adapter = probe.adapter
    assert len(adapter._unclosed_handles) == len(expected)
    assert all(actual is original for actual, original in zip(adapter._unclosed_handles, expected, strict=True))
    assert adapter.cleanup_incomplete is bool(expected)
    assert adapter.payload.acknowledgment.status_code == 202
    adapter.reconcile(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 1
    assert decode_activation_payload(row.payload).acknowledgment == adapter.payload.acknowledgment
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert len(probe.transport.calls) == probe.credential.calls == 1
    assert all(actual is original for actual, original in zip(adapter._unclosed_handles, expected, strict=True))


@pytest.mark.parametrize("boundary", ["before-first", "between", "bookkeeping", "control"])
@pytest.mark.parametrize("original_control", [False, True])
def test_actual_sdk_cleanup_boundary_interrupt_retains_originals(sdk_custody, monkeypatch, boundary, original_control):
    probe = sdk_custody
    probe.transport.replies.append((202, {"Location": OPERATION}, False))
    primary = KeyboardInterrupt("original publication control")
    cleanup_control = SystemExit("cleanup boundary control")
    if original_control:
        original_publish = OperationOwner.inspect_lifecycle_obligation
        calls: list[bool] = []

        def interrupt_publication(owner, obligation_id):
            result = original_publish(owner, obligation_id)
            if not calls:
                calls.append(True)
                raise primary
            return result

        monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", interrupt_publication)
    fired: list[bool] = []
    code = AzureVMActivation._close_handles.__code__

    def interrupt(frame, event, arg):
        if event == "line" and frame.f_code is code and not fired and boundary != "control":
            handle = frame.f_locals.get("handle")
            response = probe.transport.responses[0]
            eligible = (
                boundary == "before-first"
                and handle is response
                and not response.closed
                or boundary == "between"
                and handle is probe.clients[0]
                and not probe.transport.closed
                or boundary == "bookkeeping"
                and handle is response
                and response.closed
            )
            if eligible:
                fired.append(True)
                raise cleanup_control
        return interrupt

    previous = sys.gettrace()
    sys.settrace(interrupt)
    try:
        if original_control or boundary != "control":
            with pytest.raises(KeyboardInterrupt if original_control else SystemExit) as caught:
                probe.adapter.start(Deadline.after(5))
            assert caught.value is (primary if original_control else cleanup_control)
        else:
            assert probe.adapter.start(Deadline.after(5)).status_code == 202
    finally:
        sys.settrace(previous)
    assert bool(fired) is (boundary != "control")
    response, client = probe.transport.responses[0], probe.clients[0]
    expected = () if boundary == "control" else (client,) if boundary == "between" else (response, client)
    assert response.closed is (boundary != "before-first")
    assert probe.transport.closed is (boundary == "control")
    assert_ack_custody(probe, expected)


@pytest.mark.parametrize("handle", ["response", "client"])
@pytest.mark.parametrize("closed_first", [False, True])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("original_control", [False, True])
def test_actual_sdk_close_uncertainty_retains_identity(
    sdk_custody, monkeypatch, handle, closed_first, exception_type, original_control
):
    probe = sdk_custody
    probe.transport.replies.append((202, {"Location": OPERATION}, False))
    primary = exception_type("close uncertainty")
    publication_control = KeyboardInterrupt("original publication interruption")
    if original_control:
        original = OperationOwner.inspect_lifecycle_obligation
        interrupted: list[bool] = []

        def interrupt_publication(owner, obligation_id):
            result = original(owner, obligation_id)
            if not interrupted:
                interrupted.append(True)
                raise publication_control
            return result

        monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", interrupt_publication)
    close_calls = []

    def make_uncertain(target: Any) -> None:
        original = target.close

        def fail():
            close_calls.append(target)
            if closed_first:
                original()
            raise primary

        monkeypatch.setattr(target, "close", fail)

    if handle == "client":
        make_uncertain(probe.transport)
    else:
        original_send = probe.transport.send

        def send(*args, **kwargs):
            response = original_send(*args, **kwargs)
            make_uncertain(response)
            return response

        monkeypatch.setattr(probe.transport, "send", send)
    if original_control:
        with pytest.raises(KeyboardInterrupt) as caught:
            probe.adapter.start(Deadline.after(5))
        assert caught.value is publication_control
    elif exception_type is OSError:
        assert probe.adapter.start(Deadline.after(5)).status_code == 202
    else:
        with pytest.raises(exception_type) as caught:
            probe.adapter.start(Deadline.after(5))
        assert caught.value is primary
    response, client = probe.transport.responses[0], probe.clients[0]
    assert_ack_custody(probe, (response if handle == "response" else client,))
    assert len(close_calls) == 1
    assert response.closed is (closed_first if handle == "response" else True)
    assert probe.transport.closed is (closed_first if handle == "client" else True)


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("inject", [True, False])
def test_cleanup_entry_keeps_primary_and_custody(tmp_path, monkeypatch, primary_type, inject):
    transport, credential = ScriptedTransport(), Credential()
    transport.replies.append((202, {"Location": OPERATION}, False))
    client_refs = []
    factory = _activation_client.compute_start_client

    def construct(selected, subscription):
        client = factory(selected, subscription, transport=transport)
        client_refs.append(weakref.ref(client))
        return client

    monkeypatch.setattr(_activation_client, "compute_start_client", construct)
    primary, injected = primary_type("publication control"), SystemExit("cleanup entry control")
    original_inspect = OperationOwner.inspect_lifecycle_obligation
    published: list[bool] = []

    def interrupted_inspect(owner, obligation_id):
        result = original_inspect(owner, obligation_id)
        if not published:
            published.append(True)
            raise primary
        return result

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", interrupted_inspect)
    source, first_line = inspect.getsourcelines(AzureVMActivation.start)
    # Select the first cleanup-finally statement, including any wrapper entry before the call.
    entry = next(
        node.finalbody[0].lineno
        for node in ast.walk(ast.parse(dedent("".join(source))))
        if isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.Attribute) and child.attr == "_close_handles"
            for statement in node.finalbody
            for child in ast.walk(statement)
        )
    )
    entry += first_line - 1
    code = AzureVMActivation.start.__code__
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if inject and event == "line" and frame.f_code is code and frame.f_lineno == entry and not fired:
            assert frame.f_locals["control"] is primary
            fired.append(True)
            raise injected
        return interrupt

    with closing(Database(tmp_path / "entry.db")) as db:
        owner = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        adapter = AzureVMActivation(owner, "vm", credential, RESOURCE, ProviderLocator(f"azure-vm:{RESOURCE}"))
        previous, observed = sys.gettrace(), None
        sys.settrace(interrupt)
        try:
            adapter.start(Deadline.after(5))
        except BaseException as error:
            observed = error
        finally:
            sys.settrace(previous)
        response_ref = weakref.ref(transport.responses.pop())
        for exception in (primary, injected, observed):
            if exception is not None:
                exception.__traceback__ = None
        gc.collect()
        assert bool(fired) is inject and observed is primary
        assert not adapter._lock.locked()
        assert transport.closed is (not inject) and adapter.cleanup_incomplete is inject
        if inject:
            assert response_ref() is not None and client_refs[0]() is not None
            assert adapter._unclosed_handles[0] is response_ref() and adapter._unclosed_handles[1] is client_refs[0]()
            assert adapter._unclosed_handles[0] is not None
            assert len(adapter._unclosed_handles) == 2 and not adapter._unclosed_handles[0].closed
        else:
            assert adapter._unclosed_handles == () and response_ref() is None and client_refs[0]() is None
        row = owner.inspect_lifecycle_obligation(adapter.obligation_id)
        assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 0
        assert_ack_custody(
            SimpleNamespace(adapter=adapter, owner=owner, transport=transport, credential=credential),
            adapter._unclosed_handles,
        )
