"""Explicit selected Operation observation and local-only fenced settlement."""

from __future__ import annotations

import json
from dataclasses import replace
from http.client import HTTPResponse
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.db.operations import LifecycleObligation as PersistedLifecycleObligation
from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation import (
    ActivationPayload,
    ActivationState,
    decode_activation_payload,
    encode_activation_payload,
)
from agentworks.plugins.gcp._activation_operation import decode_completion
from tests.plugins.gcp.test_activation import PAYLOAD, operation
from tests.plugins.gcp.test_activation_correction import InertSocket
from tests.plugins.gcp.test_activation_sdk import isolated_config as isolated_config
from tests.plugins.gcp.test_activation_sdk import sdk as sdk

SELECTED = replace(PAYLOAD, operation_name="Operation_1")


def completion(payload: ActivationPayload = SELECTED, status: str = "DONE", **fields: Any) -> dict[str, Any]:
    return operation(payload) | {"status": status} | fields


def receipt(sdk: Any) -> PersistedLifecycleObligation:
    result: PersistedLifecycleObligation | None = sdk.owner.inspect_lifecycle_obligation(sdk.adapter.obligation_id)
    assert result is not None
    return result


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "DONE"])
@pytest.mark.parametrize("host", ["https://compute.googleapis.com", "https://www.googleapis.com"])
def test_completion_states_and_exact_optional_links(status, host):
    zone = f"{host}/compute/v1/projects/project/zones/us-central1-a"
    value = completion(status=status, zone=zone, selfLink=f"{zone}/operations/Operation_1")
    value["targetLink"] = f"{zone}/instances/backend"
    expected = ActivationState.SUCCEEDED if status == "DONE" else ActivationState.PENDING
    assert decode_completion(json.dumps(value).encode(), SELECTED) is expected


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "compute#instance"},
        {"name": "Operation_2"},
        {"name": "../Operation_1"},
        {"clientOperationId": "00000000-0000-0000-0000-000000000000"},
        {"clientOperationId": PAYLOAD.request_id.upper()},
        {"targetId": "0123"},
        {"targetId": 123},
        {
            "targetLink": "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/instances/other"
        },
        {
            "targetLink": "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/instances/backend/"
        },
        {"operationType": "stop"},
        {"operationType": None},
        {"status": "done"},
        {"status": "UNKNOWN"},
        {"status": None},
        {"status": ["DONE"]},
        {"zone": "https://compute.googleapis.com/compute/v1/projects/project/zones/other"},
        {"zone": "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/"},
        {"selfLink": "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/operations/other"},
        {"selfLink": "https://foreign.invalid/compute/v1/projects/project/zones/us-central1-a/operations/Operation_1"},
        {"error": None},
        {"error": []},
        {"error": {}},
        {"error": {"errors": []}},
        {"error": {"errors": {"code": "FAILED"}}},
        {"error": {"errors": [None]}},
        {"error": {"errors": [{}]}},
        {"error": {"errors": [{"code": ""}]}},
        {"error": {"errors": [{"code": 123}]}},
        {"error": {"errors": [{"code": "FAILED"}, {"code": ""}]}},
        {"httpErrorStatusCode": 500},
        {"httpErrorMessage": "FAILED"},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorStatusCode": True},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorStatusCode": "500"},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorStatusCode": 200},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorStatusCode": 600},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorMessage": {}},
        {"error": {"errors": [{"code": "FAILED"}]}, "httpErrorMessage": ""},
        {"status": "PENDING", "error": {"errors": [{"code": "FAILED"}]}},
        {"status": "RUNNING", "httpErrorStatusCode": 500},
    ],
)
def test_malformed_or_foreign_operation_cannot_certify_completion(fields):
    with pytest.raises(ValidationError):
        decode_completion(json.dumps(completion(**fields)).encode(), SELECTED)


@pytest.mark.parametrize(
    "missing", ["kind", "name", "clientOperationId", "targetId", "targetLink", "operationType", "status"]
)
def test_required_completion_fields(missing):
    value = completion()
    del value[missing]
    with pytest.raises(ValidationError):
        decode_completion(json.dumps(value).encode(), SELECTED)


