"""Independent cloud binding policy and bounded provider read behavior."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from azure.mgmt.compute.models import VirtualMachine
from azure.mgmt.network.models import NetworkInterface, PublicIPAddress
from google.cloud.compute_v1.types import Instance

from agentworks.capabilities.base import RunContext
from agentworks.errors import AlreadyExistsError, ConfigError, LimitExceededError, StateError
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._runtime_prerequisite import RuntimeTargetOS
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHSettings
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp.platform import GCEPlatform

VM_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
NIC_ID = "/subscriptions/sub-A/resourceGroups/other/providers/Microsoft.Network/networkInterfaces/nic1"
PIP_ID = "/subscriptions/sub-A/resourceGroups/third/providers/Microsoft.Network/publicIPAddresses/pip1"
IP = "198.51.100.2"
SETTINGS = SSHSettings(Path("/absent/trust"), Path("/absent/key"), "/absent/agent", "custom-ssh", 17, 5)


@pytest.fixture(params=["aws", "azure", "gcp"])
def cloud(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake only SDK construction and reads; exercise real locator checks."""
    calls: list[tuple[str, dict[str, Any]]] = []
    data: dict[str, Any] = {}
    platform: Any

    def read(label: str, **kwargs: Any) -> Any:
        calls.append((label, kwargs))
        advance = data.get("advance")
        if advance is not None:
            advance(label)
        return data[label]

    if request.param == "aws":
        platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
        vm = NS(
            name="aws-vm",
            admin_username="admin",
            platform_metadata={
                "instance_id": "i-0123456789abcdef0",
                "region": "us-east-1",
                "account_id": "111122223333",
            },
        )
        data["vm"] = {
            "Reservations": [
                {
                    "OwnerId": "111122223333",
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "PublicIpAddress": IP,
                        }
                    ],
                }
            ]
        }
        client = NS(describe_instances=lambda **kw: read("vm", **kw), close=lambda: None)
        monkeypatch.setattr(platform, "_get_session", lambda ctx: NS(client=lambda *a, **kw: client))
    elif request.param == "azure":
        platform = AzureVMPlatform(
            "azure",
            {
                "subscription_id": "sub-A",
                "resource_group": "rg1",
                "region": "eastus",
                "auth": {"mode": "ambient"},
            },
        )
        vm = NS(name="azure-vm", admin_username="admin", platform_metadata={"resource_id": VM_ID})
        data["vm"] = VirtualMachine(
            {
                "id": VM_ID,
                "properties": {
                    "networkProfile": {"networkInterfaces": [{"id": NIC_ID}]},
                },
            }
        )
        data["nic"] = NetworkInterface(
            {
                "id": NIC_ID,
                "properties": {
                    "ipConfigurations": [
                        {
                            "properties": {
                                "publicIPAddress": {"id": PIP_ID},
                            }
                        }
                    ]
                },
            }
        )
        data["pip"] = PublicIPAddress({"id": PIP_ID, "properties": {"ipAddress": IP}})
        monkeypatch.setattr(
            platform,
            "_compute_client",
            lambda *a: NS(
                virtual_machines=NS(get=lambda *a, **kw: read("vm", **kw)),
            ),
        )
        monkeypatch.setattr(
            platform,
            "_network_client",
            lambda *a: NS(
                network_interfaces=NS(get=lambda *a, **kw: read("nic", **kw)),
                public_ip_addresses=NS(get=lambda *a, **kw: read("pip", **kw)),
            ),
        )
    else:
        platform = GCEPlatform(
            "gcp",
            {
                "project_id": "project-a",
                "zone": "us-central1-a",
                "auth": {"mode": "ambient"},
            },
        )
        vm = NS(
            name="gcp-vm",
            admin_username="admin",
            platform_metadata={
                "project_id": "project-a",
                "zone": "us-central1-a",
                "instance_name": "vm-a",
                "instance_id": "201",
                "network_url": "projects/project-a/global/networks/default",
                "network_tag": "tag-a",
                "allow_rule": "allow-a",
                "allow_rule_id": "202",
                "allow_source_ranges": "198.51.100.1/32",
                "deny_rule": "deny-a",
                "deny_rule_id": "203",
                "access_config_name": "External NAT",
            },
        )
        data["vm"] = Instance(
            id=201,
            network_interfaces=[
                {
                    "network": vm.platform_metadata["network_url"],
                    "access_configs": [
                        {
                            "name": "External NAT",
                            "type_": "ONE_TO_ONE_NAT",
                            "nat_i_p": IP,
                        }
                    ],
                }
            ],
        )
        platform._clients = NS(client=lambda *a: NS(get=lambda **kw: read("vm", **kw)))
    return NS(kind=request.param, platform=platform, vm=vm, data=data, calls=calls)


