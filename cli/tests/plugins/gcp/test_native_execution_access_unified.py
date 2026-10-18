"""Real GCE reader/start/route and public Google-auth/Requests, with guest stub.

Only synthetic credentials, HTTP sends and source responses replace external
services. The guest preparation stub calls the real fresh locator callback.
This is offline composition evidence, not guest delivery, native compatibility,
provider recovery, physical socket drain or complete RunContext acceptance.
"""

from __future__ import annotations

import io
import json
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests  # type: ignore[import-untyped]
from google.auth.transport.requests import Request
from urllib3.response import HTTPResponse

from agentworks.capabilities.base import RunContext
from agentworks.config import Config, DefaultsConfig, OperatorConfig, PathsConfig, SessionConfig
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp import _native_execution_access
from agentworks.plugins.gcp._native_access import INSTANCE_FIELDS, NATIVE_INSTANCE_FIELDS
from agentworks.plugins.gcp._native_execution_access import GCEOwnedAccess
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.vms.target_preparation import VMTargetPreparation, VMTargetPreparationStatus
from tests.plugins.gcp.test_native_instance import native_instance
from tests.plugins.gcp.test_owned_auth_copy import run_copy
from tests.plugins.gcp.test_owned_read_sdk import FreshCredential

ROOT = "https://compute.googleapis.com/compute/v1/projects/project"
NETWORK = f"{ROOT}/global/networks/default"
INSTANCE = f"{ROOT}/zones/us-central1-a/instances/backend"


def baseline() -> dict[str, Any]:
    return {
        "kind": "compute#firewall",
        "name": "selected-deny",
        "id": "456",
        "selfLink": f"{ROOT}/global/firewalls/selected-deny",
        "network": NETWORK,
        "direction": "INGRESS",
        "priority": 1,
        "disabled": False,
        "sourceRanges": ["0.0.0.0/0"],
        "targetTags": ["selected-tag"],
        "denied": [{"IPProtocol": "all"}],
    }


