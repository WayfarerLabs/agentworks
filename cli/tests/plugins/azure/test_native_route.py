"""Actual locked network SDK dispatch and real SQLite rule custody, offline."""

from __future__ import annotations

import copy
import json
import socket
import time
from contextlib import closing
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.transport import HttpTransport

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _route_client
from agentworks.plugins.azure._native_route import AzureNativeRoute
from agentworks.plugins.azure._native_route_evidence import resource_url
from tests.plugins.azure.test_activation_sdk import Credential, Response, deny

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESOURCE = f"/subscriptions/{SUB}/resourceGroups/gr:oup/providers/Microsoft.Compute/virtualMachines/vm"
LINK = f"/subscriptions/{SUB}/resourceGroups/linked-group/providers/Microsoft.Network"
NIC = f"{LINK}/networkInterfaces/actual-nic"
NSG = f"{LINK}/networkSecurityGroups/actual-nsg"
VM_ID = "01234567-89ab-0cde-0123-456789abcdef"
NIC_ID = "11234567-89ab-0cde-0123-456789abcdef"
NSG_ID = "21234567-89ab-0cde-0123-456789abcdef"
OP = f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.Network/operations/create?api-version=2025-09-01"
DELETE_OP = OP.replace("create?", "delete?")
LOCATION = OP.replace("create?", "location?")
PREFIXES = ("192.0.2.1/32", "198.51.100.0/24")
LOCATOR = ProviderLocator(f"azure-vm:v2:{VM_ID}:{RESOURCE}")


class Control(BaseException):
    pass


class RuleResponse(Response):
    def __init__(self, request, status, headers, body) -> None:
        super().__init__(request, status, headers)
        self.body = body
        self.close_errors: list[BaseException] = []
        self.close_calls = 0
        self.iterations = 0

    def iter_bytes(self, **kwargs):
        self.iterations += 1
        if isinstance(self.body, BaseException):
            raise self.body
        if callable(self.body):
            yield from self.body()
        elif isinstance(self.body, list):
            yield from self.body
        else:
            yield json.dumps(self.body).encode()

    def close(self):
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        super().close()


