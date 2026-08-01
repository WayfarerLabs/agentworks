"""Azure NSG deny-all-inbound lifecycle: the public IP is permanent for
the VM's lifetime (Azure is retiring default outbound access, so
removing the IP takes the VM offline) and public exposure is controlled
by a deny-all-inbound NSG rule instead. ``post_tailscale_ready`` arms
the rule, ``transient_route`` heals a missing public IP and lifts the
rule on enter and re-arms it on exit, and ``create`` provisions the
permanent IP plus the SSH-allow NSG but never the deny rule.

Azure is a real dependency in the test env, so the fakes are installed
by patching the SDK symbols the platform module imports function-locally
(``monkeypatch.setattr`` on the real modules), matching
test_azure_credential_caching.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.plugins.azure.platform import (
    DENY_ALL_INBOUND_RULE_NAME,
    DENY_ALL_INBOUND_RULE_PRIORITY,
    AzureVMPlatform,
)

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

_RESOURCE_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}


def _fake_vm() -> Any:
    """A stand-in for a VMRow carrying just what the network ops read."""
    return SimpleNamespace(
        name="vm1",
        admin_username="agentworks",
        platform_metadata={"resource_id": _RESOURCE_ID},
    )


class _Poller:
    """A begin_* long-running-operation stub: ``.result()`` yields a value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


class _FakeSecurityRules:
    """Recording stub for ``network.security_rules``; per-test error
    injection drives the 404-tolerance and re-arm-failure paths."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, Any]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None

    def begin_create_or_update(self, rg: str, nsg: str, rule_name: str, rule: object) -> _Poller:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((rg, nsg, rule_name, rule))
        return _Poller(rule)

    def begin_delete(self, rg: str, nsg: str, rule_name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((rg, nsg, rule_name))
        return _Poller(None)


class _FakePublicIps:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def begin_create_or_update(self, rg: str, name: str, params: object) -> _Poller:
        self.created.append((rg, name))
        return _Poller(SimpleNamespace(ip_address="203.0.113.5", id="/pip/id"))


class _FakeNics:
    """One NIC whose single ip-configuration either carries a public IP
    reference (the permanent-IP steady state) or lacks one (a VM created
    under the old detach scheme, awaiting the heal)."""

    def __init__(self, *, public_ip_attached: bool) -> None:
        self.updated: list[tuple[str, str, Any]] = []
        pip = SimpleNamespace(id="/pip/id") if public_ip_attached else None
        self.nic = SimpleNamespace(ip_configurations=[SimpleNamespace(public_ip_address=pip)])

    def get(self, rg: str, name: str) -> Any:
        return self.nic

    def begin_create_or_update(self, rg: str, name: str, nic: object) -> _Poller:
        self.updated.append((rg, name, nic))
        return _Poller(SimpleNamespace(id="/nic/id"))


class _FakeNsgs:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, Any]] = []

    def begin_create_or_update(self, rg: str, name: str, nsg: object) -> _Poller:
        self.created.append((rg, name, nsg))
        return _Poller(SimpleNamespace(id="/nsg/id"))


class _FakeVnets:
    def begin_create_or_update(self, rg: str, name: str, vnet: object) -> _Poller:
        return _Poller(SimpleNamespace(subnets=[SimpleNamespace(id="/subnet/id")]))


class _FakeVMs:
    """Compute stub: no VM pre-exists (the ``create`` collision check
    sees the get raise), creates succeed, and location reads serve the
    heal's public-IP region lookup."""

    def get(self, rg: str, name: str, **_kw: object) -> Any:
        from azure.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("no such VM")

    def begin_create_or_update(self, rg: str, name: str, vm: object) -> _Poller:
        return _Poller(SimpleNamespace(id=_RESOURCE_ID))


class _FakeVMsWithLocation(_FakeVMs):
    def get(self, rg: str, name: str, **_kw: object) -> Any:
        return SimpleNamespace(location="eastus")


