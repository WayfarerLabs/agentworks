"""Registration and native binding composition with synthetic external observations.

This proves import independence, not native platform or SSH acceptance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.execution.test_independence import _run_fresh_process


@pytest.mark.windows
@pytest.mark.parametrize("first_import", ["agentworks.plugins", "agentworks.capabilities.vm_platform.registry"])
def test_shipped_registration_and_native_bindings_without_retired_modules(first_import: str, tmp_path: Path) -> None:
    script = r"""
import importlib
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def forbid_effects(event, args):
    if event in ("subprocess.Popen", "os.system", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError("passive binding performed I/O: " + event)

sys.addaudithook(forbid_effects)
importlib.import_module(sys.argv[2])
import agentworks.plugins as plugins
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.registry import VM_PLATFORM_REGISTRY
from agentworks.errors import ConfigError, StateError
from agentworks.execution import _lima_selection
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import CapturedOutput, Deadline, SinkOutput
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.lima import LimaCarrier
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHSettings
from agentworks.execution.carriers.wsl2 import WSL2Carrier

assert_first_party_origins()

# Importing the actual shipped index must seat every installed descriptor.
assert plugins._INSTALLED_MODULES
adapters = plugins.capability_adapters()
for module in plugins._INSTALLED_MODULES:
    plugin = module.PLUGIN
    assert plugins.SYSTEM_PLUGINS[plugin.name] is plugin
    for kind, implementations in plugin.capabilities.items():
        for implementation in implementations:
            assert adapters[kind].peek(implementation.name) is implementation

wsl_vm = SimpleNamespace(name="fixture-wsl", admin_username="fixture-user",
                         platform_metadata={"distro_name": "fixture-distro"})
wsl = VM_PLATFORM_REGISTRY["wsl2"]("fixture-wsl-site", {})
wsl_binding = wsl.resolve_native_execution_binding(wsl_vm, RunContext(), deadline=Deadline(None))
assert isinstance(wsl_binding.carrier, WSL2Carrier)
assert wsl_binding.carrier.connection.distribution == "fixture-distro"
assert wsl_binding.carrier.connection.user == "fixture-user"
assert wsl_binding.delivery_account == "fixture-user"
assert wsl_binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)

class FixtureSecrets:
    def __init__(self):
        self.requests = []
    def get(self, name):
        self.requests.append(name)
        assert name == "fixture-token"
        return "fixture-value"

secrets = FixtureSecrets()
pve_vm = SimpleNamespace(name="fixture-pve", admin_username="fixture-user",
                         platform_metadata={"vmid": "101", "node": "recorded-node"})
pve = VM_PLATFORM_REGISTRY["proxmox"]("fixture-pve-site", {
    "api_url": "https://pve.example:8006", "node": "configured-node",
    "token_id": "fixture@pam!token", "token_secret": "fixture-token",
    "template_vmids": {"trixie": 9001},
})
pve_binding = pve.resolve_native_execution_binding(
    pve_vm, RunContext(secrets=secrets), deadline=Deadline(None))
assert isinstance(pve_binding.carrier, ProxmoxCarrier)
connection = pve_binding.carrier._wire._connection
assert (connection.node, connection.vmid, connection.token_secret) == (
    "recorded-node", 101, "fixture-value")
assert secrets.requests == ["fixture-token"]
assert pve_binding.delivery_account == "root"
assert pve_binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)

# SDK factories are the credential boundary. Keep provider identity validation,
# linked endpoint reads, actual resolvers and binding factories in this process.
work = Path(sys.argv[3])
settings = SSHSettings(work / "trust", work / "identity")
config = SimpleNamespace(operator=SimpleNamespace(ssh=settings))
ctx = RunContext(config=config)
deadline = Deadline.after(10)
reads = []

def closed(kind):
    reads.append((kind, "close"))

def ec2_get(*, InstanceIds):
    assert InstanceIds == ["i-0123456789abcdef0"]
    reads.append(("aws", "instance"))
    return {"Reservations": [{"OwnerId": "123456789012", "Instances": [{
        "InstanceId": InstanceIds[0], "PublicIpAddress": "192.0.2.11",
    }]}]}

def ec2_client(kind, *, region_name, config):
    assert kind == "ec2" and region_name == "us-east-1"
    assert 0 < config.connect_timeout <= 10 and 0 < config.read_timeout <= 10
    reads.append(("aws", "client"))
    return SimpleNamespace(describe_instances=ec2_get, close=lambda: closed("aws"))