@pytest.fixture
def unified(request, tmp_path, monkeypatch):
    initial, fault = request.param
    p = SimpleNamespace(
        initial=initial,
        power=initial,
        fault=fault,
        rule=None,
        sends=[],
        responses=[],
        sessions=[],
        closed_sessions=[],
        events=[],
        operations={},
        selections=[],
        guest_calls=[],
        credential=FreshCredential(),
        auth_session=None,
        failed_response=None,
        full_reads=0,
        adapters=set(),
        credential_responses=[],
        credential_close_failures=0,
        refreshes=0,
        copied_sessions=[],
        copy_close_failures=0,
        copy_close_attempts=0,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    session_close = requests.Session.close

    class Response(requests.Response):  # type: ignore[misc]
        closed_original = False
        failures = 0
        close_calls = 0

        @property
        def content(self):
            pytest.fail("service body consumed outside bounded raw reads")

        def close(self):
            self.close_calls += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("scripted returned response close failure")
            super().close()
            self.closed_original = True
            p.events.append("response-close")

    class Raw(HTTPResponse):
        def read(self, amt=None, decode_content=None, cache_content=False):
            assert type(amt) is int and 0 < amt <= 8192 and decode_content is False
            return super().read(amt, decode_content=decode_content, cache_content=cache_content)

    class CredentialResponse(requests.Response):  # type: ignore[misc]
        closed_original = False
        close_calls = 0

        def close(self):
            self.close_calls += 1
            if p.credential_close_failures:
                p.credential_close_failures -= 1
                raise RuntimeError("scripted credential response close failure")
            super().close()
            self.closed_original = True
            p.events.append("credential-response-close")

    class RefreshingCredential(FreshCredential):
        def refresh(self, request: Any) -> None:
            p.refreshes += 1
            response = request("https://credential.invalid/token", method="POST", body=b"synthetic")
            assert response.status == 200 and response.data == b"{}"
            self.token = "offline-selected-token"

    p.credential = RefreshingCredential()

    def operation(action: str, target: str, incarnation: str, request_id: str) -> dict[str, Any]:
        name = "start-operation" if action == "start" else f"{action}-operation"
        scope = "zones/us-central1-a" if action == "start" else "global"
        url = f"{ROOT}/{scope}/operations/{name}"
        value = {
            "kind": "compute#operation",
            "name": name,
            "clientOperationId": request_id,
            "targetId": incarnation,
            "targetLink": target,
            "operationType": action,
            "status": "PENDING",
        }
        p.operations[url] = value
        return value

    def send(adapter, prepared, **kwargs):
        p.adapters.add(adapter)
        if prepared.url in ("https://credential.invalid/token", "https://credential.invalid/copied"):
            response = CredentialResponse()
            response.status_code, response.url, response.request = 200, prepared.url, prepared
            response.raw = HTTPResponse(body=io.BytesIO(b"{}"), preload_content=False)
            p.credential_responses.append(response)
            return response
        assert kwargs["verify"] is True and kwargs["proxies"] == {} and kwargs["stream"] is True
        assert adapter.max_retries.total == 0
        assert prepared.headers["Accept-Encoding"] == "identity"
        assert prepared.headers["Authorization"] == "Bearer offline-selected-token"
        parts = urlsplit(prepared.url)
        assert parts.scheme == "https" and parts.netloc == "compute.googleapis.com"
        url = f"https://{parts.netloc}{parts.path}"
        query = parse_qs(parts.query, strict_parsing=True)
        method = prepared.method
        p.sends.append((method, url, query, prepared.body))
        response = Response()
        response.status_code = 200
        value: Any
        if url == INSTANCE and method == "GET":
            assert query in ({"fields": [INSTANCE_FIELDS]}, {"fields": [NATIVE_INSTANCE_FIELDS]})
            p.full_reads += int(query == {"fields": [NATIVE_INSTANCE_FIELDS]})
            value = native_instance()
            value["status"] = p.power
        elif url == INSTANCE + "/start" and method == "POST":
            assert set(query) == {"requestId"} and prepared.body is None
            assert p.access._start is not None
            value = operation("start", INSTANCE, "123", query["requestId"][0])
        elif url in p.operations and method == "GET":
            value = {**p.operations[url], "status": "DONE"}
            if value["operationType"] == "start":
                p.power = "RUNNING"
        elif url == NETWORK and method == "GET":
            # Omitted enforcement order uses the documented classic-first default.
            value = {"kind": "compute#network", "name": "default", "id": "789", "selfLink": NETWORK}
        elif url == baseline()["selfLink"] and method == "GET":
            value = baseline()
        elif url == f"{ROOT}/global/firewalls" and method == "GET":
            value = {"kind": "compute#firewallList", "items": [baseline(), *([p.rule] if p.rule else [])]}
        elif url == f"{ROOT}/global/firewalls" and method == "POST":
            assert p.rule is None and set(query) == {"requestId"}
            body = json.loads(prepared.body)
            assert body["network"].endswith("projects/project/global/networks/default")
            assert body["priority"] == 0 and body["direction"] == "INGRESS"
            assert body["targetTags"] == ["selected-tag"]
            assert body["sourceRanges"] == ["192.0.2.1/32"]
            assert body["allowed"] == [{"IPProtocol": "tcp", "ports": ["22"]}]
            target = f"{ROOT}/global/firewalls/{body['name']}"
            p.rule = {**body, "kind": "compute#firewall", "id": "901", "selfLink": target}
            value = operation("insert", target, "901", query["requestId"][0])
            if fault:
                response.failures = 2
                p.failed_response = response
        elif url.startswith(f"{ROOT}/global/firewalls/") and method == "GET":
            if p.rule is None:
                value, response.status_code = {}, 404
            else:
                assert url == p.rule["selfLink"]
                value = p.rule
        elif url.startswith(f"{ROOT}/global/firewalls/") and method == "DELETE":
            assert p.rule is not None and url == p.rule["selfLink"] and set(query) == {"requestId"}
            value = operation("delete", url, "901", query["requestId"][0])
            p.rule = None
        else:
            pytest.fail(f"unexpected scripted request: {method} {url}")
        response.url, response.request = prepared.url, prepared
        response.raw = Raw(body=io.BytesIO(json.dumps(value).encode()), preload_content=False)
        p.responses.append(response)
        return response

    def default(**kwargs):
        assert isinstance(kwargs["request"], Request)
        p.selections.append(kwargs)
        p.auth_session = kwargs["request"].session
        return p.credential, "unrelated-project"

    def close_session(session):
        if session is not p.auth_session and not any(adapter in p.adapters for adapter in session.adapters.values()):
            return session_close(session)
        if session is p.auth_session:
            assert p.source.closed == 1
            assert all(response.closed_original for response in p.responses)
            assert all(response.closed_original for response in p.credential_responses)
            assert not p.owner.list_pending_lifecycle_obligations()
            assert p.database.operations.inspect(p.owner.ownership.scope) is not None
            assert p.access._reader._response is None and p.access._reader._service_session is None
            assert sum(session not in p.copied_sessions for session in p.closed_sessions) == len(p.sends)
            assert all(session in p.closed_sessions for session in p.copied_sessions)
            assert p.access._route is None or not p.access._route.cleanup_incomplete
            assert p.access._start is None or not p.access._start.cleanup_incomplete
            p.events.append("auth-close")
        elif session in p.copied_sessions:
            p.copy_close_attempts += 1
            if p.copy_close_failures:
                p.copy_close_failures -= 1
                raise RuntimeError("scripted copied session close failure")
            p.events.append("copy-close")
        else:
            p.events.append("service-close")
        session_close(session)
        p.closed_sessions.append(session)

    class Source:
        closed = 0

        def read(self, limit):
            assert limit == 65 and p.access._exposure.cleanup_incomplete
            return b"192.0.2.1\n"

        def close(self):
            self.closed += 1
            p.events.append("source-close")

    p.source = Source()
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", send)
    monkeypatch.setattr("google.auth.default", default)
    # Observe public closure while preserving the actual Requests Session class.
    monkeypatch.setattr(requests.Session, "close", close_session)
    monkeypatch.setattr("agentworks.vms._ssh_native_exposure.urlopen", lambda *a, **k: p.source)
    config = Config(
        OperatorConfig(
            tmp_path / "public", tmp_path / "private", ssh=SSHSettings(tmp_path / "trust", tmp_path / "key")
        ),
        PathsConfig(),
        DefaultsConfig(),
        tmp_path / "absent.toml",
        SessionConfig(),
    )
    p.ctx = RunContext(config=config)
    p.platform = GCEPlatform("offline", {"project_id": "project", "zone": "us-central1-a"})
    with closing(Database(tmp_path / "unified.db")) as database:
        p.database = database
        database.insert_vm("vm", "offline", "vm", admin_username="root", instance_marker="0" * 32)
        # Default-network VM rows have no explicitly selected subnet; live GCE
        # still returns an auto-mode subnet, as native_instance() does here.
        metadata = {
            "project_id": "project",
            "zone": "us-central1-a",
            "instance_name": "backend",
            "instance_id": "123",
            "network_url": NETWORK,
            "network_tag": "selected-tag",
            "deny_rule": "selected-deny",
            "deny_rule_id": "456",
            "access_config_name": "External NAT",
        }
        database.update_vm_platform_metadata("vm", metadata)
        p.vm = database.get_vm("vm")
        p.owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "unified")
        p.access = p.platform.build_native_execution_access(p.vm, p.ctx, owner=p.owner, custody=LocalDeliveryCustody())
        assert isinstance(p.access, GCEOwnedAccess)
        assert p.sends == [] and p.selections == [] and not p.access.cleanup_incomplete

        def guest_stub(vm, platform, locator, binding, *, deadline, owner, observe_locator):
            assert vm is p.vm and platform is p.platform and owner is p.owner and binding is p.access.binding
            assert p.access.route_check is None and observe_locator.__self__ is p.access
            assert observe_locator(deadline) == locator == p.access._identity.locator
            p.guest_calls.append(locator)
            target = ManagedTargetIdentity(
                ManagedTargetKind.VM, vm.name, "v1:" + "0" * 64, "00000000-0000-0000-0000-000000000000"
            )
            return VMTargetPreparation(VMTargetPreparationStatus.PREPARED, target, None)

        monkeypatch.setattr(_native_execution_access, "prepare_managed_vm_target_from_platform", guest_stub)
        p.deadline = Deadline.after(10)
        yield p
        assert database.operations.inspect(p.owner.ownership.scope) is None