class _FakeNetwork(SimpleNamespace):
    pass


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    public_ip_attached: bool = True,
    vm_exists_lookup: bool = True,
) -> _FakeNetwork:
    """Patch the Azure SDK symbols the platform imports, returning the
    fake network client holder the tests configure and assert on.
    ``vm_exists_lookup`` picks the compute stub: location reads for the
    heal (True) vs. the create path's not-found collision check (False).
    """

    network = _FakeNetwork(
        public_ip_addresses=_FakePublicIps(),
        network_interfaces=_FakeNics(public_ip_attached=public_ip_attached),
        network_security_groups=_FakeNsgs(),
        virtual_networks=_FakeVnets(),
        security_rules=_FakeSecurityRules(),
    )

    class _FakeDefaultCred:
        def get_token(self, *_scopes: str, **_kw: object) -> object:
            return SimpleNamespace(token="tok", expires_on=0)

    class _FakeComputeClient:
        def __init__(self, credential: object, subscription_id: str) -> None:
            self.virtual_machines = _FakeVMsWithLocation() if vm_exists_lookup else _FakeVMs()

    class _FakeNetworkClient:
        def __init__(self, credential: object, subscription_id: str) -> None:
            self.public_ip_addresses = network.public_ip_addresses
            self.network_interfaces = network.network_interfaces
            self.network_security_groups = network.network_security_groups
            self.virtual_networks = network.virtual_networks
            self.security_rules = network.security_rules

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _FakeDefaultCred)
    monkeypatch.setattr("azure.mgmt.compute.ComputeManagementClient", _FakeComputeClient)
    monkeypatch.setattr("azure.mgmt.network.NetworkManagementClient", _FakeNetworkClient)
    return network


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


