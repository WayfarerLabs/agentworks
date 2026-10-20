"""Literal locked-SDK creation requests with real SQLite resource custody."""

from __future__ import annotations

import copy
import json
import socket
from contextlib import closing
from types import SimpleNamespace

import pytest
from azure.core.pipeline.transport import HttpTransport

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _route_client
from agentworks.plugins.azure._creation_request import AzureCreationRequests, CreationDeadlineExpired, resource_url
from agentworks.plugins.azure._creation_resources import CreationResource
from tests.plugins.azure.test_activation_sdk import Credential, deny
from tests.plugins.azure.test_native_route import RuleResponse

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PREFIX = f"/subscriptions/{SUB}/resourceGroups/test/providers/"
IP = PREFIX + "Microsoft.Network/publicIPAddresses/attempt-ip"
VM = PREFIX + "Microsoft.Compute/virtualMachines/test-vm"
DISK = PREFIX + "Microsoft.Compute/disks/attempt-os"
UUID = "11234567-89ab-0cde-0123-456789abcdef"
MONITOR = (
    f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.Network/"
    "operations/new?api-version=2025-09-01"
)
TAGS = {"owner": "agentworks", "agentworks-creation": "attempt"}


class Control(BaseException):
    pass


class Transport(HttpTransport):
    def __init__(self):
        self.values = {}
        self.calls = []
        self.responses = []
        self.fail_put = None
        self.put_status = 201
        self.monitor = False
        self.keep_disk = False
        self.close_error = None
        self.put_close_error = None
        self.pending_identity = False

    def open(self):
        pass

    def close(self):
        if self.close_error is not None:
            raise self.close_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.calls.append(request)
        headers = {}
        if request.url == MONITOR:
            assert request.method == "GET"
            status, value = 200, {"status": "Succeeded"}
        elif request.method == "GET":
            value = self.values.get(request.url)
            status = 404 if value is None else 200
        elif request.method == "PUT":
            value = json.loads(request.body)
            value["id"] = VM if "/virtualMachines/" in request.url else IP
            value["properties"]["provisioningState"] = "Succeeded"
            value["properties"]["vmId" if value["id"] == VM else "resourceGuid"] = UUID
            if value["id"] == VM:
                value["properties"]["storageProfile"] = {"osDisk": {"managedDisk": {"id": DISK}}}
                self.values[resource_url(DISK)] = {"id": DISK}
            self.values[request.url] = copy.deepcopy(value)
            if self.fail_put is not None:
                self.close_error = self.put_close_error
                raise self.fail_put
            status = self.put_status
            if self.monitor:
                headers = {"Azure-AsyncOperation": MONITOR}
            if self.pending_identity:
                value["properties"].pop("vmId", None)
                value["properties"].pop("resourceGuid", None)
                value["properties"].pop("storageProfile", None)
                value["properties"]["provisioningState"] = "Updating"
        else:
            assert request.method == "DELETE"
            self.values.pop(request.url, None)
            if "/virtualMachines/" in request.url and not self.keep_disk:
                self.values.pop(resource_url(DISK), None)
            status, value = 204, None
        response = RuleResponse(request, status, headers, copy.deepcopy(value))
        self.responses.append(response)
        return response