def ec2_session(selected_ctx):
    assert selected_ctx is ctx
    reads.append(("aws", "session"))
    return SimpleNamespace(client=ec2_client)

resource = "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
nic = "/subscriptions/sub/resourceGroups/net-group/providers/Microsoft.Network/networkInterfaces/nic"
pip = "/subscriptions/sub/resourceGroups/ip-group/providers/Microsoft.Network/publicIPAddresses/pip"

def azure_get(kind, group, name, *, expand, **options):
    assert expand is None and 0 < options["timeout"] <= 10
    assert (group, name) == {"vm": ("group", "vm"), "nic": ("net-group", "nic"),
                             "pip": ("ip-group", "pip")}[kind]
    reads.append(("azure", kind))
    return {
        "vm": SimpleNamespace(id=resource, network_profile=SimpleNamespace(
            network_interfaces=[SimpleNamespace(id=nic)])),
        "nic": SimpleNamespace(id=nic, ip_configurations=[SimpleNamespace(
            public_ip_address=SimpleNamespace(id=pip))]),
        "pip": SimpleNamespace(id=pip, properties=SimpleNamespace(ip_address="192.0.2.12")),
    }[kind]

def azure_compute(selected_config, selected_ctx):
    assert selected_config.subscription_id == "sub" and selected_ctx is ctx
    reads.append(("azure", "compute"))
    return SimpleNamespace(virtual_machines=SimpleNamespace(
        get=lambda *a, **kw: azure_get("vm", *a, **kw)), close=lambda: closed("azure-compute"))

def azure_network(selected_config, selected_ctx):
    assert selected_config.subscription_id == "sub" and selected_ctx is ctx
    reads.append(("azure", "network"))
    return SimpleNamespace(
        network_interfaces=SimpleNamespace(get=lambda *a, **kw: azure_get("nic", *a, **kw)),
        public_ip_addresses=SimpleNamespace(get=lambda *a, **kw: azure_get("pip", *a, **kw)),
        close=lambda: closed("azure-network"))

network = "https://www.googleapis.com/compute/v1/projects/project/global/networks/selected"
subnet = "https://www.googleapis.com/compute/v1/projects/project/regions/us-central1/subnetworks/selected"

def gce_get(*, project, zone, instance, retry, timeout):
    assert (project, zone, instance) == ("project", "us-central1-a", "vm")
    assert retry is None and 0 < timeout <= 10
    reads.append(("gcp", "instance"))
    return SimpleNamespace(id=123, network_interfaces=[SimpleNamespace(
        network=network, subnetwork=subnet, access_configs=[SimpleNamespace(
            name="selected-access", type_="ONE_TO_ONE_NAT", nat_i_p="192.0.2.13")])])

def gce_client(kind, selected_ctx):
    assert kind == "instances" and selected_ctx is ctx
    reads.append(("gcp", "client"))
    return SimpleNamespace(get=gce_get)

aws = VM_PLATFORM_REGISTRY["aws-ec2"]("fixture-aws-site", {
    "region": "us-east-1", "auth": {"mode": "ambient"}})
azure = VM_PLATFORM_REGISTRY["azure-vm"]("fixture-azure-site", {
    "subscription_id": "sub", "resource_group": "group", "region": "eastus"})
gcp = VM_PLATFORM_REGISTRY["gcp-gce"]("fixture-gcp-site", {
    "project_id": "project", "zone": "us-central1-a"})