@pytest.mark.parametrize("extra", [b',"status":"DONE"}', b',"extra":NaN}', b',"extra":Infinity}'])
def test_duplicate_keys_and_invalid_json_in_discarded_fields(extra):
    with pytest.raises(ValidationError):
        decode_completion(json.dumps(completion()).encode()[:-1] + extra, SELECTED)


def test_well_formed_failure_discards_diagnostics():
    value = completion(
        error={
            "errors": [{"code": "FAILED", "message": "synthetic-private-diagnostic", "errorDetails": [{"opaque": 1}]}]
        },
        httpErrorStatusCode=503,
        httpErrorMessage="synthetic-private-diagnostic",
    )
    assert decode_completion(json.dumps(value).encode(), SELECTED) is ActivationState.FAILED


@pytest.mark.parametrize("failed", [False, True])
def test_real_pipeline_ack_pending_terminal_get_and_explicit_settlement(sdk, monkeypatch, failed):
    p = sdk
    initial = p.adapter.state
    assert initial is ActivationState.UNKNOWN
    p.adapter.start(Deadline.after(5))
    assert p.adapter.state is ActivationState.PENDING  # POST DONE is only an ACK.
    encoded = encode_activation_payload(p.adapter.payload)
    row = receipt(p)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 1
    assert not p.adapter.settle(Deadline.after(5))
    credential_calls = []
    original = p.credential.before_request

    def before_request(request, method, url, headers):
        credential_calls.append((method, url))
        return original(request, method, url, headers)

    monkeypatch.setattr(p.credential, "before_request", before_request)
    fields = {"error": {"errors": [{"code": "FAILED", "message": "discarded"}]}} if failed else {}
    p.data = json.dumps(completion(p.adapter.payload, **fields)).encode()
    expected = ActivationState.FAILED if failed else ActivationState.SUCCEEDED
    assert p.adapter.observe(Deadline.after(5)) is expected
    assert credential_calls == [
        ("GET", "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/operations/Operation_1")
    ]
    assert [method for method, _ in p.service_calls] == ["POST", "GET"]
    assert all(
        session.get_adapter("https://compute.googleapis.com").max_retries.total == 0 for _, session in p.sessions
    )
    assert not p.auth.closed and not p.adapter.cleanup_incomplete
    assert encode_activation_payload(p.adapter.payload) == encoded
    assert receipt(p).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert p.adapter.observe(Deadline.after(5)) is expected
    assert len(p.service_calls) == 2
    assert p.adapter.settle(Deadline.after(5))
    assert receipt(p).state is LifecycleObligationState.RESOLVED
    assert receipt(p).payload_revision == 1 and receipt(p).payload == encoded
    p.owner.seal_lifecycle_obligations()
    p.owner.record_effects_resolved()
    p.owner.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("settled borrower touched provider or released owner")

    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", forbidden)
    monkeypatch.setattr(p.auth, "begin", forbidden)
    assert p.adapter.settle(Deadline.after(5))
    assert p.adapter.state is expected and len(p.service_calls) == 2


def test_one_get_per_explicit_observation_and_pending_settlement_does_not_poll(sdk):
    p = sdk
    p.adapter.start(Deadline.after(5))
    for status in ("PENDING", "RUNNING", "DONE"):
        p.data = json.dumps(completion(p.adapter.payload, status=status)).encode()
        assert p.adapter.observe(Deadline.after(5)) is (
            ActivationState.SUCCEEDED if status == "DONE" else ActivationState.PENDING
        )
        before = len(p.service_calls)
        assert p.adapter.settle(Deadline.after(5)) is (status == "DONE")
        assert len(p.service_calls) == before
    assert [method for method, _ in p.service_calls] == ["POST", "GET", "GET", "GET"]
    with pytest.raises(StateError):
        p.adapter.start(Deadline.after(5))