def _resolve(cloud: Any, *, settings: Any = SETTINGS, deadline: Deadline | None = None) -> Any:
    return cloud.platform.resolve_native_execution_binding(
        cloud.vm,
        RunContext(),
        config=NS(operator=NS(ssh=settings)),
        deadline=deadline or Deadline.after(10),
    )


@pytest.mark.parametrize("settings", [None, NS(identity_file=Path("/legacy"))])
def test_missing_policy_precedes_sdk_reads(cloud: Any, settings: Any) -> None:
    with pytest.raises(ConfigError) as exc:
        _resolve(cloud, settings=settings)
    assert exc.value.entity_name == cloud.vm.name
    assert cloud.calls == []


@pytest.mark.parametrize("account", ["admin", "root"])
def test_explicit_passive_binding_and_fresh_delivery(cloud: Any, account: str) -> None:
    cloud.vm.admin_username = account
    binding = _resolve(cloud)
    connection = binding.carrier._connection
    assert (connection.host, connection.port, connection.user, connection.host_key_alias) == (IP, 22, account, None)
    assert connection.identity_file == SETTINGS.identity_file
    assert connection.trust == ManagedSSHTrust(SETTINGS.trust_store)
    assert (connection.agent_socket, connection.ssh_executable) == (SETTINGS.agent_socket, SETTINGS.ssh_executable)
    assert (connection.keepalive_interval, connection.keepalive_count_max) == (17, 5)
    assert binding.runtime_selection.target_os is RuntimeTargetOS.LINUX
    assert binding._independent_availability is None
    route = binding._early_guest_facts_route
    assert route.carrier is binding.carrier
    assert route.account == account
    assert (route.root_entry.expected.euid, route.root_entry.expected.egid, route.root_entry.expected.groups) == (
        0,
        0,
        (0,),
    )
    assert route.root_entry.mode is (IdentityMode.DIRECT if account == "root" else IdentityMode.SUDO_ROOT)
    first, second = binding._new_managed_delivery(), binding._new_managed_delivery()
    assert first is not second and first is not binding.carrier
    assert first._connection is second._connection is connection


def test_context_policy_and_explicit_override(cloud: Any) -> None:
    ctx = RunContext(config=NS(operator=NS(ssh=SETTINGS)))
    binding = cloud.platform.resolve_native_execution_binding(cloud.vm, ctx, deadline=Deadline.after(10))
    assert binding.carrier._connection.identity_file == SETTINGS.identity_file
    with pytest.raises(ConfigError):
        cloud.platform.resolve_native_execution_binding(
            cloud.vm,
            ctx,
            config=NS(operator=NS(ssh=None)),
            deadline=Deadline.after(10),
        )


@pytest.mark.parametrize("endpoint", ["", "bad", "2001:db8::1", None])
def test_invalid_or_missing_current_endpoint(cloud: Any, endpoint: Any) -> None:
    if cloud.kind == "aws":
        cloud.data["vm"]["Reservations"][0]["Instances"][0]["PublicIpAddress"] = endpoint
    elif cloud.kind == "azure":
        cloud.data["pip"] = PublicIPAddress({"id": PIP_ID, "properties": {"ipAddress": endpoint}})
    else:
        cloud.data["vm"].network_interfaces[0].access_configs[0].nat_i_p = endpoint or ""
    from agentworks.plugins.gcp.errors import GCEError

    with pytest.raises((StateError, GCEError)):
        _resolve(cloud)


def test_actual_owner_mismatch(cloud: Any) -> None:
    if cloud.kind == "aws":
        cloud.data["vm"]["Reservations"][0]["OwnerId"] = "999988887777"
    elif cloud.kind == "azure":
        cloud.data["vm"] = VirtualMachine({"id": VM_ID + "other"})
    else:
        cloud.data["vm"].id = 999
    with pytest.raises((StateError, AlreadyExistsError)):
        _resolve(cloud)