@pytest.fixture
def owned(tmp_path, monkeypatch):
    for target, method in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, method, deny)
    transport = Transport()
    credential = Credential()
    original = _route_client.network_route_client
    monkeypatch.setattr(
        _route_client,
        "network_route_client",
        lambda selected, subscription: original(selected, subscription, transport=transport),
    )
    with closing(Database(tmp_path / "creation.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        plan = (
            CreationResource(IP, {"tags": TAGS, "properties": {}}),
            CreationResource(VM, {"tags": TAGS, "properties": {}}),
        )
        requests = AzureCreationRequests(owner, SUB, plan, DISK)
        yield SimpleNamespace(requests=requests, owner=owner, transport=transport, credential=credential)


def create(owned, index):
    deadline = Deadline.after(10)
    with owned.requests.custody(deadline):
        return owned.requests.create(index, owned.credential, deadline)


def test_passive_resources_and_exact_reverse_order_rollback(owned):
    assert not owned.transport.calls
    deadline = Deadline.after(10)
    owned.requests.register(deadline)
    create(owned, 0)
    create(owned, 1)
    with owned.requests.custody(deadline):
        assert owned.requests.rollback(owned.credential, deadline)
    assert owned.requests.reconcile(deadline, resolve=True)
    deletes = [item.url for item in owned.transport.calls if item.method == "DELETE"]
    assert deletes == [resource_url(VM), resource_url(IP)]
    puts = [item for item in owned.transport.calls if item.method == "PUT"]
    assert len(puts) == 2
    assert "If-None-Match" not in puts[0].headers
    assert puts[1].headers["If-None-Match"] == "*"
    assert all(response.close_calls for response in owned.transport.responses)


def test_existing_auxiliary_refuses_without_mutation(owned):
    owned.transport.values[resource_url(IP)] = {"id": IP}
    owned.requests.register(Deadline.after(10))
    with pytest.raises(StateError):
        create(owned, 0)
    assert [item.method for item in owned.transport.calls] == ["GET"]


@pytest.mark.parametrize("failure", [OSError("lost reply"), Control("interrupted reply")])
def test_lost_put_reply_never_adopts_or_replays(owned, failure):
    deadline = Deadline.after(10)
    owned.requests.register(deadline)
    owned.transport.fail_put = failure
    with pytest.raises(type(failure) if isinstance(failure, Control) else StateError):
        create(owned, 0)
    assert owned.requests.resources[0].identity is None
    assert owned.requests.resources[0].create_ack is None
    with pytest.raises(StateError):
        owned.requests.rollback(owned.credential, deadline)
    with pytest.raises(StateError):
        create(owned, 0)
    assert [item.method for item in owned.transport.calls] == ["GET", "PUT"]
    assert owned.owner.list_pending_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


def test_monitor_completion_does_not_replay_mutation(owned):
    deadline = Deadline.after(10)
    owned.requests.register(deadline)
    owned.transport.monitor = True
    create(owned, 0)
    assert [item.method for item in owned.transport.calls] == ["GET", "PUT", "GET", "GET"]
    assert owned.transport.calls[2].url == MONITOR
    assert owned.requests.resources[0].create_state == "Succeeded"


def test_upsert_receipt_does_not_establish_creation_provenance(owned):
    owned.requests.register(Deadline.after(10))
    owned.transport.put_status = 200
    with pytest.raises(StateError):
        create(owned, 0)
    assert owned.requests.resources[0].identity is None
    with pytest.raises(StateError):
        owned.requests.rollback(owned.credential, Deadline.after(10))
    assert not any(item.method == "DELETE" for item in owned.transport.calls)


def test_acknowledged_async_creation_observes_final_incarnation(owned):
    owned.requests.register(Deadline.after(10))
    owned.transport.monitor = owned.transport.pending_identity = True
    create(owned, 0)
    create(owned, 1)
    assert owned.requests.resources[-1].identity == UUID
    assert owned.requests.observed_disk_id == DISK
    assert len([item for item in owned.transport.calls if item.method == "PUT"]) == 2


@pytest.mark.parametrize("status", [302, 401, 409, 429, 500, 503])
def test_sdk_has_no_redirect_challenge_or_service_retry(owned, status):
    owned.requests.register(Deadline.after(10))
    owned.transport.put_status = status
    with pytest.raises((StateError, ValidationError)):
        create(owned, 0)
    assert [item.method for item in owned.transport.calls] == ["GET", "PUT"]


def test_retained_disk_prevents_false_rollback_success(owned, monkeypatch):
    deadline = Deadline.after(10)
    owned.requests.register(deadline)
    create(owned, 0)
    create(owned, 1)
    owned.transport.keep_disk = True
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "agentworks.plugins.azure._creation_request.time.sleep", lambda delay: clock.__setitem__(0, 110.0)
    )
    deadline = Deadline(105.0)
    with owned.requests.custody(deadline), pytest.raises(CreationDeadlineExpired):
        owned.requests.rollback(owned.credential, deadline)
    assert owned.owner.list_pending_lifecycle_obligations()
    assert resource_url(DISK) in owned.transport.values


def test_control_survives_failed_original_close(owned):
    deadline = Deadline.after(10)
    owned.requests.register(deadline)
    control = Control("dispatch control")
    owned.transport.fail_put = control
    owned.transport.put_close_error = OSError("close failed")
    with pytest.raises(Control) as caught:
        create(owned, 0)
    assert caught.value is control
    assert owned.requests.cleanup_incomplete