@pytest.mark.parametrize("http_status", [301, 302, 307, 308, 401, 403, 404, 429, 500, 503])
def test_get_http_refusal_is_pre_body_and_never_replayed(sdk, http_status):
    p = sdk
    p.adapter.start(Deadline.after(5))
    p.status = http_status
    p.headers = {"Location": "https://foreign.invalid/redirect", "Retry-After": "0"}
    p.data = json.dumps(completion(p.adapter.payload)).encode()
    with pytest.raises(ValidationError):
        p.adapter.observe(Deadline.after(5))
    assert p.responses[-1].raw.reads == []
    assert [method for method, _ in p.service_calls] == ["POST", "GET"]
    assert p.adapter.state is ActivationState.PENDING
    assert not p.adapter.settle(Deadline.after(5))
    assert receipt(p).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity,gzip", ""])
def test_get_compressed_body_refused_before_read(sdk, encoding):
    sdk.adapter.start(Deadline.after(5))
    sdk.headers = {"Content-Encoding": encoding}
    with pytest.raises(ValidationError):
        sdk.adapter.observe(Deadline.after(5))
    assert sdk.responses[-1].raw.reads == []
    assert sdk.adapter.state is ActivationState.PENDING


@pytest.mark.parametrize("invalid", [b"{", b"x" * 65537, b'{"kind":"compute#operation"}'])
def test_get_invalid_body_retains_pending_and_closes_originals(sdk, invalid):
    sdk.adapter.start(Deadline.after(5))
    sdk.data = invalid
    with pytest.raises(ValidationError):
        sdk.adapter.observe(Deadline.after(5))
    assert sdk.adapter.state is ActivationState.PENDING
    assert not sdk.adapter.cleanup_incomplete
    assert receipt(sdk).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_unknown_no_ack_stays_unresolved_after_local_close(sdk):
    with pytest.raises(StateError):
        sdk.adapter.observe(Deadline.after(5))
    assert not sdk.sessions
    sdk.reply = OSError("lost POST reply")
    with pytest.raises(OSError):
        sdk.adapter.start(Deadline.after(5))
    assert sdk.adapter.state is ActivationState.UNKNOWN
    assert sdk.adapter.close(Deadline.after(5))
    assert not sdk.adapter.settle(Deadline.after(5))
    assert receipt(sdk).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert decode_activation_payload(receipt(sdk).payload).operation_name is None
    for action in (sdk.adapter.start, sdk.adapter.observe):
        with pytest.raises(StateError):
            action(Deadline.after(5))
    assert len(sdk.service_calls) == 1


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_close_bars_admission_before_fallible_original_retirement(sdk, error_type):
    p = sdk
    primary = error_type("original close")

    def fail():
        assert p.adapter._closed
        raise primary

    original = SimpleNamespace(close=fail)
    p.adapter._response = original
    if error_type is OSError:
        assert not p.adapter.close(Deadline.after(5))
    else:
        with pytest.raises(error_type) as caught:
            p.adapter.close(Deadline.after(5))
        assert caught.value is primary
    assert p.adapter._response is original
    for action in (p.adapter.start, p.adapter.observe):
        with pytest.raises(StateError):
            action(Deadline.after(5))
    assert not p.service_calls and not p.auth.closed


