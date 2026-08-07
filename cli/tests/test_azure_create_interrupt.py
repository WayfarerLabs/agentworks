"""Azure ``create`` rollback arms (#338, #347).

``create`` provisions the whole resource set (public IP, NSG, vnet,
NIC, VM, disk) and then waits inline for the cloud-init bootstrap, a
minutes-long window where a Ctrl-C is likeliest. The caller
(``create_vm``) deletes only the DB row on an interrupt or failure, so
any resource the platform leaves behind would be orphaned with nothing
left to target it. ``create`` therefore rolls back on
``KeyboardInterrupt`` across the whole span (resource creation AND the
inline wait) and re-raises the interrupt; a SECOND interrupt during
the cleanup abandons it loudly (naming the resource group and name
prefix) instead of wedging. The failure side matches that span with
two Exception arms (#347): a mid-creation failure runs the name-based
cleanup and raises the wrapped error (the pre-#338 arm, unchanged),
and a failure escaping the post-creation span (transport construction
plus the inline wait, e.g. a raw OSError from a missing local ssh
binary) tears the full set down VM-first; only ``SSHError``, absorbed
inside ``_wait_for_bootstrap`` as the tolerated defer-to-Phase-A path,
never rolls back.

Fakes come from ``tests._azure_platform_support`` (shared with
test_azure_nsg_exposure.py); egress detection is stubbed, no test hits
the network.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.ssh import SSHError
from agentworks.transports import SSHTransport
from tests._azure_platform_support import _install_fakes

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create resolves the bootstrap-allow prefixes before any resource
    exists; stub detection so no test hits the network. Detection lives in
    the shared ssh_exposure home now (hoisted for aws reuse)."""
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")


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
        # The vm-template layer's resolved defaults, which is the only
        # shape a platform ever sees (the hardware fields are required).
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
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

    def test_vm_delete_failure_during_rollback_warns_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A plain VM-delete failure inside the rollback (an Exception,
        distinct from the second-interrupt case above) warns that the
        VM may remain, naming the VM, resource group, and cause for
        manual cleanup; the sweep still collects the stragglers and the
        ORIGINAL interrupt propagates. This is #329's one sanctioned
        warn: the path is already unwinding on the operator's
        interrupt, so a raise would replace it, but a silent orphan is
        never acceptable."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        interrupt = _interrupt_the_wait(monkeypatch)
        fakes.compute.virtual_machines.delete_error = RuntimeError("vm delete boom")

        with pytest.raises(KeyboardInterrupt) as exc:
            _platform().create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (may_remain,) = [w for w in captured_output.warnings if "may remain" in w]
        assert "Azure VM 'vm1'" in may_remain
        assert "resource group 'rg1'" in may_remain
        assert "vm delete boom" in may_remain
        assert "delete it there manually" in may_remain
        # The cleanup was NOT abandoned: the sweep ran past the failed
        # VM delete and the stragglers stayed collectable.
        assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert not any("Cleanup abandoned" in w for w in captured_output.warnings)


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


class TestFailureDuringInlineWait:
    """#347: the failure arm spans the transport construction and the
    inline bootstrap wait, so a non-SSHError escaping the wait rolls the
    full resource set back (closing the bootstrap ingress with it)
    instead of leaking a running VM; the SSHError paths the wait absorbs
    itself stay tolerated (defer to Phase A, no rollback)."""

    def test_non_ssh_error_escaping_the_wait_rolls_back_vm_first_and_wraps(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The issue's scenario: no local ssh binary, so the wait's first
        probe raises a raw FileNotFoundError that SSHTransport.run does
        not wrap as SSHError. Every resource exists at that point, so the
        rollback deletes the VM first (it holds the NIC and disk) and
        then the name-based set, and the error re-raises wrapped, the
        Exception arms' convention."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.compute.disks.disks = [SimpleNamespace(name="vm1_OsDisk_1", tags={"owner": "agentworks"})]

        def _no_ssh_binary(self: SSHTransport, command: str, **_kw: object) -> object:
            raise FileNotFoundError("No such file or directory: 'ssh'")

        monkeypatch.setattr(SSHTransport, "run", _no_ssh_binary)

        with pytest.raises(AzureError, match="ssh"):
            _platform().create(_request(tailscale=True), RunContext())

        assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]
        assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
        assert fakes.compute.disks.deleted == [("rg1", "vm1_OsDisk_1")]
        # VM-first ordering, same contract as the interrupt arm.
        kinds = [kind for op, kind, _rg, _name in fakes.events if op == "delete"]
        assert kinds[0] == "vm"
        assert set(kinds[1:]) == {"nic", "ip", "nsg", "vnet", "disk"}
        # The failure arm, not the interrupt arm: no interrupt messaging.
        assert not any("Interrupted" in w for w in captured_output.warnings)
        assert not any("Cleanup abandoned" in w for w in captured_output.warnings)

    def test_tolerated_ssh_unavailability_defers_to_phase_a_without_rollback(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The wait's own SSHError absorption is NOT a rollback trigger:
        an SSH connection that never comes up exhausts the probe retries,
        warns, and create still returns a ProvisionResult with
        bootstrap_complete=False (Phase A retries), every resource kept."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)

        def _ssh_down(self: SSHTransport, command: str, **_kw: object) -> object:
            raise SSHError("connect timed out")

        monkeypatch.setattr(SSHTransport, "run", _ssh_down)
        # The wait sleeps 10s between its 30 probes; don't.
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        result = _platform().create(_request(tailscale=True), RunContext())

        assert result.bootstrap_complete is False
        assert result.tailscale_ip is None
        assert fakes.compute.virtual_machines.deleted == []
        assert fakes.network.network_interfaces.deleted == []
        assert fakes.network.network_security_groups.deleted == []
        assert any("deferring bootstrap to Phase A" in w for w in captured_output.warnings)


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


class TestVMDeleteFailureWarns:
    """#347's related minor under the #329 model: the teardown CAPTURES
    a VM-delete failure rather than warning in place (its callers own
    the messaging: the delete op raises via verify_vm_deleted, the
    rollback arms warn "may remain"), so the failure-arm rollback must
    surface the survivor itself, and the sweep stays best-effort."""

    def test_vm_delete_failure_during_failure_rollback_warns_and_sweep_continues(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.compute.virtual_machines.delete_error = RuntimeError("AuthorizationFailed: client lacks permission")

        def _wait_explodes(self: SSHTransport, command: str, **_kw: object) -> object:
            raise OSError("wait exploded")

        monkeypatch.setattr(SSHTransport, "run", _wait_explodes)

        with pytest.raises(AzureError, match="wait exploded"):
            _platform().create(_request(tailscale=True), RunContext())

        (warning,) = [w for w in captured_output.warnings if "may remain" in w]
        assert "'vm1'" in warning
        assert "AuthorizationFailed" in warning
        assert "'rg1'" in warning
        assert "manually" in warning
        # Still best-effort: the name-based sweep ran regardless.
        assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
