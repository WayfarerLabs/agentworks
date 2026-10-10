"""Actual public Google-auth/Requests pipeline with inert scripted HTTP transports."""

from __future__ import annotations

import ast
import gc
import inspect
import io
import json
import sys
import weakref
from collections.abc import Callable
from contextlib import closing
from textwrap import dedent
from types import SimpleNamespace
from typing import Any

import pytest
import requests  # type: ignore[import-untyped]
from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from urllib3.response import HTTPResponse

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp._activation import MAX_BODY_BYTES, GCEActivation, decode_activation_payload, start_url

RETAINED_ADAPTERS: list[GCEActivation] = []


class Credential(Credentials):
    def __init__(self) -> None:
        initialize: Callable[[], None] = super().__init__
        initialize()
        self.token = "synthetic-offline-token"
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        response = request("https://credential.invalid/token", method="POST", body=b"synthetic")
        assert response.status == 200
        self.token = "synthetic-refreshed-token"


class Raw(HTTPResponse):
    def __init__(self, body, callback) -> None:
        super().__init__(body=io.BytesIO(body), preload_content=False)
        self.reads: list[int] = []
        self.callback = callback

    def read(self, amt=None, decode_content=None, cache_content=False):
        assert type(amt) is int and 0 < amt <= 8192 and decode_content is False
        self.reads.append(amt)
        data = super().read(amt, decode_content=decode_content, cache_content=cache_content)
        self.callback(data)
        return data


class Response(requests.Response):  # type: ignore[misc]
    @property
    def content(self):
        raise AssertionError("service body consumed outside bounded raw reader")


def body(adapter: GCEActivation) -> bytes:
    p = adapter.payload
    return json.dumps(
        {
            "kind": "compute#operation",
            "name": "Operation_1",
            "clientOperationId": p.request_id,
            "targetId": p.instance_id,
            "targetLink": f"https://compute.googleapis.com/compute/v1/projects/{p.project_id}/zones/{p.zone}"
            f"/instances/{p.instance_name}",
            "operationType": "start",
            "status": "DONE",
            "error": {"message": "discarded synthetic diagnostic"},
        }
    ).encode()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    from google.auth.transport import requests as auth

    p = SimpleNamespace(
        status=200,
        headers={},
        data=None,
        reply=None,
        service_calls=[],
        credential_calls=[],
        closes=[],
        sessions=[],
        responses=[],
        before_close=None,
        read_callback=lambda data: None,
    )
    ordinary, authorized = requests.Session, auth.AuthorizedSession
    init_authorized: Callable[..., None] = authorized.__init__
    labels: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
    close_session, close_response = requests.Session.close, requests.Response.close

    def credential_session():
        session = ordinary()
        p.sessions.append(("credential", session))
        labels[session] = "credential"
        p.credential_session = session
        return session

    def service_session(session, *args, **kwargs):
        assert kwargs["max_refresh_attempts"] == 0 and 0 < kwargs["refresh_timeout"] <= 5
        assert kwargs["auth_request"] is p.adapter._auth_request
        init_authorized(session, *args, **kwargs)
        p.sessions.append(("service", session))
        labels[session] = "service"
        p.service_session = session
        assert session.verify is True
        assert session.get_adapter("https://compute.googleapis.com").max_retries.total == 0

    def close(self):
        kind = labels.get(self)
        if kind is None:
            return close_session(self)
        p.closes.append(kind)
        if p.before_close:
            p.before_close(kind, self, lambda: close_session(self))
        else:
            close_session(self)

    def close_reply(self):
        p.closes.append("response")
        if p.before_close:
            p.before_close("response", self, lambda: close_response(self))
        else:
            close_response(self)

    def send(self, request, **kwargs):
        assert kwargs["verify"] is True and kwargs["proxies"] == {}
        assert all(0 < timeout <= 5 for timeout in kwargs["timeout"])
        if request.url == "https://credential.invalid/token":
            p.credential_calls.append((request.method, request.url))
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.raw = HTTPResponse(body=io.BytesIO(b"{}"), preload_content=False)
            return response
        assert request.method == "POST" and request.url == start_url(p.adapter.payload)
        assert request.body is None and request.headers["Accept-Encoding"] == "identity"
        assert kwargs["stream"] is True
        assert p.service_session.trust_env is False and p.credential_session.trust_env is False
        row = p.owner.inspect_lifecycle_obligation(p.adapter.obligation_id)
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_activation_payload(row.payload).request_id == p.adapter.payload.request_id
        p.service_calls.append((request.method, request.url))
        if p.reply is not None:
            raise p.reply
        response = Response()
        response.request, response.url, response.status_code = request, request.url, p.status
        response.headers.update(p.headers)
        response.raw = Raw(body(p.adapter) if p.data is None else p.data, p.read_callback)
        p.responses.append(response)
        return response

    monkeypatch.setattr(requests, "Session", credential_session)
    monkeypatch.setattr(authorized, "__init__", service_session)
    monkeypatch.setattr(ordinary, "close", close)
    monkeypatch.setattr(Response, "close", close_reply)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    p.credential = Credential()
    with closing(Database(tmp_path / "sdk.db")) as database:
        p.owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "db-vm"), "sdk")
        p.adapter = GCEActivation(
            p.owner,
            "db-vm",
            p.credential,
            "project",
            "us-central1-a",
            "backend",
            "123",
            ProviderLocator("gcp-gce:project:us-central1-a:123"),
        )
        yield p
        assert len(p.service_calls) <= 1
        assert all(response.raw.closed or p.adapter._response is response for response in p.responses)
        assert not p.adapter._lock.locked()
        # Teardown does not retry uncertain originals or trigger Request.__del__ on them.
        if p.adapter.cleanup_incomplete:
            RETAINED_ADAPTERS.append(p.adapter)
        print(
            "dispatch accounting:",
            len(p.service_calls),
            "service POST, 0 service GET;",
            len(p.credential_calls),
            "credential-provider POST; retained:",
            p.adapter.cleanup_incomplete,
        )