class RouteTransport(HttpTransport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses: list[RuleResponse] = []
        self.close_calls = 0
        self.close_errors: list[BaseException] = []
        self.overrides: list[tuple[str, Any]] = []
        self.hook = None
        self.rule: Any = None
        self.nic = {
            "id": NIC,
            "tags": {"owner": "agentworks"},
            "properties": {
                "resourceGuid": NIC_ID,
                "provisioningState": "Succeeded",
                "virtualMachine": {"id": RESOURCE},
                "networkSecurityGroup": {"id": NSG},
            },
        }
        self.nsg = {
            "id": NSG,
            "tags": {"owner": "agentworks"},
            "properties": {
                "resourceGuid": NSG_ID,
                "provisioningState": "Succeeded",
                "networkInterfaces": [{"id": NIC}],
                "subnets": [],
                "securityRules": [
                    {
                        "name": "deny-all-inbound",
                        "properties": {
                            "protocol": "*",
                            "sourcePortRange": "*",
                            "destinationPortRange": "*",
                            "sourceAddressPrefix": "*",
                            "destinationAddressPrefix": "*",
                            "access": "Deny",
                            "priority": 200,
                            "direction": "Inbound",
                        },
                    },
                ],
            },
        }

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
        assert kwargs["stream"] is True
        assert 0 < kwargs["connection_timeout"] <= 5
        assert kwargs["read_timeout"] == kwargs["connection_timeout"]
        assert request.headers["Authorization"] == "Bearer synthetic-offline-token"
        content = None if request.content is None else json.loads(request.content)
        self.calls.append((request.method, request.url, content))
        if request.method == "PUT":
            assert content is not None
            self.rule = dict(content, id=unquote(urlsplit(request.url).path))
            self.rule["name"] = self.rule["id"].rsplit("/", 1)[1]
            self.rule["properties"]["provisioningState"] = "Succeeded"
        reply = None
        if self.overrides and self.overrides[0][0] in {request.method, request.url}:
            _, reply = self.overrides.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if reply is None:
            if request.method == "PUT":
                reply = (201, {}, self.rule)
            elif request.method == "DELETE":
                self.rule = None
                reply = (204, {}, deny)
            elif request.url.casefold() == resource_url(self.nic["id"]).casefold():
                reply = (200, {}, self.nic)
            elif request.url.casefold() == resource_url(self.nsg["id"]).casefold():
                reply = (200, {}, self.nsg)
            elif request.url in {OP, DELETE_OP}:
                reply = (200, {}, {"status": "Succeeded", "error": "secret provider body"})
                if request.url == DELETE_OP:
                    self.rule = None
            elif request.url == LOCATION:
                reply = (200, {}, deny)
            else:
                assert "/securityRules/allow-ssh-native-" in request.url
                reply = (404, {}, deny) if self.rule is None else (200, {}, self.rule)
        status, headers, body = reply
        response = RuleResponse(request, status, headers, copy.deepcopy(body) if isinstance(body, dict) else body)
        self.responses.append(response)
        if self.hook is not None:
            self.hook(response)
        return response


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.setattr("platform.platform", lambda: "offline-scripted-host")
    for target, method in [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ]:
        monkeypatch.setattr(target, method, deny)
    transport = RouteTransport()

    class BorrowedCredential(Credential):
        close = deny

    credential = BorrowedCredential()
    original = _route_client.network_route_client
    clients = []

    def construct(selected, subscription):
        assert selected is credential and subscription == SUB
        client = original(selected, subscription, transport=transport)
        clients.append(client)
        return client

    monkeypatch.setattr(_route_client, "network_route_client", construct)
    vm = SimpleNamespace(
        id=RESOURCE, vm_id=VM_ID, network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=NIC)])
    )
    with closing(Database(tmp_path / "route.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        route = AzureNativeRoute(owner, "vm", credential, RESOURCE, VM_ID, LOCATOR, vm, PREFIXES)
        yield SimpleNamespace(
            route=route,
            owner=owner,
            repository=repository,
            transport=transport,
            credential=credential,
            clients=clients,
            vm=vm,
        )
        assert database.operations.inspect(owner.ownership.scope) is not None


def row(owned: Any) -> Any:
    return owned.owner.inspect_lifecycle_obligation(owned.route.obligation_id)


def mutations(owned: Any) -> list[Any]:
    return [call for call in owned.transport.calls if call[0] != "GET"]


def test_passive_close_and_settle_no_cloud_work(owned):
    assert owned.clients == [] and row(owned) is None
    assert owned.route.settle(Deadline.after(5))
    assert owned.route.close(Deadline.after(5))
    for method in (owned.route.open, lambda deadline: owned.route.check(deadline, observed_vm=owned.vm)):
        with pytest.raises(StateError):
            method(Deadline.after(5))
    assert owned.transport.calls == [] and row(owned) is None


def test_locked_sdk_versions():
    assert version("azure-mgmt-network") == "32.0.0"
    assert version("azure-core") == "1.38.3"


def test_exact_rule_and_own_only_removal(owned):
    before = copy.deepcopy(owned.transport.nsg)
    owned.route.open(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert owned.route.close(Deadline.after(5))
    assert len(mutations(owned)) == 1
    assert owned.route.settle(Deadline.after(5))
    put, delete = mutations(owned)
    assert put[0] == "PUT" and delete[:2] == ("DELETE", put[1]) and delete[2] is None
    props = put[2]["properties"]
    assert props["sourceAddressPrefixes"] == list(PREFIXES)
    assert props["description"].rsplit(":", 1)[1] in put[1]
    assert props["priority"] == 100 and props["destinationPortRange"] == "22"
    assert owned.transport.nsg == before
    assert row(owned).state is LifecycleObligationState.RESOLVED
    assert b"secret" not in row(owned).payload
    assert all(response.closed for response in owned.transport.responses)
    assert owned.transport.close_calls == len(owned.clients)
    with pytest.raises(StateError):
        owned.route.open(Deadline.after(5))


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "headers",
    [{"Azure-AsyncOperation": OP}, {"Azure-AsyncOperation": OP, "Location": LOCATION}, {"Location": LOCATION}],
)
def test_put_header_even_200_requires_exact_monitor(owned, status, headers):
    owned.transport.overrides.append(("PUT", (status, headers, deny)))
    owned.route.open(Deadline.after(5))
    selected = OP if "Azure-AsyncOperation" in headers else LOCATION
    assert any(call[:2] == ("GET", selected) for call in owned.transport.calls)
    assert owned.transport.responses[2].iterations == 0
    assert owned.route.settle(Deadline.after(5))


@pytest.mark.parametrize("status", [200, 202, 204])
@pytest.mark.parametrize("header", ["Azure-AsyncOperation", "Location"])
def test_delete_header_aware_and_fresh_absence(owned, status, header):
    owned.route.open(Deadline.after(5))
    monitor = DELETE_OP if header == "Azure-AsyncOperation" else LOCATION
    owned.transport.overrides.append(("DELETE", (status, {header: monitor}, deny)))
    if header == "Location":

        def remove(response):
            if response.request.url == LOCATION:
                owned.transport.rule = None

        owned.transport.hook = remove
    assert owned.route.settle(Deadline.after(5))
    assert any(call[:2] == ("GET", monitor) for call in owned.transport.calls)
    assert row(owned).state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("stage", ["PUT", "DELETE"])
@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 429, 500, 503])
def test_actual_sdk_no_redirect_challenge_retry_or_registration(owned, stage, status):
    if stage == "DELETE":
        owned.route.open(Deadline.after(5))
    owned.transport.overrides.append((stage, (status, {"Location": OP, "WWW-Authenticate": "Bearer secret"}, deny)))
    with pytest.raises(ValidationError):
        (owned.route.open if stage == "PUT" else owned.route.settle)(Deadline.after(5))
    assert sum(call[0] == stage for call in owned.transport.calls) == 1
    assert not any("register" in call[1] for call in owned.transport.calls)
    assert owned.transport.responses[-1].iterations == 0
    assert not owned.route.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("stage", ["PUT", "DELETE"])
