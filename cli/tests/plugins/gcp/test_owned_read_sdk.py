"""Real public Requests and Google-auth with inert scripted HTTP adapters."""

from __future__ import annotations

import io
import json
import weakref
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import google.auth
import pytest
import requests  # type: ignore[import-untyped]
from google.auth.credentials import Credentials, TokenState
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from urllib3.response import HTTPResponse

from agentworks.db import VMStatus
from agentworks.errors import (
    AuthorizationError,
    LimitExceededError,
    NotFoundError,
    ProvisioningError,
    StateError,
    TokenRejectedError,
    ValidationError,
)
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._native_access import INSTANCE_FIELDS, MAX_BODY_BYTES
from agentworks.plugins.gcp.config import GcpServiceAccountAuth
from tests.plugins.gcp.test_owned_read import instance, make_reader

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.plugins.gcp._native_access import GCEOwnedRead


class FreshCredential(Credentials):
    def __init__(self) -> None:
        initialize = cast("Callable[[], None]", super().__init__)
        initialize()
        self.token = "offline-selected-token"

    def refresh(self, request: Any) -> None:
        pytest.fail("fresh credential refreshed")


class ServiceResponse(requests.Response):  # type: ignore[misc]
    @property
    def content(self):
        pytest.fail("service body consumed outside bounded raw reads")


@pytest.fixture
def sdk(monkeypatch):
    p = SimpleNamespace(
        now=1.0,
        body=json.dumps(instance()).encode(),
        status=200,
        headers={},
        credential=FreshCredential(),
        credential_status=200,
        credential_body=b'{"access_token":"offline-refreshed-token","expires_in":3600,"token_type":"Bearer"}',
        credential_redirects=0,
        credential_responses=0,
        sends=[],
        responses=[],
        sessions=[],
        closes=[],
        raw_reads=[],
        failed_close=None,
        close_control=None,
        send_control=None,
        on_service=lambda: None,
        after_raw_read=lambda: None,
        adc_calls=[],
        stock_default=google.auth.default,
    )
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: p.now)
    ordinary = requests.Session
    session_close = ordinary.close
    response_close = requests.Response.close
    labels: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()

    class Raw(HTTPResponse):
        def read(self, amt=None, decode_content=None, cache_content=False):
            assert type(amt) is int and 0 < amt <= 8192 and decode_content is False
            p.raw_reads.append(amt)
            data = super().read(amt, decode_content=decode_content, cache_content=cache_content)
            p.after_raw_read()
            return data

    def session():
        result = ordinary()
        label = "credential-session" if not p.sessions else "service-session"
        labels[result] = label
        p.sessions.append(result)
        return result

    def close_session(self):
        label = labels.get(self)
        if label is None:
            return session_close(self)
        p.closes.append((label, self))
        if p.failed_close == label:
            raise RuntimeError("scripted ordinary session close")
        if p.close_control is not None:
            raise p.close_control
        return session_close(self)

    def close_response(self):
        label = labels.get(self)
        if label is None:
            return response_close(self)
        p.closes.append((label, self))
        if p.failed_close == label:
            raise RuntimeError("scripted ordinary response close")
        if p.close_control is not None:
            raise p.close_control
        return response_close(self)

    def send(adapter, request, **kwargs):
        if p.send_control is not None:
            raise p.send_control
        assert kwargs["verify"] is not False
        assert kwargs["proxies"] == {}
        assert adapter.max_retries.total == 0
        parts = urlsplit(request.url)
        kind = "service" if parts.hostname == "compute.googleapis.com" else "credential"
        p.sends.append((kind, request, kwargs))
        if kind == "service":
            assert kwargs["verify"] is True
            assert request.method == "GET" and request.body is None
            assert parts.scheme == "https" and parts.netloc == "compute.googleapis.com"
            assert parts.path == "/compute/v1/projects/project/zones/us-central1-a/instances/backend"
            assert parse_qs(parts.query, strict_parsing=True) == {"fields": [INSTANCE_FIELDS]}
            assert kwargs["stream"] is True
            assert request.headers["Accept-Encoding"] == "identity"
            response = ServiceResponse()
            response.status_code = p.status
            response.headers.update(p.headers)
            response.raw = Raw(body=io.BytesIO(p.body), preload_content=False)
            p.on_service()
        else:
            response = requests.Response()
            response.status_code = p.credential_status
            response.raw = HTTPResponse(body=io.BytesIO(p.credential_body), preload_content=False)
            if p.credential_responses < p.credential_redirects:
                response.status_code = 302
                response.headers["Location"] = "https://credential.invalid/redirected"
            p.credential_responses += 1
        response.url = request.url
        response.request = request
        labels[response] = f"{kind}-response"
        p.responses.append(response)
        return response

    def default(**kwargs):
        p.adc_calls.append(kwargs)
        assert isinstance(kwargs["request"], Request)
        assert kwargs["request"].session is p.sessions[0]
        return p.credential, "unrelated-detected-project"

    monkeypatch.setattr("requests.Session", session)
    monkeypatch.setattr(ordinary, "close", close_session)
    monkeypatch.setattr("requests.Response.close", close_response)
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", send)
    monkeypatch.setattr("google.auth.default", default)
    yield p
    # Restore ordinary closure behavior before disposing inert test originals.
    p.failed_close = None
    p.close_control = None
    for response in p.responses:
        response_close(response)
    for retained in p.sessions:
        session_close(retained)