@pytest.mark.parametrize("kind", ["response", "service"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_terminal_retained_before_close_failure_and_settle_retries_only_original(sdk, monkeypatch, kind, error_type):
    p = sdk
    p.adapter.start(Deadline.after(5))
    p.data = json.dumps(completion(p.adapter.payload)).encode()
    primary = error_type("uncertain original")

    def fail(label, original, normal):
        if label == kind:
            assert p.adapter.state is ActivationState.SUCCEEDED
            raise primary
        normal()

    p.before_close = fail
    if error_type is OSError:
        assert p.adapter.observe(Deadline.after(5)) is ActivationState.SUCCEEDED
    else:
        with pytest.raises(error_type) as caught:
            p.adapter.observe(Deadline.after(5))
        assert caught.value is primary
    assert p.adapter.state is ActivationState.SUCCEEDED and p.adapter.cleanup_incomplete
    if error_type is OSError:
        assert not p.adapter.settle(Deadline.after(5))
    assert receipt(p).state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        p.adapter.observe(Deadline.after(5))
    retained = p.adapter._response if kind == "response" else p.adapter._service_session
    assert retained is (p.responses[-1] if kind == "response" else p.service_session)
    p.before_close = None

    def forbidden(*args, **kwargs):
        raise AssertionError("local settlement acquired authentication")

    monkeypatch.setattr(p.auth, "begin", forbidden)
    assert p.adapter.settle(Deadline.after(5))
    assert not p.adapter.cleanup_incomplete and receipt(p).state is LifecycleObligationState.RESOLVED
    assert len(p.service_calls) == 2 and not p.auth.closed


def test_terminal_retained_before_late_framed_read_budget_refusal(sdk, monkeypatch):
    p = sdk
    p.adapter.start(Deadline.after(5))
    selected = json.dumps(completion(p.adapter.payload)).encode()
    response = HTTPResponse(
        InertSocket(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(selected)).encode() + b"\r\n\r\n" + selected)
    )
    response.begin()
    p.data = response
    deadline = Deadline.after(5)
    expired = []
    original = Deadline.remaining

    def read(data):
        assert data == selected and response.isclosed()
        expired.append(True)

    monkeypatch.setattr(Deadline, "remaining", lambda self: 0 if self is deadline and expired else original(self))
    p.read_callback = read
    with pytest.raises(LimitExceededError):
        p.adapter.observe(deadline)
    assert p.adapter.state is ActivationState.SUCCEEDED
    assert receipt(p).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert p.adapter.settle(Deadline.after(5))


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_resolve_reply_reconciles_exact_local_intent_without_get(sdk, monkeypatch, committed, error_type):
    p = sdk
    p.adapter.start(Deadline.after(5))
    p.data = json.dumps(completion(p.adapter.payload)).encode()
    p.adapter.observe(Deadline.after(5))
    primary = error_type("lost resolve reply")
    original = LifecycleObligation.resolve

    def fail(self):
        if committed:
            original(self)
        raise primary

    monkeypatch.setattr(LifecycleObligation, "resolve", fail)
    with pytest.raises(error_type) as caught:
        p.adapter.settle(Deadline.after(5))
    assert caught.value is primary and p.adapter.state is ActivationState.SUCCEEDED
    assert receipt(p).state is (
        LifecycleObligationState.RESOLVED if committed else LifecycleObligationState.POSSIBLE_EFFECT
    )
    monkeypatch.setattr(LifecycleObligation, "resolve", original)
    assert p.adapter.settle(Deadline.after(5))
    assert receipt(p).state is LifecycleObligationState.RESOLVED and len(p.service_calls) == 2


def test_terminal_state_does_not_authorize_foreign_premature_resolution(sdk, monkeypatch):
    sdk.adapter.start(Deadline.after(5))
    sdk.data = json.dumps(completion(sdk.adapter.payload)).encode()
    sdk.adapter.observe(Deadline.after(5))
    foreign = replace(receipt(sdk), state=LifecycleObligationState.RESOLVED)
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", lambda *args: foreign)
    with pytest.raises(StateError):
        sdk.adapter.settle(Deadline.after(5))
    assert sdk.adapter.state is ActivationState.SUCCEEDED and not sdk.adapter._settled


def test_settle_known_no_effect_registration_without_provider_work(sdk, monkeypatch):
    original = OperationOwner.register_lifecycle_obligation

    def lost_reply(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("lost registration reply")

    monkeypatch.setattr(OperationOwner, "register_lifecycle_obligation", lost_reply)
    with pytest.raises(OSError):
        sdk.adapter.start(Deadline.after(5))
    assert sdk.adapter.settle(Deadline.after(5))
    assert receipt(sdk).state is LifecycleObligationState.RESOLVED
    assert sdk.adapter.state is ActivationState.UNKNOWN and not sdk.sessions
