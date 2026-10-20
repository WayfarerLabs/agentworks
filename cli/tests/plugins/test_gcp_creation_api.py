"""Original Requests custody and selected credential use for creation REST calls."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import google.auth
import pytest
import requests  # type: ignore[import-untyped]
from urllib3.response import HTTPResponse

from agentworks.capabilities.base import RunContext
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._creation_api import ROOT, CreationAPI
from agentworks.plugins.gcp.config import GcpGCEConfig
from tests.plugins.gcp.test_owned_read_sdk import FreshCredential


@pytest.fixture
def sdk(monkeypatch):
    p = SimpleNamespace(
        sessions=[], responses=[], sends=[], credential_calls=[], body=b'{"id":"123"}',
        status=200, encoding=None, close_failures=0,
    )
    session_factory = requests.Session
    close_response = requests.Response.close

    class Response(requests.Response):
        @property
        def content(self):
            pytest.fail("creation response bypassed bounded raw ingestion")

        def close(self):
            if p.close_failures:
                p.close_failures -= 1
                raise RuntimeError("original response close failed")
            return close_response(self)

    class Raw(HTTPResponse):
        def read(self, amt=None, decode_content=None, cache_content=False):
            assert type(amt) is int and 0 < amt <= 8192 and decode_content is False
            return super().read(amt, decode_content=decode_content, cache_content=cache_content)

    def session():
        original = session_factory()
        p.sessions.append(original)
        return original

    def send(adapter, request, **kwargs):
        p.sends.append(request)
        response = Response()
        response.status_code = p.status
        response.request = request
        response.url = request.url
        response.headers = {} if p.encoding is None else {"Content-Encoding": p.encoding}
        response.raw = Raw(body=io.BytesIO(p.body), preload_content=False)
        p.responses.append(response)
        return response

    def default(**kwargs):
        p.credential_calls.append(kwargs)
        return FreshCredential(), "unused-detected-project"

    monkeypatch.setattr(requests, "Session", session)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(google.auth, "default", default)
    p.api = CreationAPI("sample", GcpGCEConfig(name="gcp-gce", project_id="project", zone="us-central1-a"), RunContext(), "offline")
    return p


def test_constructor_is_passive_and_consumer_retains_originals(sdk):
    p = sdk
    assert not p.sessions and not p.credential_calls
    deadline = Deadline.after(5)
    with p.api.exchange("POST", "projects/project/global/firewalls?requestId=uuid", deadline, body={"name": "rule"}) as value:
        assert value == {"id": "123"}
        assert p.api._response is p.responses[0] and p.api._session is p.sessions[1]
        assert p.api.auth._request.session is p.sessions[0]
    assert p.api._response is None and p.api._session is None
    assert len(p.sends) == 1 and p.sends[0].method == "POST"
    assert p.sends[0].url.startswith(ROOT) and "Authorization" in p.sends[0].headers
    assert p.api.settle(deadline, final=True)
    assert p.api.auth.closed


def test_failed_original_response_close_blocks_new_requests_then_retires(sdk):
    p = sdk
    p.close_failures = 1
    deadline = Deadline.after(5)
    assert p.api.get("projects/project", deadline) == {"id": "123"}
    assert p.api._response is p.responses[0] and p.api.cleanup_incomplete
    with pytest.raises(StateError):
        p.api.get("projects/project", deadline)
    assert len(p.sends) == 1
    assert p.api.settle(deadline, final=False)
    assert not p.api.cleanup_incomplete


@pytest.mark.parametrize("status,encoding,body", [
    (302, None, b'{}'), (200, "gzip", b'{}'), (200, None, b'{"id":1,"id":2}'),
    (200, None, json.dumps({"data": "x" * 65_536}).encode()),
])
def test_external_status_encoding_and_ingestion_fail_closed(sdk, status, encoding, body):
    p = sdk
    p.status, p.encoding, p.body = status, encoding, body
    with pytest.raises(ValidationError):
        p.api.get("projects/project", Deadline.after(5))
    assert len(p.sends) == 1
    assert p.api._response is None and p.api._session is None


def test_control_in_receipt_consumer_preserves_same_control_and_custody(sdk):
    p = sdk
    primary = KeyboardInterrupt("original consumer control")
    p.close_failures = 1
    with pytest.raises(KeyboardInterrupt) as caught:
        with p.api.exchange("POST", "projects/project/global/firewalls", Deadline.after(5), body={}):
            raise primary
    assert caught.value is primary and p.api._response is p.responses[0]
    assert len(p.sends) == 1
    assert p.api.settle(Deadline.after(5), final=True)


def test_only_explicit_missing_get_accepts_not_found(sdk):
    p = sdk
    p.status = 404
    assert p.api.get("projects/project/global/firewalls/rule", Deadline.after(5), missing=True) is None
    with pytest.raises(ValidationError):
        with p.api.exchange("DELETE", "projects/project/global/firewalls/rule", Deadline.after(5), missing=True):
            pytest.fail("DELETE 404 was admitted as absence proof")