@pytest.mark.parametrize("error_type", [OSError, ServiceRequestError, KeyboardInterrupt, SystemExit, Control])
def test_missing_receipt_never_replayed_or_drained_by_absence(owned, stage, error_type):
    if stage == "DELETE":
        owned.route.open(Deadline.after(5))
    primary = error_type("secret provider diagnostic")
    owned.transport.overrides.append((stage, primary))
    with pytest.raises(StateError if issubclass(error_type, Exception) else error_type) as caught:
        (owned.route.open if stage == "PUT" else owned.route.settle)(Deadline.after(5))
    if issubclass(error_type, Exception):
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "secret" not in str(caught.value)
    else:
        assert caught.value is primary
    owned.transport.rule = None
    assert not owned.route.settle(Deadline.after(5))
    assert sum(call[0] == stage for call in owned.transport.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "foreign"),
        ("priority", 101),
        ("destinationPortRange", "23"),
        ("sourceAddressPrefixes", ["0.0.0.0/0"]),
        ("destinationAddressPrefixes", ["*"]),
        ("sourceApplicationSecurityGroups", [{"id": "foreign"}]),
    ],
)
def test_changed_rule_content_never_deleted(owned, field, value):
    owned.route.open(Deadline.after(5))
    owned.transport.rule["properties"][field] = value
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 1 and row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize(
    "kind",
    [
        "nic-guid",
        "nsg-guid",
        "nic-owner",
        "nsg-owner",
        "backref",
        "association",
        "shared-nic",
        "shared-subnet",
        "not-provisioned",
    ],
)
@pytest.mark.parametrize("before_open", [False, True])
def test_topology_refusals_and_replaced_parents(owned, kind, before_open):
    if not before_open:
        owned.route.open(Deadline.after(5))
    nic, nsg = owned.transport.nic, owned.transport.nsg
    if kind.endswith("guid"):
        target = nic if kind.startswith("nic") else nsg
        target["properties"]["resourceGuid"] = "31234567-89ab-0cde-0123-456789abcdef"
        if before_open:
            target["properties"]["resourceGuid"] = "invalid"
    elif kind.endswith("owner"):
        (nic if kind.startswith("nic") else nsg)["tags"]["owner"] = "foreign"
    elif kind == "backref":
        nic["properties"]["virtualMachine"]["id"] = RESOURCE + "foreign"
    elif kind == "association":
        nic["properties"]["networkSecurityGroup"]["id"] = NSG + "foreign"
        if before_open:
            nic["properties"]["networkSecurityGroup"]["id"] = NSG.replace(SUB, "foreign-subscription")
    elif kind == "shared-nic":
        nsg["properties"]["networkInterfaces"].append({"id": NIC + "foreign"})
    elif kind == "shared-subnet":
        nsg["properties"]["subnets"] = [{"id": "foreign"}]
    else:
        nsg["properties"]["provisioningState"] = "Updating"
    with pytest.raises((StateError, ValidationError)):
        (owned.route.open if before_open else owned.route.settle)(Deadline.after(5))
    assert len(mutations(owned)) == (0 if before_open else 1)


@pytest.mark.parametrize("kind", ["missing", "broad", "duplicate", "no-space", "collision"])
def test_baseline_and_priority_inventory_refusal(owned, kind):
    rules = owned.transport.nsg["properties"]["securityRules"]
    if kind == "missing":
        rules.clear()
    elif kind == "broad":
        rules[0]["properties"]["sourceAddressPrefix"] = "10.0.0.0/8"
    elif kind == "duplicate":
        rules.append(copy.deepcopy(rules[0]))
    elif kind == "collision":
        rules.append({"name": owned.route._name, "properties": dict(rules[0]["properties"], priority=100)})
    else:
        for priority in range(100, 200):
            rules.append(
                {"name": f"unrelated-{priority}", "properties": dict(rules[0]["properties"], priority=priority)}
            )
    with pytest.raises((StateError, ValidationError)):
        owned.route.open(Deadline.after(5))
    assert mutations(owned) == []