clouds = [
    (aws, "192.0.2.11", {"instance_id": "i-0123456789abcdef0", "region": "us-east-1",
                         "account_id": "123456789012"}),
    (azure, "192.0.2.12", {"resource_id": resource}),
    (gcp, "192.0.2.13", {"project_id": "project", "zone": "us-central1-a", "instance_name": "vm",
                         "instance_id": "123", "network_url": network, "subnet_url": subnet,
                         "network_tag": "selected", "allow_rule": "allow", "allow_rule_id": "124",
                         "allow_source_ranges": "192.0.2.0/24", "deny_rule": "deny", "deny_rule_id": "125",
                         "access_config_name": "selected-access"}),
]
with ExitStack() as patches:
    patches.enter_context(patch.object(aws, "_get_session", ec2_session))
    patches.enter_context(patch.object(azure, "_native_compute_client", azure_compute))
    patches.enter_context(patch.object(azure, "_native_network_client", azure_network))
    patches.enter_context(patch.object(gcp._clients, "client", gce_client))
    for platform, endpoint, metadata in clouds:
        vm = SimpleNamespace(name="fixture-cloud", admin_username="fixture-admin",
                             platform_metadata=metadata)
        before = list(reads)
        try:
            platform.resolve_native_execution_binding(vm, RunContext(), deadline=deadline)
        except ConfigError as error:
            assert (error.entity_kind, error.entity_name) == ("vm", vm.name)
        else:
            raise AssertionError("binding accepted missing SSH policy")
        assert reads == before
        binding = platform.resolve_native_execution_binding(vm, ctx, deadline=deadline)
        assert isinstance(binding.carrier, SSHCarrier)
        connection = binding.carrier._connection
        assert (connection.host, connection.port, connection.user) == (endpoint, 22, "fixture-admin")
        assert connection.identity_file == settings.identity_file
        assert connection.trust == ManagedSSHTrust(settings.trust_store)
        assert connection.host_key_alias is None
        assert binding.delivery_account == "fixture-admin"
        assert binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)
        assert binding._independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
assert reads == [
    ("aws", "session"), ("aws", "client"), ("aws", "instance"), ("aws", "close"),
    ("azure", "compute"), ("azure", "vm"), ("azure-compute", "close"),
    ("azure", "network"), ("azure", "nic"), ("azure", "pip"), ("azure-network", "close"),
    ("gcp", "client"), ("gcp", "instance"),
]

# Real local selection reads private fixture configuration and directory identity.
# Only tool discovery and process output are controlled; no Lima guest is entered.
home = work / "lima-home"
directory = home / "selected"
directory.mkdir(parents=True)
(directory / "lima.yaml").write_text("vmType: qemu\n", encoding="utf-8")
lima_reads = []
lima_deadline = deadline

def lima_process(argv, *, io, deadline, custody, env):
    assert deadline is lima_deadline and custody.settled
    assert argv[:3] == ["/fixture/limactl", "list", "--format=json"]
    assert argv[3] == "--yq" and argv[-1] == "selected" and len(argv) == 6
    assert env["LIMA_HOME"] == str(home) and isinstance(io.output, SinkOutput)
    native = '"user"' in argv[4]
    lima_reads.append(native)
    observed = {"name": "selected", "status": "Running", "dir": str(directory),
                "vmType": "qemu", "hasErrors": False}
    if native:
        observed["user"] = "lima-resolved-user"
    data = json.dumps(observed).encode()
    assert io.output.stdout.try_write(memoryview(data)) == len(data)
    return ProcessResult(True, 0, 0, CapturedOutput(b"", complete=True),
                         CapturedOutput(b"", complete=True), None)

def lima_which(name):
    assert name == "limactl"
    return "/fixture/limactl"

lima_vm = SimpleNamespace(name="fixture-lima", admin_username="recorded-user",
                          platform_metadata={"instance_name": "selected"})
lima = VM_PLATFORM_REGISTRY["lima"]("fixture-lima-site", {})
with patch.dict(os.environ, {"LIMA_HOME": str(home)}), \
        patch.object(_lima_selection.shutil, "which", lima_which), \
        patch.object(_lima_selection, "run_process", lima_process):
    remote = VM_PLATFORM_REGISTRY["lima"]("fixture-remote-lima-site", {
        "placement": {"mode": "ssh", "host": "fixture@host"}})
    try:
        remote.resolve_native_execution_binding(lima_vm, RunContext(), deadline=deadline)
    except StateError:
        pass
    else:
        raise AssertionError("remote Lima accepted an unowned host route")
    assert lima_reads == []
    binding = lima.resolve_native_execution_binding(lima_vm, RunContext(), deadline=deadline)
assert isinstance(binding.carrier, LimaCarrier)
connection = binding.carrier.connection
assert (connection.instance, connection.limactl_executable, connection.lima_home) == (
    "selected", "/fixture/limactl", str(home))
assert connection.selected_directory.path == str(directory)
connection.selected_directory.check()
assert binding.delivery_account == "lima-resolved-user"
assert binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)
assert binding._independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
assert not any(name == root or name.startswith(root + ".") for name in sys.modules for root in retired)
assert_first_party_origins()
"""
    cli_root = Path(__file__).resolve().parents[2]
    _run_fresh_process(cli_root, script, first_import, str(tmp_path))
