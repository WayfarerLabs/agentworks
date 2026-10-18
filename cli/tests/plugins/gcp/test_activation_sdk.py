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
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import google.auth
import pytest
import requests  # type: ignore[import-untyped]
from google.auth.credentials import Credentials
from google.oauth2 import service_account
from urllib3.response import HTTPResponse

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp._activation import MAX_BODY_BYTES, GCEActivation, decode_activation_payload
from agentworks.plugins.gcp._owned_auth import _OwnedAuth, _OwnedRequest
from agentworks.plugins.gcp.config import GcpAmbientAuth
from tests.plugins.gcp.test_owned_read_sdk import sa_document as sa_document

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
        stream = io.BytesIO(body) if isinstance(body, bytes) else body
        super().__init__(body=stream, preload_content=False)
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
    p = SimpleNamespace(
        status=200,
        headers={},
        data=None,
        reply=None,
        service_calls=[],
        credential_calls=[],
        boundary_calls=[],
        boundary_entered=Event(),
        closes=[],
        sessions=[],
        responses=[],
        before_close=None,
        read_callback=lambda data: None,
    )
    ordinary = requests.Session
    labels: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
    close_session, close_response = requests.Session.close, requests.Response.close

    def credential_session():
        session = ordinary()
        kind = "credential" if not p.sessions else "service"
        p.sessions.append((kind, session))
        labels[session] = kind
        if kind == "credential":
            p.credential_session = session
        else:
            p.service_session = session
        return session

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
        timeout = kwargs["timeout"]
        assert all(0 < value <= 5 for value in (timeout if isinstance(timeout, tuple) else (timeout,)))
        if request.url.endswith("/allowedLocations"):
            assert request.method == "GET"
            p.boundary_calls.append((request.method, request.url))
            p.boundary_entered.set()
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.raw = HTTPResponse(body=io.BytesIO(b'{"encodedLocations":"0x1"}'), preload_content=False)
            return response
        if request.url == "https://credential.invalid/token":
            row = p.owner.inspect_lifecycle_obligation(p.adapter.obligation_id)
            assert row is not None and row.state is LifecycleObligationState.REGISTERED
            p.credential_calls.append((request.method, request.url))
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.raw = HTTPResponse(body=io.BytesIO(b"{}"), preload_content=False)
            return response
        base = "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a"
        expected = (
            f"{base}/instances/backend/start?requestId={p.adapter.payload.request_id}"
            if request.method == "POST"
            else f"{base}/operations/Operation_1"
        )
        assert request.method in ("POST", "GET") and request.url == expected
        assert request.body is None and request.headers["Accept-Encoding"] == "identity"
        if isinstance(p.credential, service_account.Credentials):
            assert p.credential.token is not None
            assert request.headers["Authorization"] == "Bearer " + p.credential.token
            assert request.headers["x-goog-user-project"] == p.credential.quota_project_id
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
    monkeypatch.setattr(ordinary, "close", close)
    monkeypatch.setattr(Response, "close", close_reply)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    p.credential = Credential()
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (p.credential, "ignored"))
    p.auth = _OwnedAuth("db-vm", GcpAmbientAuth(mode="ambient"), SimpleNamespace(), "site")
    with closing(Database(tmp_path / "sdk.db")) as database:
        p.owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "db-vm"), "sdk")
        p.adapter = GCEActivation(
            p.owner,
            "db-vm",
            p.auth,
            "project",
            "us-central1-a",
            "backend",
            "123",
            ProviderLocator("gcp-gce:project:us-central1-a:123"),
        )
        yield p
        assert sum(method == "POST" for method, _ in p.service_calls) <= 1
        assert all(response.raw.closed or p.adapter._response is response for response in p.responses)
        assert not p.adapter._lock.locked()
        # Teardown does not retry uncertain originals or trigger Request.__del__ on them.
        if p.adapter.cleanup_incomplete or p.auth.cleanup_incomplete:
            RETAINED_ADAPTERS.append(p.adapter)
        else:
            p.before_close = None
            p.auth.close(Deadline.after(5))
        print(
            "dispatch accounting:",
            sum(method == "POST" for method, _ in p.service_calls),
            "service POST;",
            sum(method == "GET" for method, _ in p.service_calls),
            "service GET;",
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
    assert sdk.closes == ["response", "service"] and not sdk.adapter.cleanup_incomplete


def test_real_selected_service_account_post_preserves_default_boundary_and_shared_lifetime(sdk, sa_document):
    factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
    selected = factory(sa_document, scopes=("https://www.googleapis.com/auth/cloud-platform",)).with_quota_project(
        "selected-quota"
    )
    selected.token = "selected-service-account-token"
    sdk.credential = selected
    assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    assert sdk.boundary_entered.wait(5)
    assert sdk.adapter.close(Deadline.after(5))
    retained_request = sdk.auth._request
    assert not sdk.auth.closed and retained_request is not None
    assert retained_request.session is sdk.credential_session
    sdk.auth.close(Deadline.after(5))
    assert selected.regional_access_boundary == "0x1"
    assert sdk.auth._credential is selected
    assert len(sdk.service_calls) == len(sdk.boundary_calls) == 1 and not sdk.credential_calls
    assert_receipt(sdk, True)


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
    assert sdk.closes == ["response", "service"]
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
    assert caught.value is primary and sdk.closes == ["service"]
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
    with pytest.raises(LimitExceededError):
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
    if handle == "credential":
        assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    if error_type is OSError:
        if handle == "credential":
            sdk.auth.close(Deadline.after(5))
        else:
            assert sdk.adapter.start(Deadline.after(5)) == "Operation_1"
    else:
        with pytest.raises(error_type) as caught:
            if handle == "credential":
                sdk.auth.close(Deadline.after(5))
            else:
                sdk.adapter.start(Deadline.after(5))
        assert caught.value is primary
    primary.__traceback__ = None
    field = {"response": "_response", "service": "_service_session"}.get(handle)
    original_ref = weakref.ref(selected.pop())
    wrapper_ref = weakref.ref(sdk.auth._request)
    if field is None:
        assert sdk.auth._request.session is original_ref() and sdk.auth.cleanup_incomplete
        assert not sdk.adapter.cleanup_incomplete
    else:
        assert getattr(sdk.adapter, field) is original_ref() and sdk.adapter.cleanup_incomplete
    assert sdk.auth._request.session is sdk.credential_session
    sdk.sessions.clear()
    sdk.responses.clear()
    sdk.service_session = sdk.credential_session = None
    gc.collect()
    assert original_ref() is not None and wrapper_ref() is sdk.auth._request
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
    source, first = inspect.getsourcelines(GCEActivation._request)
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
            eligible = (
                boundary == "entry" and frame.f_code is GCEActivation._request.__code__ and frame.f_lineno == entry
            )
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
    request: Callable[..., Any] = requests.sessions.Session.request
    primary = KeyboardInterrupt("after hook")

    def interrupted(self, *args, **kwargs):
        request(self, *args, **kwargs)
        raise primary

    monkeypatch.setattr(requests.sessions.Session, "request", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        sdk.adapter.start(Deadline.after(5))
    assert caught.value is primary and sdk.closes == ["response", "service"]
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
    assert caught.value is primary and sdk.closes == ["response", "service"]
    assert sdk.adapter._response is sdk.responses[0]
    assert sdk.adapter._service_session is sdk.service_session
    assert sdk.auth._request.session is sdk.credential_session
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
                and frame.f_code is _OwnedRequest.attach.__code__
                and frame.f_locals.get("self") is adapter._auth._request
                and adapter._auth._request.session is not None
                or handle == "service"
                and frame.f_code is GCEActivation._request.__code__
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
        []
        if inject and handle == "credential"
        else ["service"]
        if inject and handle == "service"
        else ["response", "service"]
    )
