"""Offline real SDK/SQLite composition, with guest preparation explicitly stubbed.

The reader, start, route, source-response ownership and credential lifecycle are
real. Scripted HTTP transports replace the provider network. This is not native
guest delivery or physical socket-drain evidence.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.config import Config, DefaultsConfig, OperatorConfig, PathsConfig, SessionConfig
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _activation_client, _passive_clients, _route_client
from agentworks.plugins.azure._activation import start_url
from agentworks.plugins.azure._native_execution_access import AzureOwnedAccess
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.vms.target_preparation import VMTargetPreparation, VMTargetPreparationStatus
from tests.plugins.azure.test_activation_sdk import Credential, deny
from tests.plugins.azure.test_incarnation_start import OPERATION, Transport
from tests.plugins.azure.test_native_access_sdk import ScriptedTransport
from tests.plugins.azure.test_native_access_sdk import offline_native_guard as offline_native_guard
from tests.plugins.azure.test_native_route import (
    DELETE_OP,
    LINK,
    LOCATOR,
    NIC,
    OP,
    RESOURCE,
    SUB,
    VM_ID,
    RouteTransport,
)
from tests.vms.native_stages import prepare_native

PIP = f"{LINK}/publicIPAddresses/actual-ip"
ENDPOINT = "198.51.100.7"


def vm_payload(power: VMStatus) -> dict[str, Any]:
    return {
        "id": RESOURCE.swapcase(),
        "location": "eastus",
        "properties": {
            "vmId": VM_ID.upper(),
            "instanceView": {"statuses": [{"code": "PowerState/" + power.value}]},
            "networkProfile": {"networkInterfaces": [{"id": NIC.upper()}]},
        },
    }


@pytest.fixture
def unified(request, tmp_path, monkeypatch):
    power, close_fault = request.param
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    route = RouteTransport()
    nic = cast("dict[str, Any]", route.nic)
    nic["id"] = NIC.lower()
    nic["properties"]["virtualMachine"]["id"] = RESOURCE.upper()
    nic["properties"]["ipConfigurations"] = [{"properties": {"publicIPAddress": {"id": PIP.swapcase()}}}]
    route.overrides = [
        ("PUT", (201, {"Azure-AsyncOperation": OP}, deny)),
        ("DELETE", (202, {"Azure-AsyncOperation": DELETE_OP}, deny)),
    ]
    # Exact finite scripts make an additional read or missing startup read fail.
    reads = [power, power]
    reads.extend([VMStatus.RUNNING] * (1 if close_fault else (4 if power is VMStatus.RUNNING else 5)))
    compute = ScriptedTransport([(200, vm_payload(state), {}) for state in reads])
    endpoint_reads = 1 if close_fault else 2
    network_replies: list[tuple[int, dict[str, Any], dict[str, str]]] = []
    for _ in range(endpoint_reads):
        network_replies.extend(
            [
                (200, nic, {}),
                (200, {"id": PIP.upper(), "properties": {"ipAddress": ENDPOINT}}, {}),
            ]
        )
    network = ScriptedTransport(network_replies)
    start = Transport()
    if power is not VMStatus.RUNNING:
        start.replies = [
            (202, {"Azure-AsyncOperation": OPERATION}, deny),
            (200, {}, [b'{"status":"Succeeded"}']),
        ]
    clients: dict[str, list[Any]] = {kind: [] for kind in ("compute", "network", "start", "route")}
    events: list[str] = []
    constructions: list[Credential] = []
    guest_calls: list[ProviderLocator] = []

    class SourceResponse:
        closed = 0

        def read(self, limit):
            assert limit == 65
            return b"192.0.2.1\n"

        def close(self):
            self.closed += 1
            events.append("source-close")

    source = SourceResponse()

    class RetainedCredential(Credential):
        closed = 0

        def close(self):
            # This public close must follow every returned dependent original
            # and both durable provider-effect resolutions, while owner remains.
            assert compute.closed == len(clients["compute"])
            assert network.closed == len(clients["network"])
            assert start.close_calls == len(clients["start"])
            assert route.close_calls == len(clients["route"])
            assert all(response.closed for response in start.responses + route.responses)
            assert source.closed == 1
            assert not owner.list_pending_lifecycle_obligations()
            assert database.operations.inspect(owner.ownership.scope) is not None
            self.closed += 1
            events.append("credential-close")

    credential = RetainedCredential()

    def construct_credential(**kwargs):
        constructions.append(credential)
        return credential

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", construct_credential)

    # Preserve each real factory's policy list, API version and model parsing.
    # Only the transport argument is injected, with the same cached credential.
    def factory(kind: str, original: Any, transport: Any) -> Any:
        def construct(selected, subscription):
            assert selected is credential and credential.closed == 0 and subscription == SUB
            client = original(selected, subscription, transport=transport)
            clients[kind].append(client)
            return client

        return construct

    monkeypatch.setattr(
        _passive_clients, "compute_read_client", factory("compute", _passive_clients.compute_read_client, compute)
    )
    monkeypatch.setattr(
        _passive_clients, "network_read_client", factory("network", _passive_clients.network_read_client, network)
    )
    monkeypatch.setattr(
        _activation_client, "compute_start_client", factory("start", _activation_client.compute_start_client, start)
    )
    monkeypatch.setattr(
        _route_client, "network_route_client", factory("route", _route_client.network_route_client, route)
    )
    monkeypatch.setattr("agentworks.vms._ssh_native_exposure.urlopen", lambda *a, **k: source)

    def fail_put_response_close(response):
        if response.request.method == "PUT":
            response.close_errors = [RuntimeError("synthetic public response close")] * 2

    if close_fault:
        monkeypatch.setattr(route, "hook", fail_put_response_close)

    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    config = Config(
        OperatorConfig(tmp_path / "public", tmp_path / "private", ssh=settings),
        PathsConfig(),
        DefaultsConfig(),
        tmp_path / "absent.toml",
        SessionConfig(),
    )
    ctx = RunContext(config=config)
    platform = AzureVMPlatform("offline", {"subscription_id": SUB, "resource_group": "gr:oup", "region": "eastus"})
    with closing(Database(tmp_path / "unified.db")) as database:
        database.insert_vm("vm", "offline", "vm", admin_username="root", instance_marker="0" * 32)
        database.update_vm_platform_metadata("vm", {"resource_id": RESOURCE, "vm_id": VM_ID})
        vm = database.get_vm("vm")
        assert vm is not None
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm"), "unified")
        access = platform.build_native_execution_access(vm, ctx, owner=owner, custody=LocalDeliveryCustody())
        assert isinstance(access, AzureOwnedAccess)

        def guest_stub(
            selected_vm,
            selected_platform,
            locator,
            binding,
            *,
            deadline: Deadline,
            owner: OperationOwner,
            observe_locator,
        ):
            assert selected_vm is vm and selected_platform is platform and owner is probe.owner
            assert deadline is probe.deadline and locator == LOCATOR and binding is access.binding
            assert access.route_check is not None and observe_locator.__self__ is access
            assert observe_locator(deadline) == LOCATOR
            guest_calls.append(locator)
            target = ManagedTargetIdentity(ManagedTargetKind.VM, vm.name, "v1:" + "0" * 64, VM_ID)
            return VMTargetPreparation(VMTargetPreparationStatus.PREPARED, target, None)

        monkeypatch.setattr("agentworks.vms._native_operation.prepare_managed_vm_target_from_platform", guest_stub)
        deadline = Deadline.after(5)
        probe = SimpleNamespace(
            access=access,
            owner=owner,
            repository=repository,
            database=database,
            deadline=deadline,
            power=power,
            compute=compute,
            network=network,
            start=start,
            route=route,
            credential=credential,
            source=source,
            clients=clients,
            constructions=constructions,
            guest_calls=guest_calls,
            events=events,
            vm=vm,
        )
        yield probe
        # Each test explicitly completes cleanup and outer owner finalization.
        assert database.operations.inspect(owner.ownership.scope) is None


def assert_closed_and_finalize(probe: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    p = probe
    assert p.events[-1] == "credential-close" and p.credential.closed == 1
    assert p.constructions == [p.credential]
    assert p.source.closed == 1 and not p.owner.list_pending_lifecycle_obligations()
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None
    assert not p.access.cleanup_incomplete
    assert p.vm.platform_metadata == {"resource_id": RESOURCE, "vm_id": VM_ID}
    assert p.access._reader.recorded_locator == LOCATOR
    for transport in (p.compute, p.network):
        with pytest.raises(StopIteration):
            next(transport.replies)
    assert not p.start.replies and not p.route.overrides
    assert all(response.closed for response in p.start.responses + p.route.responses)
    assert p.route.rule is None
    p.owner.seal_lifecycle_obligations()
    p.owner.record_effects_resolved()
    p.owner.close()
    assert p.database.operations.inspect(p.owner.ownership.scope) is None
    before = [len(transport.calls) for transport in (p.compute, p.network, p.start, p.route)]
    tokens = p.credential.calls
    # A fully settled access cannot inspect or mutate the now-removed ledger.
    for method in (
        "inspect_lifecycle_obligation",
        "list_pending_lifecycle_obligations",
        "register_lifecycle_obligation",
        "mark_lifecycle_obligation_possible_effect",
        "publish_lifecycle_obligation_payload",
        "resolve_lifecycle_obligation",
    ):
        monkeypatch.setattr(p.repository, method, deny)
    assert p.access.settle(Deadline.after(5))
    assert before == [len(transport.calls) for transport in (p.compute, p.network, p.start, p.route)]
    assert tokens == p.credential.calls and p.credential.closed == 1


@pytest.mark.parametrize(
    "unified", [(power, False) for power in (VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED)], indirect=True
)
def test_actual_sdk_composition_keeps_exact_owner_and_credential_until_all_effects_settle(unified, monkeypatch):
    p = unified
    assert not p.constructions and not any(p.clients.values())
    assert p.access.observe_power(p.deadline) is p.power
    prepare_native(p, p.power, p.deadline)
    assert p.guest_calls == [LOCATOR] and p.access.route_check is not None
    assert len(p.owner.list_pending_lifecycle_obligations()) == 1
    assert p.credential.closed == 0 and p.source.closed == 1
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None
    started = p.power is not VMStatus.RUNNING
    assert p.start.calls == ([("POST", start_url(RESOURCE)), ("GET", OPERATION)] if started else [])
    assert sum(method == "PUT" for method, _, _ in p.route.calls) == 1
    assert ("GET", OP, None) in p.route.calls
    assert sum(method == "DELETE" for method, _, _ in p.route.calls) == 0
    assert p.access.settle(p.deadline)
    assert sum(method == "PUT" for method, _, _ in p.route.calls) == 1
    assert sum(method == "DELETE" for method, _, _ in p.route.calls) == 1
    assert ("GET", DELETE_OP, None) in p.route.calls
    assert len(p.route.calls) == 18
    assert len(p.compute.calls) == (7 if started else 6)
    assert len(p.network.calls) == 4
    for method, url, headers, options in p.compute.calls:
        assert method == "GET" and unquote(urlsplit(url).path).lower() == RESOURCE.lower()
        assert headers["Authorization"] == "Bearer synthetic-offline-token"
        assert 0 < options["connection_timeout"] <= 5
    assert all(
        parse_qs(urlsplit(call[1]).query).get("$expand") == ["instanceView"]
        for index, call in enumerate(p.compute.calls)
        if index not in ({3, 5} if started else {2, 4})
    )
    assert_closed_and_finalize(p, monkeypatch)


@pytest.mark.parametrize("unified", [(VMStatus.RUNNING, True)], indirect=True)
def test_actual_route_response_close_failure_retains_owner_then_fresh_cleanup_finishes(unified, monkeypatch):
    p = unified
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    with pytest.raises(StateError):
        prepare_native(p, VMStatus.RUNNING, p.deadline)
    original = next(response for response in p.route.responses if response.request.method == "PUT")
    assert original.close_calls == 2 and not original.closed
    assert p.access.cleanup_incomplete and p.credential.closed == 0
    assert not p.guest_calls and not p.workflow.routes and p.access.route_check is None
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None
    assert len(p.owner.list_pending_lifecycle_obligations()) == 1
    before = (len(p.compute.calls), len(p.network.calls), len(p.constructions))
    assert p.access.settle(Deadline.after(5))
    assert original.closed and original.close_calls == 3
    assert before == (len(p.compute.calls), len(p.network.calls), len(p.constructions))
    assert not p.start.calls
    assert sum(method == "PUT" for method, _, _ in p.route.calls) == 1
    assert sum(method == "DELETE" for method, _, _ in p.route.calls) == 1
    assert ("GET", OP, None) in p.route.calls and ("GET", DELETE_OP, None) in p.route.calls
    assert len(p.route.calls) == 12
    assert_closed_and_finalize(p, monkeypatch)
