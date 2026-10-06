"""Shipped registration and passive native bindings do not need retired execution."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.windows
@pytest.mark.parametrize("first_import", ["agentworks.plugins", "agentworks.capabilities.vm_platform.registry"])
def test_shipped_registration_and_native_bindings_without_retired_modules(first_import: str) -> None:
    script = r"""
import importlib
import importlib.abc
import sys
from types import SimpleNamespace

roots = (
    "agentworks.transports", "agentworks.ssh", "agentworks.remote_exec",
    "agentworks.harness_setup.runner", "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
)
assert not any(name == root or name.startswith(root + ".")
               for name in sys.modules for root in roots)

class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + ".") for root in roots):
            raise AssertionError("retired execution imported: " + fullname)

def forbid_effects(event, args):
    if event in ("subprocess.Popen", "os.system", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError("passive binding performed I/O: " + event)

sys.meta_path.insert(0, Guard())
sys.addaudithook(forbid_effects)
importlib.import_module(sys.argv[1])
import agentworks.plugins as plugins
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.registry import VM_PLATFORM_REGISTRY
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.carriers.wsl2 import WSL2Carrier

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
assert not any(name == root or name.startswith(root + ".")
               for name in sys.modules for root in roots)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, first_import],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