def test_priority_selection_preserves_unrelated_rules(owned):
    rules = owned.transport.nsg["properties"]["securityRules"]
    rules.append({"name": "unrelated", "properties": dict(rules[0]["properties"], priority=100)})
    owned.route.open(Deadline.after(5))
    assert mutations(owned)[0][2]["properties"]["priority"] == 101
    assert owned.route.settle(Deadline.after(5))
    assert rules[-1]["name"] == "unrelated"


@pytest.mark.parametrize("state", ["Failed", "Canceled"])
@pytest.mark.parametrize("leftover", [True, False])
def test_known_failed_create_cleanup_requires_exact_leftover_or_absence(owned, state, leftover):
    owned.transport.overrides.extend(
        [("PUT", (201, {"Azure-AsyncOperation": OP}, deny)), (OP, (200, {}, {"status": state}))]
    )
    with pytest.raises(StateError):
        owned.route.open(Deadline.after(5))
    if leftover:
        owned.transport.rule["properties"]["provisioningState"] = state
    else:
        owned.transport.rule = None
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == (2 if leftover else 1)


def test_known_created_already_removed_does_not_delete(owned):
    owned.route.open(Deadline.after(5))
    owned.transport.rule = None
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 1


@pytest.mark.parametrize("stage", ["register", "mark", "publish", "resolve"])
@pytest.mark.parametrize("error_type", [OSError, Control])
def test_lost_sqlite_reply_reconciles_exact_local_evidence(owned, monkeypatch, stage, error_type):
    method = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }[stage]
    original = getattr(owned.repository, method)
    primary = error_type("lost ledger reply")

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise primary

    if stage == "resolve":
        owned.route.open(Deadline.after(5))
    monkeypatch.setattr(owned.repository, method, fail)
    with pytest.raises(error_type) as caught:
        (owned.route.settle if stage == "resolve" else owned.route.open)(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, method, original)
    assert owned.route.settle(Deadline.after(5))
    assert row(owned).state is LifecycleObligationState.RESOLVED
    assert len(mutations(owned)) == (0 if stage in {"register", "mark"} else 2)


def test_foreign_payload_revision_cannot_admit_cleanup(owned):
    owned.route.open(Deadline.after(5))
    selected = row(owned)
    owned.repository.publish_lifecycle_obligation_payload(
        owned.owner.ownership,
        selected.obligation_id,
        expected_revision=selected.payload_revision,
        payload_version=1,
        payload=b"{}",
    )
    before = len(owned.transport.calls)
    with pytest.raises(StateError):
        owned.route.settle(Deadline.after(5))
    assert len(owned.transport.calls) == before


@pytest.mark.parametrize("expired", [True, False])
def test_expired_and_nonfinite_budget_refuse_without_work(owned, expired):
    with pytest.raises(TimeoutError if expired else ValidationError):
        owned.route.open(Deadline.after(0) if expired else Deadline(None))
    assert owned.clients == [] and row(owned) is None


@pytest.mark.parametrize("handle", ["response", "client"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit, Control])
def test_failed_original_close_requires_explicit_cleanup(owned, handle, error_type):
    owned.route.open(Deadline.after(5))

    def fail_last(response):
        if response.request.url.endswith("?api-version=2025-09-01") and "/securityRules/" in response.request.url:
            if handle == "response":
                response.close_errors.append(error_type("secret close"))
            else:
                owned.transport.close_errors.append(error_type("secret close"))

    owned.transport.hook = fail_last
    if issubclass(error_type, Exception):
        owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    else:
        with pytest.raises(error_type):
            owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert owned.route.cleanup_incomplete
    before = len(owned.transport.calls)
    with pytest.raises(StateError):
        owned.route.check(Deadline.after(5), observed_vm=owned.vm)
    assert len(owned.transport.calls) == before
    owned.transport.hook = None
    assert owned.route.close(Deadline.after(5))
    assert owned.route.settle(Deadline.after(5))


def test_late_delete_receipt_retained_without_absence_admission(owned, monkeypatch):
    owned.route.open(Deadline.after(5))
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    owned.transport.hook = lambda response: clock.__setitem__(0, 102.0) if response.request.method == "DELETE" else None
    with pytest.raises(TimeoutError):
        owned.route.settle(Deadline.after(1))
    assert owned.route.cleanup_incomplete
    assert row(owned).state is LifecycleObligationState.POSSIBLE_EFFECT
    owned.transport.hook = None
    assert owned.route.settle(Deadline.after(5))
    assert len(mutations(owned)) == 2
