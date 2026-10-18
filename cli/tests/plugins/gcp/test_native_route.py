"""Real Google-auth/Requests route dispatch with fresh fenced SQLite custody."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import google.auth
import pytest
import requests  # type: ignore[import-untyped]
from google.auth.credentials import Credentials
from urllib3.response import HTTPResponse

from agentworks.db import Database, VMStatus
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation_operation import ActivationState
from agentworks.plugins.gcp._native_access import GCEInstanceObservation
from agentworks.plugins.gcp._native_identity import NativeIdentity
from agentworks.plugins.gcp._native_route import GCENativeRoute
from agentworks.plugins.gcp._native_route_evidence import decode_payload, encode_payload
from agentworks.plugins.gcp._native_route_topology import rule_body
from agentworks.plugins.gcp._owned_auth import _OwnedAuth
from agentworks.plugins.gcp.config import GcpAmbientAuth

IDENTITY = NativeIdentity(
    "project",
    "us-central1-a",
    "backend",
    "123",
    "projects/project/global/networks/net",
    None,
    "selected-tag",
    "deny-rule",
    "456",
    "External NAT",
)
OBSERVATION = GCEInstanceObservation(
    IDENTITY.locator,
    "RUNNING",
    VMStatus.RUNNING,
    "192.0.2.7",
    ("vm@example.test",),
    ("selected-tag", "additional-tag"),
)
RETAINED: list[Any] = []


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


class Response(requests.Response):  # type: ignore[misc]
    @property
    def content(self):
        raise AssertionError("service body escaped bounded raw reader")


class Raw(HTTPResponse):
    def __init__(self, body: bytes, probe: Any) -> None:
        super().__init__(body=io.BytesIO(body), preload_content=False)
        self.probe = probe
        self.reads: list[int] = []

    def read(self, amt=None, decode_content=None, cache_content=False):
        assert type(amt) is int and 0 < amt <= 8192 and decode_content is False
        self.reads.append(amt)
        result = super().read(amt, decode_content=decode_content, cache_content=cache_content)
        if self.probe.on_read:
            self.probe.on_read(result)
        return result


def receipt(route: Any, deleting=False, status="DONE", failed=False) -> dict[str, Any]:
    payload = route.payload
    result = {
        "kind": "compute#operation",
        "name": "Delete_1" if deleting else "Insert_1",
        "clientOperationId": payload.delete_request_id if deleting else payload.create_request_id,
        "targetId": payload.rule_id if deleting else "789",
        "targetLink": f"https://compute.googleapis.com/compute/v1/projects/project/global/firewalls/{payload.name}",
        "operationType": "delete" if deleting else "insert",
        "status": status,
    }
    if failed:
        result["error"] = {"errors": [{"code": "SYNTHETIC_FAILURE", "message": "discarded diagnostic"}]}
    return result


def owned_rule(route: Any) -> dict[str, Any]:
    return {"kind": "compute#firewall", "id": "789", **rule_body(route.payload)}


def baseline() -> dict[str, Any]:
    return {
        "kind": "compute#firewall",
        "name": IDENTITY.deny_rule,
        "id": IDENTITY.deny_rule_id,
        "network": IDENTITY.network_url,
        "direction": "INGRESS",
        "disabled": False,
        "priority": 1,
        "targetTags": [IDENTITY.network_tag],
        "sourceRanges": ["0.0.0.0/0"],
        "denied": [{"IPProtocol": "all"}],
    }


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    p = SimpleNamespace(
        calls=[],
        responses=[],
        sessions=[],
        closes=[],
        before_close=None,
        override=None,
        on_read=None,
        exists=False,
        create_status="DONE",
        delete_status="DONE",
        delete_failed=False,
        extra_rules=[],
        pages=None,
        network_order=None,
        baseline=baseline(),
        credential_calls=[],
    )
    original_session = requests.Session
    close_session, close_response = original_session.close, requests.Response.close

    def session():
        original = original_session()
        p.sessions.append(original)
        return original

    def close(self):
        p.closes.append(self)
        if p.before_close:
            p.before_close(self)
        close_session(self)

    def close_reply(self):
        p.closes.append(self)
        if p.before_close:
            p.before_close(self)
        close_response(self)

    def send(self, request, **kwargs):
        assert kwargs["verify"] is True and kwargs["proxies"] == {}
        assert self.max_retries.total == 0
        timeout = kwargs["timeout"]
        assert all(0 < t <= 5 for t in (timeout if isinstance(timeout, tuple) else (timeout,)))
        assert all(s.trust_env is False for s in p.sessions)
        if request.url == "https://credential.invalid/token":
            p.credential_calls.append(request)
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.raw = HTTPResponse(body=io.BytesIO(b"{}"), preload_content=False)
            return response
        assert kwargs["stream"] is True and request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Authorization"] == "Bearer " + p.credential.token
        path, query = urlsplit(request.url).path, parse_qs(urlsplit(request.url).query)
        p.calls.append((request.method, path, query, request.body))
        status, headers = 200, {}
        value: Any
        if request.method in ("POST", "DELETE"):
            row = p.owner.inspect_lifecycle_obligation(p.route.obligation_id)
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            durable = decode_payload(row.payload)
            expected = durable.create_request_id if request.method == "POST" else durable.delete_request_id
            assert query == {"requestId": [expected]}
            assert (
                request.body is None
                if request.method == "DELETE"
                else json.loads(request.body) == rule_body(p.route.payload)
            )
            if request.method == "POST":
                p.exists = True
            elif not p.delete_failed and p.delete_status == "DONE":
                p.exists = False
            value = receipt(
                p.route,
                request.method == "DELETE",
                p.delete_status if request.method == "DELETE" else p.create_status,
                p.delete_failed and request.method == "DELETE",
            )
        elif path.endswith("/networks/net"):
            value = {"kind": "compute#network", "name": "net", "selfLink": IDENTITY.network_url}
            if p.network_order is not None:
                value["networkFirewallPolicyEnforcementOrder"] = p.network_order
        elif path.endswith("/firewalls/deny-rule"):
            value = p.baseline
        elif path.endswith("/firewalls"):
            assert query["maxResults"] == ["100"] and "fields" in query
            value = (
                p.pages(query)
                if p.pages
                else {"items": [p.baseline, *p.extra_rules, *([owned_rule(p.route)] if p.exists else [])]}
            )
        elif "/operations/" in path:
            deleting = path.endswith("/Delete_1")
            value = receipt(p.route, deleting)
            if deleting:
                p.exists = False
        else:
            assert path.endswith("/firewalls/" + p.route.payload.name)
            value = owned_rule(p.route) if p.exists else {}
            status = 200 if p.exists else 404
        if p.override:
            replacement = p.override(request, value)
            if replacement is not None:
                status, headers, value = replacement
        response = Response()
        response.request, response.url, response.status_code = request, request.url, status
        response.headers.update(headers)
        response.raw = Raw(value if type(value) is bytes else json.dumps(value).encode(), p)
        p.responses.append(response)
        return response

    monkeypatch.setattr(requests, "Session", session)
    monkeypatch.setattr(original_session, "close", close)
    monkeypatch.setattr(Response, "close", close_reply)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    p.credential = Credential()
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (p.credential, "ignored"))
    p.auth = _OwnedAuth("db-vm", GcpAmbientAuth(mode="ambient"), SimpleNamespace(), "site")
    with closing(Database(tmp_path / "route.db")) as database:
        p.database = database
        p.owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "db-vm"), "test")
        p.route = GCENativeRoute(p.owner, "db-vm", p.auth, IDENTITY, ("198.51.100.0/24",))
        yield p
        assert not p.route._lock.locked()
        if p.route.cleanup_incomplete or p.auth.cleanup_incomplete:
            RETAINED.append(p)
        else:
            p.before_close = None
            p.auth.close(Deadline.after(5))


def open_route(p: Any) -> None:
    p.route.open(Deadline.after(5), observed_instance=OBSERVATION)


def mutations(p: Any, method: str) -> list[Any]:
    return [call for call in p.calls if call[0] == method]


def test_open_check_close_settle_auth_last_and_memoize(sdk):
    p = sdk
    assert not p.calls and not p.sessions
    open_route(p)
    assert p.route.payload.create_state is ActivationState.SUCCEEDED
    assert len(mutations(p, "POST")) == 1 and not p.route.cleanup_incomplete
    p.route.check(Deadline.after(5), observed_instance=OBSERVATION)
    assert p.route.close(Deadline.after(5))
    before = len(p.calls)
    with pytest.raises(StateError):
        p.route.check(Deadline.after(5), observed_instance=OBSERVATION)
    assert len(p.calls) == before
    # Cleanup is independent of baseline health and the VM's power or presence.
    p.baseline["priority"] = 9
    p.network_order = "BEFORE_CLASSIC_FIREWALL"
    assert p.route.settle(Deadline.after(5))
    assert len(mutations(p, "DELETE")) == 1
    assert not p.auth.closed and p.sessions[0] not in p.closes
    assert p.owner.inspect_lifecycle_obligation(p.route.obligation_id).state is LifecycleObligationState.RESOLVED
    before = len(p.calls)
    p.owner.seal_lifecycle_obligations()
    p.owner.record_effects_resolved()
    p.owner.close()
    assert p.route.settle(Deadline.after(5)) and len(p.calls) == before


def test_pending_creation_and_deletion_observe_exact_global_operations(sdk):
    p = sdk
    p.create_status = p.delete_status = "RUNNING"
    open_route(p)
    assert p.route.settle(Deadline.after(5))
    assert [c[1].rsplit("/", 1)[1] for c in p.calls if "/operations/" in c[1]] == ["Insert_1", "Delete_1"]
    assert len(mutations(p, "POST")) == len(mutations(p, "DELETE")) == 1


@pytest.mark.parametrize("refresh", [False, True])
def test_shared_public_before_request_and_optional_refresh(sdk, refresh):
    if refresh:
        sdk.credential.token = None
    open_route(sdk)
    assert sdk.credential.refreshes == len(sdk.credential_calls) == int(refresh)
    assert sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize("order", ["BEFORE_CLASSIC_FIREWALL", "unknown", False])
def test_network_policy_refuses_before_mutation(sdk, order):
    sdk.network_order = order
    with pytest.raises(StateError):
        open_route(sdk)
    assert not mutations(sdk, "POST") and sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize("field,value", [("id", "999"), ("disabled", True), ("priority", 2), ("targetTags", ["other"])])
def test_baseline_incarnation_and_full_behavior(sdk, field, value):
    sdk.baseline[field] = value
    with pytest.raises(StateError):
        open_route(sdk)
    assert not mutations(sdk, "POST")


@pytest.mark.parametrize(
    "target", [{"targetTags": ["additional-tag"]}, {"targetServiceAccounts": ["vm@example.test"]}, {}]
)
@pytest.mark.parametrize("deny", [False, True])
def test_conflicts_apply_to_all_live_tags_and_service_accounts(sdk, target, deny):
    rule = baseline()
    rule.update(name="operator-policy", id="111", priority=0, **target)
    if "targetServiceAccounts" in target or not target:
        rule.pop("targetTags", None)
    if not deny:
        rule.pop("denied")
        rule["allowed"] = [{"IPProtocol": "udp", "ports": ["53"]}]
    sdk.extra_rules = [rule]
    with pytest.raises(StateError):
        open_route(sdk)
    assert not mutations(sdk, "POST")


def test_nonapplicable_rule_and_nonoverlapping_ssh_deny_are_permitted(sdk):
    other = baseline()
    other.update(name="other-rule", id="111", targetTags=["unattached"], priority=0)
    outside = baseline()
    outside.update(name="outside-sources", id="112", sourceRanges=["203.0.113.0/24"])
    sdk.extra_rules = [other, outside]
    open_route(sdk)
    assert sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "mutation",
    [
        {"raw_power": "TERMINATED", "power": VMStatus.STOPPED},
        {"tags": ("additional-tag",)},
    ],
)
def test_observed_running_selected_instance_required_before_network(sdk, mutation):
    with pytest.raises(StateError):
        sdk.route.open(Deadline.after(5), observed_instance=replace(OBSERVATION, **mutation))
    assert not sdk.calls


def test_inventory_is_complete_and_bounded(sdk):
    seen = []

    def pages(query):
        token = query.get("pageToken", [None])[0]
        seen.append(token)
        return {"items": [baseline()], "nextPageToken": "next"} if token is None else {"items": []}

    sdk.pages = pages
    open_route(sdk)
    assert seen == [None, "next"]
    assert sdk.route.settle(Deadline.after(5))


@pytest.mark.parametrize(
    "page",
    [
        {"warning": {"code": "PARTIAL_RESULTS"}},
        {"nextPageToken": "repeat"},
        {"nextPageToken": "x" * 4097},
        {"items": [{}]},
        {"items": [baseline()] * 101},
    ],
)
def test_incomplete_malformed_or_looping_inventory_refuses(sdk, page):
    sdk.pages = lambda query: page
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert not mutations(sdk, "POST")


@pytest.mark.parametrize("stage", ["register", "mark", "publish"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("control_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_ledger_reply_is_repaired_without_insert_replay(sdk, monkeypatch, stage, committed, control_type):
    p = sdk
    target, name = {
        "register": (OperationOwner, "register_lifecycle_obligation"),
        "mark": (LifecycleObligation, "mark_possible_effect"),
        "publish": (LifecycleObligation, "publish_payload"),
    }[stage]
    original = getattr(target, name)
    primary = control_type("lost reply")

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(control_type) as caught:
        open_route(p)
    assert caught.value is primary
    monkeypatch.setattr(target, name, original)
    assert len(mutations(p, "POST")) == int(stage == "publish")
    assert p.route.settle(Deadline.after(5))
    assert len(mutations(p, "POST")) == int(stage == "publish")
    assert p.route._publication is None


def test_missing_insert_ack_never_adopts_rule_or_replays(sdk):
    def override(request, value):
        if request.method == "POST":
            raise OSError("reply lost after possible dispatch")

    sdk.override = override
    with pytest.raises(OSError):
        open_route(sdk)
    sdk.override = None
    assert not sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "POST")) == 1 and not mutations(sdk, "DELETE")


def test_matching_ack_retained_when_status_malformed_then_observed(sdk):
    def override(request, value):
        if request.method == "POST":
            value["status"] = "alien"

    sdk.override = override
    with pytest.raises(ValidationError):
        open_route(sdk)
    assert sdk.route.payload.create_operation == "Insert_1" and sdk.route.payload.rule_id == "789"
    sdk.override = None
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "POST")) == 1


def test_late_complete_receipt_survives_before_deadline_refusal(sdk, monkeypatch):
    deadline = Deadline.after(5)
    original = GCENativeRoute._remaining
    late = False

    def override(request, value):
        if request.method == "POST":
            sdk.on_read = lambda data: expire() if not data else None

    def expire() -> None:
        nonlocal late
        late = True

    def remaining(current):
        if late and current is deadline:
            raise LimitExceededError("late")
        return original(current)

    sdk.override = override
    monkeypatch.setattr(GCENativeRoute, "_remaining", staticmethod(remaining))
    with pytest.raises(LimitExceededError):
        sdk.route.open(deadline, observed_instance=OBSERVATION)
    assert sdk.route.payload.create_state is ActivationState.SUCCEEDED
    assert sdk.route.payload.rule_id == "789"
    sdk.on_read = sdk.override = None
    assert sdk.route.settle(Deadline.after(5))


def test_known_terminal_delete_failure_retries_only_later_with_new_uuid(sdk):
    open_route(sdk)
    sdk.delete_failed = True
    assert not sdk.route.settle(Deadline.after(5))
    first = sdk.route.payload.delete_request_id
    assert len(mutations(sdk, "DELETE")) == 1
    sdk.delete_failed = False
    assert sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "DELETE")) == 2 and sdk.route.payload.delete_request_id != first


def test_delete_missing_ack_never_replays_or_resolves_from_absence(sdk):
    open_route(sdk)

    def override(request, value):
        if request.method == "DELETE":
            raise OSError("delete reply lost")

    sdk.override = override
    with pytest.raises(OSError):
        sdk.route.settle(Deadline.after(5))
    assert not sdk.exists
    sdk.override = None
    assert not sdk.route.settle(Deadline.after(5))
    assert len(mutations(sdk, "DELETE")) == 1


@pytest.mark.parametrize("field,value", [("id", "999"), ("description", "foreign"), ("priority", 3)])
def test_cleanup_refuses_replaced_or_modified_rule(sdk, field, value):
    open_route(sdk)

    def override(request, body):
        if request.method == "GET" and urlsplit(request.url).path.endswith(sdk.route.payload.name):
            body[field] = value

    sdk.override = override
    with pytest.raises(StateError):
        sdk.route.settle(Deadline.after(5))
    assert not mutations(sdk, "DELETE")


def test_payload_canonical_roundtrip_rejects_corruption_and_keeps_no_diagnostic(sdk):
    open_route(sdk)
    data = encode_payload(sdk.route.payload)
    assert decode_payload(data) == sdk.route.payload
    assert b"synthetic" not in data and b"token" not in data
    for corrupt in (data + b" ", b"{}", b'{"a":1,"a":2}', data.replace(b'"789"', b'"0789"')):
        with pytest.raises(ValidationError):
            decode_payload(corrupt)