def observe(p: Any) -> tuple[GCEOwnedRead, VMStatus]:
    selected, _ = make_reader()
    p.reader = selected
    return selected, selected.observe_power(Deadline.after(5))


def test_public_pipeline_exact_get_retirement_and_repeated_explicit_observation(sdk):
    reader, power = observe(sdk)
    assert power is VMStatus.RUNNING
    assert len(sdk.sends) == 1
    assert sdk.adc_calls[0]["scopes"] == ("https://www.googleapis.com/auth/cloud-platform",)
    credential_session = sdk.sessions[0]
    assert credential_session.trust_env is False
    assert not reader.cleanup_incomplete
    retained_request = reader._auth._request
    assert retained_request is not None and retained_request.session is credential_session
    sdk.now = 3
    assert reader.observe_locator(Deadline.after(7)).token == "gcp-gce:project:us-central1-a:123"
    assert len(sdk.adc_calls) == 1 and len(sdk.sends) == 2
    assert sdk.sessions[-1] is not sdk.sessions[1]
    assert reader.close(Deadline.after(5))
    assert not reader.cleanup_incomplete


@pytest.mark.parametrize(
    "status,error",
    [
        (301, ValidationError),
        (302, ValidationError),
        (307, ValidationError),
        (401, TokenRejectedError),
        (403, AuthorizationError),
        (404, NotFoundError),
        (500, ValidationError),
    ],
)
def test_error_hook_refuses_before_body_or_redirect(sdk, status, error):
    sdk.status = status
    sdk.headers = {"Location": "https://foreign.invalid/replay"}
    reader, _ = make_reader()
    with pytest.raises(error):
        reader.observe_power(Deadline.after(5))
    assert len(sdk.sends) == 1 and not sdk.raw_reads
    assert not reader.cleanup_incomplete
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip", "identity, identity"])
def test_encoded_body_refused_before_ingestion(sdk, encoding):
    sdk.headers = {"Content-Encoding": encoding}
    reader, _ = make_reader()
    with pytest.raises(ValidationError):
        reader.observe_locator(Deadline.after(5))
    assert not sdk.raw_reads
    assert reader.close(Deadline.after(5))