class TestPostTailscaleReady:
    def test_arms_deny_rule_with_pinned_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hook creates the deny-all-inbound rule on the VM's NSG:
        the pinned shape (name, priority 100 outranking SSH's 1000,
        Deny, Inbound, wildcard protocol/ports/prefixes) is the security
        contract, so every field is asserted."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().post_tailscale_ready(vm)

        assert len(network.security_rules.created) == 1
        rg, nsg, rule_name, rule = network.security_rules.created[0]
        assert (rg, nsg, rule_name) == ("rg1", "vm1-nsg", DENY_ALL_INBOUND_RULE_NAME)
        assert rule.name == DENY_ALL_INBOUND_RULE_NAME
        assert rule.priority == DENY_ALL_INBOUND_RULE_PRIORITY == 100
        assert rule.access == "Deny"
        assert rule.direction == "Inbound"
        assert rule.protocol == "*"
        assert rule.source_port_range == "*"
        assert rule.destination_port_range == "*"
        assert rule.source_address_prefix == "*"
        assert rule.destination_address_prefix == "*"

    def test_arm_failure_warns_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A failed arming warns (naming the NSG and the exposure)
        rather than raising: the create flow treats the hook as
        non-fatal, and silence would hide an internet-exposed VM."""
        network = _install_fakes(monkeypatch)
        network.security_rules.create_error = RuntimeError("boom")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().post_tailscale_ready(vm)

        assert any("vm1-nsg" in w and "exposed" in w for w in captured_output.warnings)


class TestTransientRoute:
    def test_enter_heals_missing_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A NIC with no public IP (a VM created under the old detach
        scheme) converges on enter: the IP is created (idempotent, same
        name as create's) and attached to the NIC before the rule lift."""
        network = _install_fakes(monkeypatch, public_ip_attached=False)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform().transient_route(vm):
            assert network.public_ip_addresses.created == [("rg1", "vm1-ip")]
            assert len(network.network_interfaces.updated) == 1
            nic = network.network_interfaces.updated[0][2]
            assert nic.ip_configurations[0].public_ip_address.id == "/pip/id"

    def test_enter_skips_heal_when_public_ip_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The steady state (permanent IP already on the NIC) is a
        single NIC read: no IP create, no NIC update."""
        network = _install_fakes(monkeypatch, public_ip_attached=True)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform().transient_route(vm):
            assert network.public_ip_addresses.created == []
            assert network.network_interfaces.updated == []

    def test_enter_deletes_deny_rule_and_exit_recreates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enter lifts the deny rule (delete), exit re-arms it (create):
        the exposure is bounded by the context."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform().transient_route(vm):
            assert network.security_rules.deleted == [("rg1", "vm1-nsg", DENY_ALL_INBOUND_RULE_NAME)]
            assert network.security_rules.created == []

        assert len(network.security_rules.created) == 1
        assert network.security_rules.created[0][:3] == ("rg1", "vm1-nsg", DENY_ALL_INBOUND_RULE_NAME)

    def test_enter_tolerates_missing_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 on the rule delete (legacy VM, or the pre-Tailscale
        window before the rule was first armed) is expected: the body
        still runs and the exit re-arm converges the VM onto the new
        scheme."""
        from azure.core.exceptions import ResourceNotFoundError

        network = _install_fakes(monkeypatch)
        network.security_rules.delete_error = ResourceNotFoundError("rule absent")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        body_ran = False
        with _platform().transient_route(vm):
            body_ran = True

        assert body_ran
        assert len(network.security_rules.created) == 1

    def test_enter_warns_on_non_404_lift_failure_and_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A non-404 failure on the rule delete warns (naming the NSG)
        and proceeds: if the rule is genuinely still armed, the caller's
        transport attempt surfaces the unreachability, and the exit
        re-arm still fires."""
        network = _install_fakes(monkeypatch)
        network.security_rules.delete_error = RuntimeError("boom")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        body_ran = False
        with _platform().transient_route(vm):
            body_ran = True

        assert body_ran
        assert any("vm1-nsg" in w and "lift" in w for w in captured_output.warnings)
        assert len(network.security_rules.created) == 1

    def test_exit_rearms_when_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The re-arm is a finally: it fires however the caller unwinds."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="kaboom"), _platform().transient_route(vm):
            raise RuntimeError("kaboom")

        assert len(network.security_rules.created) == 1
        assert network.security_rules.created[0][:3] == ("rg1", "vm1-nsg", DENY_ALL_INBOUND_RULE_NAME)

    def test_exit_rearm_failure_warns_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A failed exit re-arm must not raise out of the finally (the
        caller is unwinding), but it must not be silent either: the
        warning names the NSG and says SSH remains exposed."""
        network = _install_fakes(monkeypatch)
        network.security_rules.create_error = RuntimeError("boom")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform().transient_route(vm):
            pass

        assert any("vm1-nsg" in w and "exposed" in w for w in captured_output.warnings)


class TestCreateProvisionsPermanentIp:
    def test_create_provisions_ip_and_ssh_nsg_without_deny_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``create`` provisions the Static public IP and the NSG with
        the SSH-allow rule at priority 1000, exactly as before, and never
        creates the deny rule itself: arming is ``post_tailscale_ready``'s
        job, after Tailscale is confirmed."""
        network = _install_fakes(monkeypatch, vm_exists_lookup=False)

        request = ProvisionRequest(
            vm_name="vm1",
            hostname="vm1",
            system_slug=None,
            admin_username="agentworks",
            ssh_public_key="ssh-ed25519 AAAA test",
            ssh_private_key=None,
            # No Tailscale key: create skips the inline bootstrap wait,
            # keeping the test hermetic (no SSH).
            tailscale_auth_key=None,
        )
        result = _platform().create(request, RunContext())

        assert result.platform_metadata == {"resource_id": _RESOURCE_ID}
        assert network.public_ip_addresses.created == [("rg1", "vm1-ip")]

        assert len(network.network_security_groups.created) == 1
        rg, nsg_name, nsg = network.network_security_groups.created[0]
        assert (rg, nsg_name) == ("rg1", "vm1-nsg")
        rules = nsg.security_rules
        assert [r.name for r in rules] == ["SSH"]
        assert rules[0].priority == 1000
        assert rules[0].access == "Allow"
        assert rules[0].destination_port_range == "22"

        # The deny rule is never created (or deleted) during create.
        assert network.security_rules.created == []
        assert network.security_rules.deleted == []