def test_original_deadline_rejects_late_success(cloud: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    cloud.data["advance"] = lambda label: now.__setitem__(0, 111.0)
    with pytest.raises(LimitExceededError):
        _resolve(cloud, deadline=Deadline.after(10))
    assert len(cloud.calls) == 1


def test_expired_deadline_precedes_reads(cloud: Any) -> None:
    with pytest.raises(LimitExceededError):
        _resolve(cloud, deadline=Deadline.after(0))
    assert cloud.calls == []


def test_setup_expiry_precedes_dispatch(cloud: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])

    def late(factory: Any) -> Any:
        def setup(*args: Any, **kwargs: Any) -> Any:
            result = factory(*args, **kwargs)
            now[0] = 111.0
            return result

        return setup

    if cloud.kind == "aws":
        monkeypatch.setattr(cloud.platform, "_get_session", late(cloud.platform._get_session))
    elif cloud.kind == "azure":
        monkeypatch.setattr(cloud.platform, "_compute_client", late(cloud.platform._compute_client))
    else:
        cloud.platform._clients.client = late(cloud.platform._clients.client)
    with pytest.raises(LimitExceededError):
        _resolve(cloud, deadline=Deadline.after(10))
    assert cloud.calls == []


def test_azure_network_setup_expiry_precedes_dispatch(cloud: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    if cloud.kind != "azure":
        pytest.skip("Azure network client")
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    original = cloud.platform._network_client

    def late(*args: Any) -> Any:
        result = original(*args)
        now[0] = 111.0
        return result

    monkeypatch.setattr(cloud.platform, "_network_client", late)
    with pytest.raises(LimitExceededError):
        _resolve(cloud, deadline=Deadline.after(10))
    assert [label for label, _ in cloud.calls] == ["vm"]


@pytest.mark.parametrize("fault", ["network", "subnet", "access"])
def test_gcp_persisted_network_policy(cloud: Any, fault: str) -> None:
    if cloud.kind != "gcp":
        pytest.skip("GCP network identity")
    if fault == "network":
        cloud.data["vm"].network_interfaces[0].network += "other"
    elif fault == "subnet":
        cloud.vm.platform_metadata["subnet_url"] = "projects/project-a/regions/us-central1/subnetworks/expected"
    else:
        cloud.data["vm"].network_interfaces[0].access_configs[0].name = "other"
    from agentworks.plugins.gcp.errors import GCEError

    with pytest.raises(GCEError):
        _resolve(cloud)


def test_binding_construction_does_not_admit_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.vms._ssh_native_binding import ssh_native_binding

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("binding construction must stay passive")

    for method in ("stat", "is_file", "exists", "open"):
        monkeypatch.setattr(Path, method, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    binding = ssh_native_binding(NS(name="vm", admin_username="admin"), IP, SETTINGS)
    assert binding.carrier.features.live_stdio
    assert binding._new_managed_delivery is not None
    assert binding._new_managed_delivery() is not binding.carrier


def test_azure_downstream_reads_share_budget(cloud: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    if cloud.kind != "azure":
        pytest.skip("Azure linked resources")
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    cloud.data["advance"] = lambda label: now.__setitem__(0, now[0] + 2)
    _resolve(cloud, deadline=Deadline.after(10))
    assert [kw["timeout"] for _, kw in cloud.calls] == [10, 8, 6]
    for _, kw in cloud.calls:
        assert all(kw[key] == 0 for key in ("retry_total", "retry_connect", "retry_read", "retry_status"))


def test_azure_linked_reads_use_real_sdk_serialization() -> None:
    from azure.core.credentials import AccessToken
    from azure.core.pipeline.transport import HttpResponse, HttpTransport
    from azure.mgmt.network import NetworkManagementClient

    from agentworks.plugins.azure.network import read_native_public_ipv4

    calls: list[tuple[str, dict[str, Any]]] = []

    class Response(HttpResponse):
        def __init__(self, request: Any, payload: dict[str, Any]) -> None:
            super().__init__(request, None)
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self.content_type = "application/json"
            self.payload = json.dumps(payload).encode()
            self.data = payload

        def body(self) -> bytes:
            return self.payload

        def json(self) -> dict[str, Any]:
            return self.data

    class Transport(HttpTransport):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        def send(self, request: Any, **kwargs: Any) -> Response:
            calls.append((request.url, kwargs))
            if "/networkInterfaces/" in request.url:
                payload = {
                    "id": NIC_ID,
                    "properties": {
                        "ipConfigurations": [
                            {
                                "properties": {
                                    "publicIPAddress": {"id": PIP_ID},
                                }
                            }
                        ]
                    },
                }
            else:
                payload = {"id": PIP_ID, "properties": {"ipAddress": IP}}
            return Response(request, payload)

    credential = NS(get_token=lambda *a, **kw: AccessToken("fixture-token", int(time.time()) + 3600))
    client = NetworkManagementClient(credential, "sub-A", transport=Transport())
    vm = VirtualMachine({"properties": {"networkProfile": {"networkInterfaces": [{"id": NIC_ID}]}}})
    endpoint = read_native_public_ipv4(client, vm, subscription_id="sub-A", vm_name="vm", deadline=Deadline.after(10))
    assert endpoint == IP
    assert len(calls) == 2
    assert NIC_ID in calls[0][0] and PIP_ID in calls[1][0]
    assert all(0 < kwargs["read_timeout"] <= kwargs["connection_timeout"] <= 10 for _, kwargs in calls)


@pytest.mark.parametrize("resource", ["nic", "pip"])
@pytest.mark.parametrize("fault", ["returned-id", "subscription", "malformed", "late"])
def test_azure_linked_identity_and_deadline(
    cloud: Any, monkeypatch: pytest.MonkeyPatch, resource: str, fault: str
) -> None:
    if cloud.kind != "azure":
        pytest.skip("Azure linked resources")
    if fault == "returned-id":
        cloud.data[resource].id += "other"
    elif fault in ("subscription", "malformed"):
        value = (NIC_ID if resource == "nic" else PIP_ID).replace("sub-A", "sub-B")
        if fault == "malformed":
            value += "/extra"
        if resource == "nic":
            cloud.data["vm"].network_profile.network_interfaces[0].id = value
        else:
            cloud.data["nic"].ip_configurations[0].public_ip_address.id = value
    else:
        now = [100.0]
        monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
        cloud.data["advance"] = lambda label: now.__setitem__(0, 111.0 if label == resource else 100.0)
    with pytest.raises(LimitExceededError if fault == "late" else StateError):
        _resolve(cloud)


@pytest.mark.parametrize(
    "provider,platform", [("aws", "EC2Platform"), ("azure", "AzureVMPlatform"), ("gcp", "GCEPlatform")]
)
def test_fresh_process_hook_blocks_retired_roots(provider: str, platform: str) -> None:
    source = f'''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + ".") for root in
               ("agentworks.transports", "agentworks.ssh", "agentworks.remote_exec",
                "agentworks.harness_setup.runner", "agentworks.native_files",
                "agentworks.plugins.proxmox.transport")):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
def forbid_effects(event, args):
    if event in ("subprocess.Popen", "os.system", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError(event)
sys.addaudithook(forbid_effects)
from types import SimpleNamespace as NS
from pathlib import Path
from agentworks.plugins.{provider}.platform import {platform}
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.execution.carrier import Deadline
from agentworks.capabilities.base import RunContext
p = {platform}.__new__({platform})
p._locator_metadata = lambda vm: ()
p._read_exact_instance = lambda *a, **kw: {{"PublicIpAddress": "{IP}"}}
from agentworks.vms._ssh_native_binding import ssh_native_binding
if "{provider}" == "azure":
    import agentworks.plugins.azure.platform as mod
    mod._parse_locator_resource_id = lambda *a, **kw: ("rg", "vm", NS(subscription_id="sub-A"))
    p._read_exact_vm = lambda *a, **kw: NS()
    p._network_client = lambda *a: NS()
    import agentworks.plugins.azure.network as net
    net.read_native_public_ipv4 = lambda *a, **kw: "{IP}"
elif "{provider}" == "gcp":
    import agentworks.plugins.gcp.platform as mod
    mod._VMIdentity.from_row = lambda vm: NS(network_url="n", subnet_url=None, access_config_name="a")
    mod.verify_instance_network = lambda *a, **kw: None
    mod.live_external_ipv4 = lambda *a, **kw: "{IP}"
vm = NS(name="vm", admin_username="admin", platform_metadata={{"resource_id":"id"}})
config = NS(operator=NS(ssh=SSHSettings(Path("/absent/trust"), Path("/absent/key"))))
b = p.resolve_native_execution_binding(vm, RunContext(), config=config, deadline=Deadline.after(10))
assert b.carrier._connection.host == "{IP}"
assert b._new_managed_delivery()._connection is b.carrier._connection
'''
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