def test_public_pipeline_body_bound_ignores_content_length(sdk):
    sdk.body = b"x" * (MAX_BODY_BYTES + 1)
    sdk.headers = {"Content-Length": "1"}
    reader, _ = make_reader()
    with pytest.raises(ValidationError):
        reader.observe_power(Deadline.after(5))
    assert sum(sdk.raw_reads) == MAX_BODY_BYTES + 1
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("stage", ["send", "body"])
def test_late_success_refused_against_original_deadline(sdk, stage):
    def expire():
        sdk.now = 99

    if stage == "send":
        sdk.on_service = expire
    else:
        sdk.after_raw_read = expire
    reader, _ = make_reader()
    with pytest.raises(LimitExceededError):
        reader.observe_power(Deadline.after(5))
    assert len(sdk.sends) == 1
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("kind", ["service-response", "service-session"])
def test_useful_read_retains_uncertain_original_and_refuses_next_read(sdk, kind):
    sdk.failed_close = kind
    reader, power = observe(sdk)
    assert power is VMStatus.RUNNING and reader.cleanup_incomplete
    original = reader._response if kind == "service-response" else reader._service_session
    assert original is not None
    with pytest.raises(StateError):
        reader.observe_locator(Deadline.after(5))
    assert len(sdk.sends) == 1
    sdk.failed_close = None
    assert reader.close(Deadline.after(5))
    closed_originals = [handle for label, handle in sdk.closes if label == kind]
    assert closed_originals == [original, original]
    assert len(sdk.sends) == 1


def test_final_credential_session_failure_is_visible_and_retryable(sdk):
    reader, _ = observe(sdk)
    sdk.failed_close = "credential-session"
    assert not reader.close(Deadline.after(5))
    assert reader.cleanup_incomplete
    original = reader._auth._session
    retained_request = reader._auth._request
    assert retained_request is not None and retained_request.session is original
    sdk.failed_close = None
    assert reader.close(Deadline.after(5))
    assert len(sdk.sends) == 1


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_stage", ["close", "entry", "end", "release"])
def test_original_dispatch_control_survives_cleanup_boundaries(sdk, monkeypatch, control_type, cleanup_stage):
    primary = control_type("dispatch-control")
    secondary = SystemExit("cleanup-control")
    sdk.send_control = primary
    reader, _ = make_reader()
    if cleanup_stage == "close":
        sdk.close_control = secondary
    elif cleanup_stage == "entry":
        monkeypatch.setattr(reader, "_close_handles", lambda **kwargs: (_ for _ in ()).throw(secondary))
    elif cleanup_stage == "end":
        monkeypatch.setattr(reader._auth, "end", lambda: (_ for _ in ()).throw(secondary))
    else:
        original_lock = reader._lock

        class Lock:
            def acquire(self, **kwargs):
                return original_lock.acquire(**kwargs)

            def release(self):
                original_lock.release()
                raise secondary

        monkeypatch.setattr(reader, "_lock", Lock())
    with pytest.raises(control_type) as caught:
        reader.observe_power(Deadline.after(5))
    assert caught.value is primary


@pytest.fixture(scope="module")
def sa_document():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        "type": "service_account",
        "project_id": "credential-project",
        "private_key_id": "offline-key",
        "private_key": pem,
        "client_email": "offline@credential-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
        "quota_project_id": "credential-quota",
    }


def test_real_service_account_refresh_apply_without_dispatch_rab(sdk, monkeypatch, sa_document):
    from google.auth import credentials

    monkeypatch.setattr(
        credentials.CredentialsWithRegionalAccessBoundary,
        "_after_refresh",
        lambda *args: pytest.fail("service dispatch used before_request/RAB hook"),
    )
    auth = GcpServiceAccountAuth(mode="service-account", secret="sa-json")
    reader, _ = make_reader(auth=auth, secret=lambda name: json.dumps(sa_document))
    assert reader.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    selected = reader._auth._credential
    assert isinstance(selected, service_account.Credentials)
    assert selected.scopes == ("https://www.googleapis.com/auth/cloud-platform",)
    assert selected.service_account_email == sa_document["client_email"]
    assert selected.token_state is TokenState.FRESH
    assert selected.token == "offline-refreshed-token" and selected.expiry is not None
    assert [kind for kind, _, _ in sdk.sends] == ["credential", "service"]
    assert sdk.sends[-1][1].headers["Authorization"] == "Bearer offline-refreshed-token"
    assert not sdk.adc_calls
    assert reader.observe_locator(Deadline.after(5)).token.endswith(":123")
    assert [kind for kind, _, _ in sdk.sends] == ["credential", "service", "service"]
    assert reader.close(Deadline.after(5))


