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
binary) tears the full set down VM-first. Readiness ``SSHError`` now
becomes a typed failure in the shared bootstrap helper and takes the
same rollback path before the key is delivered.

Fakes come from ``tests._azure_platform_support`` (shared with
test_azure_nsg_exposure.py); egress detection is stubbed, no test hits
the network.
"""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure
from agentworks.capabilities.vm_platform.tailscale_join import EphemeralTailscaleBootstrap
from agentworks.debian import DebianRelease
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.ssh import SSHError
from agentworks.transports import SSHTransport
from tests._azure_platform_support import _install_fakes

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}}
_SENTINEL = "tskey-azure-readiness-'sentinel"


def _assert_exception_graph_is_value_free(failure: BaseException) -> None:
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert _SENTINEL not in repr(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create resolves the bootstrap-allow prefixes before any resource
    exists; stub detection so no test hits the network. Detection lives in
    the shared ssh_exposure home now (hoisted for aws reuse)."""
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")

    def _successful_bootstrap(self: SSHTransport, command: str, **kwargs: object) -> object:
        del self, kwargs
        return SimpleNamespace(stdout="100.64.0.5\n" if command == "tailscale ip -4" else "", returncode=0)

    monkeypatch.setattr(SSHTransport, "run", _successful_bootstrap)


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