def assert_receipt(p: Any, acknowledged: bool) -> None:
    p.adapter.reconcile(Deadline.after(5))
    row = p.owner.inspect_lifecycle_obligation(p.adapter.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == int(acknowledged)
    assert (decode_activation_payload(row.payload).operation_name is not None) is acknowledged
    with pytest.raises(StateError):
        p.adapter.start(Deadline.after(5))
    assert len(p.service_calls) == 1


@pytest.mark.parametrize("refresh", [False, True])
def test_public_success_and_separately_attributed_credential_refresh(sdk, refresh):
    if refresh:
        sdk.credential.token = None
    assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    assert_receipt(sdk, True)
    assert len(sdk.credential_calls) == sdk.credential.refreshes == int(refresh)
    assert sdk.closes == ["response", "service", "credential"] and not sdk.adapter.cleanup_incomplete


@pytest.mark.parametrize(
    "status,headers",
    [
        (401, {}),
        (403, {}),
        (429, {"Retry-After": "1"}),
        (500, {}),
        (503, {}),
        (301, {"Location": "https://foreign.invalid/redirect"}),
        (302, {"Location": "https://foreign.invalid/redirect"}),
        (307, {"Location": "https://foreign.invalid/redirect"}),
        (308, {"Location": "https://foreign.invalid/redirect"}),
        (200, {"Content-Encoding": "gzip"}),
        (200, {"Content-Encoding": "br"}),
        (200, {"Content-Encoding": "identity,identity"}),
        (200, {"Content-Encoding": "identity,gzip"}),
        (200, {"Content-Encoding": ""}),
    ],
)
def test_hook_refuses_before_body_read_redirect_or_refresh_replay(sdk, status, headers):
    sdk.status, sdk.headers = status, headers
    with pytest.raises(ValidationError):
        sdk.adapter.start(Deadline.after(5))
    assert sdk.responses[0].raw.reads == []
    assert sdk.credential.refreshes == 0 and not sdk.credential_calls
    assert sdk.closes == ["response", "service", "credential"]
    assert_receipt(sdk, False)


@pytest.mark.parametrize("encoding", ["identity", "IDENTITY", " identity "])
def test_single_identity_encoding_uses_only_finite_raw_reads(sdk, encoding):
    sdk.headers = {"Content-Encoding": encoding, "Content-Length": "999999999"}
    assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    assert sdk.responses[0].raw.reads and all(0 < n <= 8192 for n in sdk.responses[0].raw.reads)


@pytest.mark.parametrize("exception_type", [requests.ConnectionError, requests.Timeout, KeyboardInterrupt, SystemExit])
def test_connection_failure_or_control_does_not_replay(sdk, exception_type):
    primary = exception_type("synthetic connection failure")
    sdk.reply = primary
    with pytest.raises(exception_type) as caught:
        sdk.adapter.start(Deadline.after(5))
    assert caught.value is primary and sdk.closes == ["service", "credential"]
    assert_receipt(sdk, False)


@pytest.mark.parametrize("overflow", [False, True])
def test_exact_body_capacity_and_one_overflow_byte(sdk, overflow):
    selected = body(sdk.adapter)
    sdk.data = selected + b" " * (MAX_BODY_BYTES + int(overflow) - len(selected))
    if overflow:
        with pytest.raises(ValidationError):
            sdk.adapter.start(Deadline.after(5))
    else:
        assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    reads = sdk.responses[0].raw.reads
    assert reads[-1] == 1 and sum(reads) == MAX_BODY_BYTES + 1
    assert_receipt(sdk, not overflow)


@pytest.mark.parametrize("complete", [False, True])
def test_original_deadline_partial_expiry_or_matching_late_ack(sdk, monkeypatch, complete):
    expired: list[bool] = []
    original = Deadline.remaining
    deadline = Deadline.after(5)

    def remaining(self):
        return 0 if self is deadline and expired else original(self)

    def read(data):
        if not complete or not data:
            expired.append(True)

    monkeypatch.setattr(Deadline, "remaining", remaining)
    sdk.read_callback = read
    with pytest.raises(TimeoutError):
        sdk.adapter.start(deadline)
    assert (sdk.adapter.payload.operation_name is not None) is complete
    assert_receipt(sdk, complete)


@pytest.mark.parametrize("handle", ["response", "service", "credential"])
@pytest.mark.parametrize("closed_first", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_uncertain_original_close_retained_once_with_wrapper(sdk, handle, closed_first, error_type):
    primary = error_type("uncertain original close")
    selected = []

    def close(kind, original, normally):
        if kind == handle:
            selected.append(original)
            if closed_first:
                normally()
            raise primary
        normally()

    sdk.before_close = close
    if error_type is OSError:
        assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    else:
        with pytest.raises(error_type) as caught:
            sdk.adapter.start(Deadline.after(5))
        assert caught.value is primary
    primary.__traceback__ = None
    field = {"response": "_response", "service": "_service_session", "credential": "_credential_session"}[handle]
    original_ref = weakref.ref(selected.pop())
    wrapper_ref = weakref.ref(sdk.adapter._auth_request)
    assert getattr(sdk.adapter, field) is original_ref() and sdk.adapter.cleanup_incomplete
    assert sdk.adapter._auth_request.session is sdk.credential_session
    sdk.sessions.clear()
    sdk.responses.clear()
    sdk.service_session = sdk.credential_session = None
    gc.collect()
    assert original_ref() is not None and wrapper_ref() is sdk.adapter._auth_request
    before = list(sdk.closes)
    assert_receipt(sdk, True)
    assert sdk.closes == before and sdk.closes.count(handle) == 1


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("boundary", ["entry", "before-close", "bookkeeping", "control"])
def test_cleanup_boundaries_keep_primary_custody_and_release_lock(sdk, monkeypatch, primary_type, boundary):
    primary, secondary = primary_type("publication control"), SystemExit("cleanup boundary control")
    original = OperationOwner.inspect_lifecycle_obligation
    calls: list[bool] = []

    def publication(owner, obligation_id):
        result = original(owner, obligation_id)
        if len(sdk.service_calls) == 1 and sdk.adapter.payload.operation_name is not None and not calls:
            calls.append(True)
            raise primary
        return result

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", publication)
    source, first = inspect.getsourcelines(GCEActivation.start)
    entry = (
        next(
            node.finalbody[0].lineno
            for node in ast.walk(ast.parse(dedent("".join(source))))
            if isinstance(node, ast.Try)
            and any(
                isinstance(child, ast.Attribute) and child.attr == "_close_handles"
                for statement in node.finalbody
                for child in ast.walk(statement)
            )
        )
        + first
        - 1
    )
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if event == "line" and not fired:
            eligible = boundary == "entry" and frame.f_code is GCEActivation.start.__code__ and frame.f_lineno == entry
            if frame.f_code is GCEActivation._close_handles.__code__:
                handle = frame.f_locals.get("handle")
                eligible |= boundary == "before-close" and handle is sdk.responses[0] and "response" not in sdk.closes
                eligible |= boundary == "bookkeeping" and handle is sdk.responses[0] and "response" in sdk.closes
            if eligible:
                fired.append(True)
                raise secondary
        return interrupt

    previous = sys.gettrace()
    sys.settrace(interrupt)
    try:
        with pytest.raises(primary_type) as caught:
            sdk.adapter.start(Deadline.after(5))
        assert caught.value is primary
    finally:
        sys.settrace(previous)
    for error in (primary, secondary):
        error.__traceback__ = None
    assert bool(fired) is (boundary != "control")
    assert sdk.adapter.cleanup_incomplete is (boundary != "control")
    assert not sdk.adapter._lock.locked()
    assert_receipt(sdk, True)


def test_hook_captures_original_when_public_method_raises_after_hook(sdk, monkeypatch):
    request: Callable[..., Any] = AuthorizedSession.request
    primary = KeyboardInterrupt("after hook")

    def interrupted(self, *args, **kwargs):
        request(self, *args, **kwargs)
        raise primary

    monkeypatch.setattr(AuthorizedSession, "request", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        sdk.adapter.start(Deadline.after(5))
    assert caught.value is primary and sdk.closes == ["response", "service", "credential"]
    assert_receipt(sdk, False)


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
def test_original_control_wins_over_all_close_failures(sdk, monkeypatch, primary_type):
    primary = primary_type("publication control")
    original = OperationOwner.inspect_lifecycle_obligation

    def publication(owner, obligation_id):
        result = original(owner, obligation_id)
        if sdk.adapter.payload.operation_name is not None:
            raise primary
        return result

    def fail(kind, handle, normally):
        raise {"response": OSError, "service": KeyboardInterrupt, "credential": SystemExit}[kind]("close uncertainty")

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", publication)
    sdk.before_close = fail
    with pytest.raises(primary_type) as caught:
        sdk.adapter.start(Deadline.after(5))
    assert caught.value is primary and sdk.closes == ["response", "service", "credential"]
    assert sdk.adapter._response is sdk.responses[0]
    assert sdk.adapter._service_session is sdk.service_session
    assert sdk.adapter._credential_session is sdk.credential_session
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", original)
    primary.__traceback__ = None
    before = list(sdk.closes)
    assert_receipt(sdk, True)
    assert sdk.closes == before


@pytest.mark.parametrize("handle", ["credential", "service", "response"])
@pytest.mark.parametrize("inject", [False, True])
def test_interrupt_after_retained_original_has_matched_control(sdk, handle, inject):
    primary = KeyboardInterrupt("after original handoff")
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if inject and event == "line" and not fired:
            adapter = sdk.adapter
            eligible = (
                handle == "credential"
                and frame.f_code is GCEActivation.start.__code__
                and adapter._credential_session is not None
                and adapter._auth_request is None
                or handle == "service"
                and frame.f_code is GCEActivation.start.__code__
                and adapter._service_session is not None
                and not sdk.service_calls
                or handle == "response"
                and frame.f_code is GCEActivation._retain_response.__code__
                and adapter._response is not None
            )
            if eligible:
                fired.append(True)
                raise primary
        return interrupt

    previous = sys.gettrace()
    sys.settrace(interrupt)
    try:
        if inject:
            with pytest.raises(KeyboardInterrupt) as caught:
                sdk.adapter.start(Deadline.after(5))
            assert caught.value is primary
        else:
            assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    finally:
        sys.settrace(previous)
    assert bool(fired) is inject and not sdk.adapter.cleanup_incomplete
    assert len(sdk.service_calls) == int(not inject or handle == "response")
    assert sdk.closes == (
        ["credential"]
        if inject and handle == "credential"
        else ["service", "credential"]
        if inject and handle == "service"
        else ["response", "service", "credential"]
    )