def test_stock_adc_real_authorized_user_document_preserves_scope_and_quota(sdk, monkeypatch, tmp_path):
    import google.auth

    document = tmp_path / "adc.json"
    document.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "offline-client",
                "client_secret": "offline-secret",
                "refresh_token": "offline-refresh",
                "quota_project_id": "document-quota",
                "scopes": ["selected-consent"],
            }
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(document))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "discovered-other-project")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "no-cloud-sdk"))
    monkeypatch.delenv("GOOGLE_CLOUD_QUOTA_PROJECT", raising=False)
    monkeypatch.setattr(google.auth, "default", sdk.stock_default)
    reader, _ = make_reader()
    assert reader.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    selected = reader._auth._credential
    assert selected.scopes == ["selected-consent"]
    assert selected.quota_project_id == "document-quota"
    assert sdk.sends[-1][1].headers["x-goog-user-project"] == "document-quota"
    assert reader.close(Deadline.after(5))


def test_real_service_account_selected_quota_and_stale_refresh(sdk, sa_document):
    from datetime import UTC, datetime, timedelta

    factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
    selected = factory(sa_document, scopes=("https://www.googleapis.com/auth/cloud-platform",)).with_quota_project(
        "selected-quota"
    )
    selected.token = "stale-selected-token"
    selected.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30)
    initial_state = selected.token_state
    assert initial_state is TokenState.STALE
    sdk.credential = selected
    reader, power = observe(sdk)
    assert power is VMStatus.RUNNING
    assert selected.token_state is TokenState.FRESH
    assert [kind for kind, _, _ in sdk.sends] == ["credential", "service"]
    assert sdk.sends[-1][1].headers["x-goog-user-project"] == "selected-quota"
    assert reader._auth._credential is selected
    assert reader.close(Deadline.after(5))


def test_adc_late_return_retains_selected_credential_before_refusal(sdk, monkeypatch):
    selected = sdk.credential

    def late_default(**kwargs):
        sdk.now = 99
        return selected, "unused-project"

    monkeypatch.setattr("google.auth.default", late_default)
    reader, _ = make_reader()
    with pytest.raises(LimitExceededError):
        reader.observe_power(Deadline.after(5))
    assert reader._auth._credential is selected
    assert not sdk.sends
    assert reader.close(Deadline.after(5))


