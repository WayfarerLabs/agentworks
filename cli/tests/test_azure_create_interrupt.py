"""Azure ``create`` interrupt rollback (#338).

``create`` provisions the whole resource set (public IP, NSG, vnet,
NIC, VM, disk) and then waits inline for the cloud-init bootstrap, a
minutes-long window where a Ctrl-C is likeliest. The caller
(``create_vm``) deletes only the DB row on an interrupt, so any
resource the platform leaves behind would be orphaned with nothing
left to target it. ``create`` therefore rolls back on
``KeyboardInterrupt`` across the whole span (resource creation AND the
inline wait) and re-raises the interrupt; a SECOND interrupt during
the cleanup abandons it loudly (naming the resource group and name
prefix) instead of wedging. The plain-failure arm (``Exception``) is
unchanged: name-based cleanup, wrapped error.

Fakes come from ``tests._azure_platform_support`` (shared with
test_azure_nsg_exposure.py); egress detection is stubbed, no test hits
the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from tests._azure_platform_support import _install_fakes

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create resolves the bootstrap-allow prefixes before any resource
    exists; stub detection so no test hits the network."""
    monkeypatch.setattr(azure_network, "_egress_ip_cache", None)
    monkeypatch.setattr(azure_network, "detect_egress_ip", lambda: "198.18.0.7")


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


def _request(*, tailscale: bool) -> ProvisionRequest:
    """With a Tailscale key create runs the inline bootstrap wait;
    without one it returns straight after the resources are up."""
    return ProvisionRequest(
        vm_name="vm1",
        hostname="vm1",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=None,
        tailscale_auth_key="tskey-test" if tailscale else None,
    )


def _interrupt_the_wait(monkeypatch: pytest.MonkeyPatch) -> KeyboardInterrupt:
    """Make the inline bootstrap wait raise, returning the instance so
    tests can assert the ORIGINAL interrupt is what propagates."""
    interrupt = KeyboardInterrupt("first")

    def _raise(self: AzureVMPlatform, target: object, vm_name: str) -> str | None:
        raise interrupt

    monkeypatch.setattr(AzureVMPlatform, "_wait_for_bootstrap", _raise)
    return interrupt


class TestInterruptDuringInlineWait:
    def test_rolls_back_the_full_resource_set_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The issue's scenario: every resource exists when the wait is
        interrupted, so the rollback deletes the VM first (it holds the
        NIC and disk) and then the name-based set, and the interrupt
        propagates for the caller's row unwind."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        # A managed OS disk with the owner tag, as Azure names them.
        fakes.compute.disks.disks = [SimpleNamespace(name="vm1_OsDisk_1", tags={"owner": "agentworks"})]
        interrupt = _interrupt_the_wait(monkeypatch)

        with pytest.raises(KeyboardInterrupt) as exc:
            _platform().create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]
        assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
        assert fakes.compute.disks.deleted == [("rg1", "vm1_OsDisk_1")]
        # The ordering contract, pinned via the shared sequence log: the
        # VM delete PRECEDES every sweep delete (the VM holds the NIC
        # and managed disk; Azure refuses to delete them while attached).
        kinds = [kind for op, kind, _rg, _name in fakes.events if op == "delete"]
        assert kinds[0] == "vm"
        assert set(kinds[1:]) == {"nic", "ip", "nsg", "vnet", "disk"}
        assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)

    def test_second_interrupt_abandons_cleanup_loudly(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A second Ctrl-C during the cleanup abandons it instead of
        wedging: no further deletes are attempted, the warning names the
        resource group and name prefix for manual cleanup, and the
        ORIGINAL interrupt still propagates."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        interrupt = _interrupt_the_wait(monkeypatch)
        fakes.compute.virtual_machines.delete_error = KeyboardInterrupt("second")

        with pytest.raises(KeyboardInterrupt) as exc:
            _platform().create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        # Abandoned at the first cleanup step: nothing else was touched.
        assert fakes.network.network_interfaces.deleted == []
        assert fakes.network.public_ip_addresses.deleted == []
        (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
        assert "'vm1*'" in abandoned
        assert "resource group 'rg1'" in abandoned


class TestInterruptDuringResourceCreation:
    def test_rolls_back_what_exists_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A Ctrl-C mid-creation (here: the NIC call) escapes the
        Exception arm by design; the interrupt arm still tears down the
        already-created resources, best-effort over the full named set."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.network.network_interfaces.create_error = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            _platform().create(_request(tailscale=False), RunContext())

        # The best-effort sweep attempts the whole set, including the
        # VM-first delete (a no-op server-side when it never existed).
        assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
        assert any("cleaning up partial Azure resources" in w for w in captured_output.warnings)


class TestPlainFailureArmUnchanged:
    def test_failure_still_cleans_up_and_wraps(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The pre-#338 Exception arm, byte-for-byte behavior: a backend
        failure runs the name-based cleanup (no VM-first delete step) and
        raises the wrapped Azure error; the interrupt messaging never
        appears."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.network.network_interfaces.create_error = RuntimeError("nic exploded")

        with pytest.raises(AzureError, match="nic exploded"):
            _platform().create(_request(tailscale=False), RunContext())

        assert fakes.compute.virtual_machines.deleted == []
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
        assert not any("Interrupted" in w for w in captured_output.warnings)
        assert not any("Cleanup abandoned" in w for w in captured_output.warnings)