def _request(*, tailscale: bool) -> ProvisionRequest:
    """Use the sentinel for reflection checks, otherwise a regular required key."""
    return ProvisionRequest(
        vm_name="vm1",
        debian_release=DebianRelease.TRIXIE,
        hostname="vm1",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=None,
        tailscale_auth_key=_SENTINEL if tailscale else "tskey-test",
        progress=MagicMock(),
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

    def _raise(self: EphemeralTailscaleBootstrap, auth_key: str) -> None:
        raise interrupt

    monkeypatch.setattr(EphemeralTailscaleBootstrap, "complete", _raise)
    return interrupt


class TestInterruptDuringInlineWait:
    @pytest.mark.parametrize(
        ("failure_command", "expected_commands"),
        [
            ("echo ok", ["echo ok"]),
            ("cloud-init status --wait", ["echo ok", "cloud-init status --wait"]),
        ],
        ids=("ssh-readiness", "cloud-init-wait"),
    )
    def test_rolls_back_the_full_resource_set_and_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        failure_command: str,
        expected_commands: list[str],
    ) -> None:
        """A real readiness-command interrupt rolls back the full resource
        set before fixed-stdin delivery can run."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        # A managed OS disk with the owner tag, as Azure names them.
        fakes.compute.disks.disks = [SimpleNamespace(name="vm1_OsDisk_1", tags={"owner": "agentworks"})]
        interrupt = KeyboardInterrupt(f"interrupted during {failure_command}")
        calls: list[tuple[str, dict[str, object]]] = []

        def _interrupt_readiness(self: SSHTransport, command: str, **kwargs: object) -> object:
            calls.append((command, kwargs))
            if command == failure_command:
                raise interrupt
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(SSHTransport, "run", _interrupt_readiness)
        with pytest.raises(KeyboardInterrupt) as exc:
            _platform().create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        assert [command for command, _kwargs in calls] == expected_commands
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        assert not any("agentworks-bootstrap" in command for command, _kwargs in calls)
        vm = fakes.compute.virtual_machines.created[0][2]
        retained_cloud_init = base64.b64decode(vm.os_profile.custom_data).decode()
        assert _SENTINEL not in retained_cloud_init
        assert _SENTINEL not in repr(calls)
        assert _SENTINEL not in repr(captured_output.lines)
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

    @pytest.mark.parametrize(
        ("failure_command", "expected_commands"),
        [
            ("echo ok", ["echo ok"]),
            ("cloud-init status --wait", ["echo ok", "cloud-init status --wait"]),
        ],
        ids=("ssh-readiness", "cloud-init-wait"),
    )
    def test_second_interrupt_abandons_cleanup_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        failure_command: str,
        expected_commands: list[str],
    ) -> None:
        """A readiness interrupt followed by cleanup interruption preserves
        the first interrupt and gives exact manual-removal coordinates."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        interrupt = KeyboardInterrupt(f"interrupted during {failure_command}")
        calls: list[tuple[str, dict[str, object]]] = []

        def _interrupt_readiness(self: SSHTransport, command: str, **kwargs: object) -> object:
            calls.append((command, kwargs))
            if command == failure_command:
                raise interrupt
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(SSHTransport, "run", _interrupt_readiness)
        fakes.compute.virtual_machines.delete_error = KeyboardInterrupt("second")

        with pytest.raises(KeyboardInterrupt) as exc:
            _platform().create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        assert [command for command, _kwargs in calls] == expected_commands
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        # Abandoned at the first cleanup step: nothing else was touched.
        assert fakes.network.network_interfaces.deleted == []
        assert fakes.network.public_ip_addresses.deleted == []
        abandoned = [warning for warning in captured_output.warnings if "Cleanup abandoned" in warning]
        assert abandoned == [
            "Cleanup abandoned: Azure resources named 'vm1*' may remain in resource group "
            "'rg1'; delete them there manually."
        ]

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
    instead of leaking a running VM. Readiness SSHError paths now raise
    typed too, keeping them in the same rollback window."""

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

    def test_ssh_readiness_exhaustion_fails_closed_without_key_delivery(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """SSH exhaustion raises before key delivery and removes every retained
        Azure resource, so Phase A cannot select generated-script staging."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        calls: list[tuple[str, dict[str, object]]] = []

        def _ssh_down(self: SSHTransport, command: str, **kw: object) -> object:
            calls.append((command, kw))
            raise SSHError("connect timed out")

        monkeypatch.setattr(SSHTransport, "run", _ssh_down)
        # The wait sleeps 10s between its 30 probes; don't.
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        with pytest.raises(AzureError, match="SSH did not become ready") as caught:
            _platform().create(_request(tailscale=True), RunContext())

        assert len(calls) == 30
        assert {command for command, _kwargs in calls} == {"echo ok"}
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        self._assert_safe_provider_failure(fakes, calls, caught.value, captured_output)

    def test_cloud_init_wait_failure_fails_closed_without_key_delivery(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        calls: list[tuple[str, dict[str, object]]] = []

        def _cloud_init_fails(self: SSHTransport, command: str, **kwargs: object) -> object:
            calls.append((command, kwargs))
            if command == "cloud-init status --wait":
                raise SSHError("cloud-init exited 1")
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(SSHTransport, "run", _cloud_init_fails)

        with pytest.raises(AzureError, match="cloud-init did not complete") as caught:
            _platform().create(_request(tailscale=True), RunContext())

        assert calls == [
            ("echo ok", {"check": True, "timeout": 10}),
            ("cloud-init status --wait", {"check": True, "timeout": 600}),
        ]
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        self._assert_safe_provider_failure(fakes, calls, caught.value, captured_output)

    @staticmethod
    def _assert_safe_provider_failure(
        fakes: Any,
        calls: list[tuple[str, dict[str, object]]],
        failure: AzureError,
        captured_output: CapturedOutput,
    ) -> None:
        azure = fakes
        vm = azure.compute.virtual_machines.created[0][2]
        retained_cloud_init = base64.b64decode(vm.os_profile.custom_data).decode()
        assert _SENTINEL not in retained_cloud_init
        assert _SENTINEL not in repr(calls)
        assert not any("agentworks-bootstrap" in command for command, _kwargs in calls)
        assert azure.compute.virtual_machines.deleted == [("rg1", "vm1")]
        assert azure.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert azure.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert azure.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert azure.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]
        _assert_exception_graph_is_value_free(failure)
        assert _SENTINEL not in repr(captured_output.lines)


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