def test_adc_failure_is_safe_and_not_reacquired(sdk, monkeypatch):
    calls = []

    def failed_default(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("offline-provider-private-sentinel")

    monkeypatch.setattr("google.auth.default", failed_default)
    reader, _ = make_reader()
    with pytest.raises(ProvisioningError) as caught:
        reader.observe_power(Deadline.after(5))
    assert "offline-provider-private-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    with pytest.raises(StateError):
        reader.observe_power(Deadline.after(5))
    assert len(calls) == 1 and len(sdk.sessions) == 1 and not sdk.sends
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("secret", ["not-json", "[]", '{"private_key":"offline-private-sentinel"}'])
def test_explicit_failure_never_falls_back_or_restarts_acquisition(sdk, secret):
    reader, _ = make_reader(
        auth=GcpServiceAccountAuth(mode="service-account", secret="sa-json"), secret=lambda name: secret
    )
    with pytest.raises(ProvisioningError) as caught:
        reader.observe_power(Deadline.after(5))
    assert "offline-private-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    with pytest.raises(StateError):
        reader.observe_power(Deadline.after(5))
    assert not sdk.adc_calls and not sdk.sends and len(sdk.sessions) == 1
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("timeout", [None, 120, (120, None)])
def test_real_request_type_session_security_kwargs_and_operation_binding(sdk, timeout):
    reader, _ = observe(sdk)
    request = reader._auth._request
    assert isinstance(request, Request) and request.session is sdk.sessions[0]
    with pytest.raises(StateError):
        request("https://credential.invalid/token")
    reader._auth.begin(Deadline.after(3))
    try:
        result = request(
            "https://credential.invalid/token",
            timeout=timeout,
            cert=("offline-cert", "offline-key"),
            verify="offline-ca",
        )
        assert result.status == 200 and result.data
        kwargs = sdk.sends[-1][2]
        assert kwargs["cert"] == ("offline-cert", "offline-key") and kwargs["verify"] == "offline-ca"
        bounded = kwargs["timeout"]
        assert bounded == (3, 3) if isinstance(timeout, tuple) else bounded == 3
        with pytest.raises(StateError):
            reader._auth.begin(Deadline.after(30))
    finally:
        reader._auth.retire_responses()
        reader._auth.end()
    assert reader.close(Deadline.after(5))


def test_uncertain_credential_response_blocks_next_owned_request_without_overwrite(sdk):
    reader, _ = observe(sdk)
    reader._auth.begin(Deadline.after(5))
    try:
        request = reader._auth._request
        assert isinstance(request, Request)
        assert request("https://credential.invalid/token").status == 200
        original = reader._auth._responses[0]
        sdk.failed_close = "credential-response"
        with pytest.raises(StateError):
            request("https://credential.invalid/second")
        assert reader._auth._responses == [original]
        assert len(sdk.sends) == 2
    finally:
        reader._auth.end()
    assert reader.cleanup_incomplete
    with pytest.raises(StateError):
        reader.observe_locator(Deadline.after(5))
    sdk.failed_close = None
    assert reader.close(Deadline.after(5))
    assert len(sdk.sends) == 2


@pytest.mark.parametrize("failure", [None, "ordinary", "control"])
def test_real_request_redirect_chain_retains_all_originals_until_sdk_returns(sdk, failure):
    reader, _ = observe(sdk)
    reader._auth.begin(Deadline.after(5))
    sdk.credential_redirects = 2
    try:
        request = reader._auth._request
        assert isinstance(request, Request) and request.session.max_redirects == 30
        result = request("https://credential.invalid/start")
        assert result.status == 200 and result.data
        originals = tuple(reader._auth._responses)
        assert len(originals) == 3
        assert [response.status_code for response in originals] == [302, 302, 200]
        assert len(sdk.sends) == 4
        control = KeyboardInterrupt("credential-close-control")
        if failure == "ordinary":
            sdk.failed_close = "credential-response"
        elif failure == "control":
            sdk.close_control = control
        if failure == "control":
            with pytest.raises(KeyboardInterrupt) as caught:
                reader._auth.retire_responses()
            assert caught.value is control
        else:
            reader._auth.retire_responses()
        if failure is not None:
            assert tuple(reader._auth._responses) == originals
            assert all(any(handle is response for _, handle in sdk.closes) for response in originals)
            with pytest.raises(StateError):
                request("https://credential.invalid/not-admitted")
            assert len(sdk.sends) == 4
        else:
            assert not reader._auth.cleanup_incomplete
    finally:
        reader._auth.end()
    sdk.failed_close = None
    sdk.close_control = None
    assert reader.close(Deadline.after(5))
    assert len(sdk.sends) == 4


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), (1, 2, 3)])
def test_invalid_credential_timeout_refuses_before_transport(sdk, timeout):
    reader, _ = observe(sdk)
    reader._auth.begin(Deadline.after(5))
    try:
        request = reader._auth._request
        assert isinstance(request, Request)
        with pytest.raises(ValidationError):
            request("https://credential.invalid/token", timeout=timeout)
        assert len(sdk.sends) == 1
    finally:
        reader._auth.end()
    assert reader.close(Deadline.after(5))