def finish(p: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert p.events[-1] == "auth-close" and p.closed_sessions.count(p.auth_session) == 1
    assert len(p.selections) == 1 and p.source.closed == 1
    assert all(response.closed_original for response in p.responses)
    assert not p.access.cleanup_incomplete and p.rule is None
    assert not p.owner.list_pending_lifecycle_obligations()
    methods = [(method, url) for method, url, _, _ in p.sends]
    assert methods.count(("POST", INSTANCE + "/start")) == int(p.initial != "RUNNING")
    assert sum(method == "POST" and url.endswith("/global/firewalls") for method, url in methods) == 1
    assert sum(method == "DELETE" for method, _ in methods) == 1
    assert sum("/operations/insert-operation" in url for _, url in methods) == 1
    assert sum("/operations/delete-operation" in url for _, url in methods) == 1
    assert sum("/operations/start-operation" in url for _, url in methods) == int(p.initial != "RUNNING")
    p.owner.seal_lifecycle_obligations()
    p.owner.record_effects_resolved()
    p.owner.close()
    assert p.database.operations.inspect(p.owner.ownership.scope) is None
    before = len(p.sends), len(p.events), len(p.guest_calls)

    def no_ledger(*a, **k):
        pytest.fail("settled composition revisited released ledger")

    for method in (
        "inspect_lifecycle_obligation",
        "list_pending_lifecycle_obligations",
        "register_lifecycle_obligation",
        "mark_lifecycle_obligation_possible_effect",
        "publish_lifecycle_obligation_payload",
        "resolve_lifecycle_obligation",
    ):
        monkeypatch.setattr(p.database.operations, method, no_ledger)
    assert p.access.settle(Deadline.after(5))
    assert before == (len(p.sends), len(p.events), len(p.guest_calls))


@pytest.mark.parametrize("unified", [("RUNNING", False), ("TERMINATED", False)], indirect=True)
def test_actual_composition_one_shot_route_and_start_with_guest_stub(unified, monkeypatch):
    p = unified
    power = p.access.observe_power(p.deadline)
    assert power is (VMStatus.RUNNING if p.initial == "RUNNING" else VMStatus.STOPPED)
    p.access.prepare(power, p.deadline)
    assert len(p.guest_calls) == 1 and p.full_reads >= 4
    assert p.access.route_check is not None
    p.access.route_check(p.deadline)
    assert p.access.settle(Deadline.after(5))
    finish(p, monkeypatch)


@pytest.mark.parametrize("unified", [("RUNNING", True), ("TERMINATED", True)], indirect=True)
def test_actual_returned_route_response_close_failure_then_fresh_cleanup(unified, monkeypatch):
    p = unified
    power = p.access.observe_power(p.deadline)
    with pytest.raises(StateError):
        p.access.prepare(power, p.deadline)
    original = p.failed_response
    assert original is not None and original.close_calls >= 1 and not original.closed_original
    assert not p.guest_calls and p.access.cleanup_incomplete and p.access.route_check is None
    assert "auth-close" not in p.events and p.owner.list_pending_lifecycle_obligations()
    original.failures = 0
    assert p.access.settle(Deadline.after(5))
    assert original.closed_original and not p.guest_calls
    finish(p, monkeypatch)


@pytest.mark.parametrize("unified", [("RUNNING", False), ("TERMINATED", False)], indirect=True)
def test_open_route_then_refresh_response_close_failure_recovers_without_reselection(unified, monkeypatch):
    p = unified
    p.access.prepare(VMStatus.RUNNING if p.initial == "RUNNING" else VMStatus.STOPPED, p.deadline)
    assert p.rule is not None and len(p.guest_calls) == 1
    selected = p.access._auth._credential
    root_session = p.auth_session
    p.credential.token = None
    p.credential_close_failures = 1
    check = p.access.route_check
    with pytest.raises(StateError):
        check(p.deadline)
    assert p.access._auth.cleanup_incomplete and not p.access._auth.closed
    original = p.credential_responses[0]
    assert original.close_calls == 1 and not original.closed_original
    assert p.rule is not None and "auth-close" not in p.events
    assert p.access.settle(Deadline.after(5))
    assert original.close_calls == 2 and original.closed_original
    assert p.refreshes == 1 and p.access._auth._credential is selected and p.auth_session is root_session
    assert len(p.guest_calls) == 1
    with pytest.raises(StateError):
        check(Deadline.after(5))
    finish(p, monkeypatch)


@pytest.mark.parametrize("unified", [("RUNNING", False)], indirect=True)
@pytest.mark.parametrize("debt", ["response", "session"])
def test_open_route_then_completed_copy_close_failure_recovers_originals(unified, monkeypatch, debt):
    p = unified
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    auth = p.access._auth
    selected = auth._credential
    auth.begin(Deadline.after(5))
    try:
        clone = deepcopy(auth._request)
    finally:
        auth.end()
    p.copied_sessions.append(clone.session)
    worker, outcomes = run_copy(clone)
    worker.join(2)
    assert not worker.is_alive() and outcomes == [b"{}"]
    response = clone._responses[0]
    if debt == "response":
        p.credential_close_failures = 1
    else:
        p.copy_close_failures = 1
    with pytest.raises(StateError):
        p.access.route_check(p.deadline)
    assert auth.cleanup_incomplete and not auth.closed and clone in auth._copies
    assert p.rule is not None and "auth-close" not in p.events
    assert p.access.settle(Deadline.after(5))
    assert not auth._copies and response.closed_original and clone.session is None
    assert auth._credential is selected and len(p.guest_calls) == 1
    assert p.copy_close_attempts == (2 if debt == "session" else 1)
    finish(p, monkeypatch)


@pytest.mark.parametrize("unified", [("RUNNING", False)], indirect=True)
def test_route_cleanup_refresh_creates_new_debt_after_positive_auth_retry(unified, monkeypatch):
    p = unified
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    p.credential.token = None
    p.credential_close_failures = 1
    with pytest.raises(StateError):
        p.access.settle(Deadline.after(5))
    auth = p.access._auth
    assert auth.cleanup_incomplete and not auth.closed and p.rule is not None
    assert p.access.route_check is None and "auth-close" not in p.events
    original = p.credential_responses[0]
    assert original.close_calls == 1 and not original.closed_original
    assert p.access.settle(Deadline.after(5))
    assert original.close_calls == 2 and p.refreshes == 1 and len(p.guest_calls) == 1
    finish(p, monkeypatch)
