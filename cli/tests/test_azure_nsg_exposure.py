"""Azure NSG exposure model: baseline deny, ephemeral scoped allows.

The public IP is permanent for the VM's lifetime (Azure is retiring
default outbound access, so removing the IP takes the VM offline).
Every NSG carries a permanent ``deny-all-inbound`` rule at priority 200
and NO standing allow: SSH happens through one ephemeral
``allow-ssh-transient`` rule at priority 100 (Azure's minimum, so it
always outranks the deny), scoped to the detected operator egress IP
plus the ``operator.ssh_allow_cidrs`` config extras. ``create`` opens
the bootstrap hole, ``post_tailscale_ready`` / ``secure_failed_vm``
close it, and ``transient_route`` pokes/removes it around each
native-transport session, converging legacy VMs on the way.

Azure is a real dependency in the test env, so the fakes are installed
by patching the SDK symbols the modules import function-locally
(``monkeypatch.setattr`` on the real modules), matching
test_azure_credential_caching.py. Egress detection is always stubbed:
no test hits the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.errors import ConfigError, ConnectivityError
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.network import (
    ALLOW_SSH_RULE_NAME,
    ALLOW_SSH_RULE_PRIORITY,
    DENY_ALL_INBOUND_RULE_NAME,
    DENY_ALL_INBOUND_RULE_PRIORITY,
    LEGACY_SSH_RULE_NAME,
)
from agentworks.plugins.azure.platform import AzureVMPlatform

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

_RESOURCE_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}
_DETECTED = "198.18.0.7"
_DETECTED_PREFIX = f"{_DETECTED}/32"


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with detection stubbed (never a live probe) and a
    clean per-process cache; individual tests override the stub to drive
    the failure branches."""
    monkeypatch.setattr(azure_network, "_egress_ip_cache", None)
    monkeypatch.setattr(azure_network, "detect_egress_ip", lambda: _DETECTED)


def _fail_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise OSError("no route to checkip")

    monkeypatch.setattr(azure_network, "detect_egress_ip", _boom)


def _fake_vm() -> Any:
    """A stand-in for a VMRow carrying just what the network ops read."""
    return SimpleNamespace(
        name="vm1",
        admin_username="agentworks",
        platform_metadata={"resource_id": _RESOURCE_ID},
    )


def _operator_config(allow_cidrs: list[str] | None = None) -> Any:
    """A config stand-in carrying just ``operator.ssh_allow_cidrs``, the
    only operator field the route path reads."""
    return SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=allow_cidrs or []))


class _Poller:
    """A begin_* long-running-operation stub: ``.result()`` yields a value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


class _FakeSecurityRules:
    """Recording stub for ``network.security_rules``; per-test error
    injection drives the 404-tolerance and removal-failure paths. The
    shared ``events`` list records creations and deletions in call
    order so ordering contracts (deny re-pin before allow poke) can be
    asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, str, Any]] = []
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None

    def begin_create_or_update(self, rg: str, nsg: str, rule_name: str, rule: object) -> _Poller:
        if self.create_error is not None:
            raise self.create_error
        self.events.append(("create", rg, nsg, rule_name, rule))
        return _Poller(rule)

    def begin_delete(self, rg: str, nsg: str, rule_name: str) -> _Poller:
        if self.delete_error is not None:
            raise self.delete_error
        self.events.append(("delete", rg, nsg, rule_name, None))
        return _Poller(None)

    def creates(self) -> list[tuple[str, str, str, Any]]:
        return [(e[1], e[2], e[3], e[4]) for e in self.events if e[0] == "create"]

    def deletes(self) -> list[tuple[str, str, str]]:
        return [(e[1], e[2], e[3]) for e in self.events if e[0] == "delete"]


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
    """Compute stub for the create path: no VM pre-exists (the collision
    check sees the get raise) and creates succeed."""

    def get(self, rg: str, name: str, **_kw: object) -> Any:
        from azure.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("no such VM")

    def begin_create_or_update(self, rg: str, name: str, vm: object) -> _Poller:
        return _Poller(SimpleNamespace(id=_RESOURCE_ID))


class _FakeVMsWithLocation(_FakeVMs):
    """Compute stub for the route path: location reads serve the heal's
    public-IP region lookup."""

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
    """Patch the Azure SDK symbols the plugin imports, returning the
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


def _assert_deny_shape(rule: Any) -> None:
    assert rule.name == DENY_ALL_INBOUND_RULE_NAME
    assert rule.priority == DENY_ALL_INBOUND_RULE_PRIORITY == 200
    assert rule.access == "Deny"
    assert rule.direction == "Inbound"
    assert rule.protocol == "*"
    assert rule.source_address_prefix == "*"
    assert rule.destination_address_prefix == "*"


def _assert_allow_shape(rule: Any, prefixes: list[str]) -> None:
    assert rule.name == ALLOW_SSH_RULE_NAME
    assert rule.priority == ALLOW_SSH_RULE_PRIORITY == 100
    assert rule.access == "Allow"
    assert rule.direction == "Inbound"
    assert rule.protocol == "Tcp"
    assert rule.destination_port_range == "22"
    assert rule.source_address_prefixes == prefixes
    assert rule.source_address_prefix is None


class TestCreate:
    def _request(self) -> ProvisionRequest:
        return ProvisionRequest(
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

    def test_create_provisions_deny_baseline_and_scoped_bootstrap_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``create`` builds the NSG with exactly two rules: the deny
        baseline at 200 and the scoped bootstrap allow at 100 carrying
        the detected prefix plus the config extras. No standing
        world-open SSH rule exists, and the per-rule ops are untouched
        (closing the hole is the hooks' job)."""
        network = _install_fakes(monkeypatch, vm_exists_lookup=False)

        result = _platform().create(
            self._request(),
            RunContext(config=_operator_config(["198.51.100.0/24"])),
        )

        assert result.platform_metadata == {"resource_id": _RESOURCE_ID}
        assert network.public_ip_addresses.created == [("rg1", "vm1-ip")]

        assert len(network.network_security_groups.created) == 1
        rg, nsg_name, nsg = network.network_security_groups.created[0]
        assert (rg, nsg_name) == ("rg1", "vm1-nsg")
        rules = {r.name: r for r in nsg.security_rules}
        assert set(rules) == {DENY_ALL_INBOUND_RULE_NAME, ALLOW_SSH_RULE_NAME}
        _assert_deny_shape(rules[DENY_ALL_INBOUND_RULE_NAME])
        _assert_allow_shape(rules[ALLOW_SSH_RULE_NAME], [_DETECTED_PREFIX, "198.51.100.0/24"])

        # No per-rule create/delete during create; the hooks own the close.
        assert network.security_rules.events == []

    def test_create_fails_typed_when_detection_fails_and_no_extras(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Detection failure with no configured extras is a typed error
        BEFORE any resource exists, hinting at operator.ssh_allow_cidrs."""
        network = _install_fakes(monkeypatch, vm_exists_lookup=False)
        _fail_detection(monkeypatch)

        with pytest.raises(ConnectivityError) as exc:
            _platform().create(self._request(), RunContext())

        assert "ssh_allow_cidrs" in (exc.value.hint or "")
        # Nothing was provisioned: the failure precedes all SDK calls.
        assert network.public_ip_addresses.created == []
        assert network.network_security_groups.created == []


class TestPrefixAssembly:
    def test_detected_plus_extras(self) -> None:
        prefixes = azure_network.operator_ssh_prefixes(["203.0.113.7", "198.51.100.0/24"])
        # Bare IP normalized to /32; detected prefix leads.
        assert prefixes == [_DETECTED_PREFIX, "203.0.113.7/32", "198.51.100.0/24"]

    def test_detected_duplicate_collapses(self) -> None:
        assert azure_network.operator_ssh_prefixes([_DETECTED]) == [_DETECTED_PREFIX]

    def test_invalid_extra_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not-an-ip"):
            azure_network.operator_ssh_prefixes(["not-an-ip"])

    def test_detection_failure_with_extras_proceeds_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        _fail_detection(monkeypatch)
        assert azure_network.operator_ssh_prefixes(["203.0.113.7"]) == ["203.0.113.7/32"]
        assert any("could not detect" in w and "ssh_allow_cidrs" in w for w in captured_output.warnings)

    def test_detection_failure_without_extras_is_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fail_detection(monkeypatch)
        with pytest.raises(ConnectivityError) as exc:
            azure_network.operator_ssh_prefixes([])
        assert "ssh_allow_cidrs" in (exc.value.hint or "")


class TestCloseProvisioningAccessHooks:
    def test_post_tailscale_ready_deletes_the_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The success hook deletes the bootstrap allow; the deny
        baseline is never touched (nothing to arm)."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().post_tailscale_ready(vm)

        assert network.security_rules.deletes() == [("rg1", "vm1-nsg", ALLOW_SSH_RULE_NAME)]
        assert network.security_rules.creates() == []

    def test_secure_failed_vm_deletes_the_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: the kept-FAILED hook removes the bootstrap allow
        too, so a failed VM defaults to zero inbound exposure."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().secure_failed_vm(vm)

        assert network.security_rules.deletes() == [("rg1", "vm1-nsg", ALLOW_SSH_RULE_NAME)]
        assert network.security_rules.creates() == []

    def test_hooks_tolerate_missing_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 on the delete (hook re-run, already-closed VM) is fine."""
        from azure.core.exceptions import ResourceNotFoundError

        network = _install_fakes(monkeypatch)
        network.security_rules.delete_error = ResourceNotFoundError("already gone")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().post_tailscale_ready(vm)
        _platform().secure_failed_vm(vm)  # no raise either


class TestTransientRoute:
    def test_enter_heals_missing_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A NIC with no public IP (a VM created under the old detach
        scheme) converges on enter: the IP is created (idempotent, same
        name as create's) and attached to the NIC."""
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

    def test_enter_converges_then_pokes_and_exit_removes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enter re-pins the deny to 200 BEFORE creating the allow at 100
        (Azure requires unique priorities per direction, so a legacy deny
        still at 100 would collide with the allow's slot), deletes the
        legacy standing SSH rule, then pokes the scoped allow. Exit
        deletes the allow; the deny is never deleted."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform().transient_route(vm, config=_operator_config(["198.51.100.0/24"])):
            # Call order: deny re-pin (create), legacy SSH delete, allow poke.
            kinds = [(e[0], e[3]) for e in network.security_rules.events]
            assert kinds == [
                ("create", DENY_ALL_INBOUND_RULE_NAME),
                ("delete", LEGACY_SSH_RULE_NAME),
                ("create", ALLOW_SSH_RULE_NAME),
            ]
            creates = network.security_rules.creates()
            _assert_deny_shape(creates[0][3])
            _assert_allow_shape(creates[1][3], [_DETECTED_PREFIX, "198.51.100.0/24"])

        # Exit removed the allow, and only the allow: the deny (and the
        # legacy rule, already gone) saw no further ops.
        assert network.security_rules.deletes() == [
            ("rg1", "vm1-nsg", LEGACY_SSH_RULE_NAME),
            ("rg1", "vm1-nsg", ALLOW_SSH_RULE_NAME),
        ]
        assert [e[3] for e in network.security_rules.events if e[0] == "create"] == [
            DENY_ALL_INBOUND_RULE_NAME,
            ALLOW_SSH_RULE_NAME,
        ]

    def test_convergence_tolerates_absent_legacy_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 on the legacy SSH delete (an already-converged or
        new-scheme VM) is expected; the enter proceeds to the poke."""
        from azure.core.exceptions import ResourceNotFoundError

        network = _install_fakes(monkeypatch)

        rules: _FakeSecurityRules = network.security_rules
        real_delete = rules.begin_delete

        def _delete(rg: str, nsg: str, rule_name: str) -> _Poller:
            if rule_name == LEGACY_SSH_RULE_NAME:
                raise ResourceNotFoundError("no legacy rule")
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(network.security_rules, "begin_delete", _delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        body_ran = False
        with _platform().transient_route(vm):
            body_ran = True

        assert body_ran
        assert [e[3] for e in network.security_rules.events if e[0] == "create"] == [
            DENY_ALL_INBOUND_RULE_NAME,
            ALLOW_SSH_RULE_NAME,
        ]

    def test_exit_removes_allow_when_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The allow removal is a finally: it fires however the caller
        unwinds."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="kaboom"), _platform().transient_route(vm):
            raise RuntimeError("kaboom")

        assert network.security_rules.deletes()[-1] == ("rg1", "vm1-nsg", ALLOW_SSH_RULE_NAME)

    def test_exit_removal_failure_warns_with_prefixes(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A failed exit removal must not raise out of the finally, but
        the warning states the actual residual exposure: the NSG and the
        exact prefixes that remain allowed (not the world)."""
        network = _install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        entered = False
        with _platform().transient_route(vm, config=_operator_config(["198.51.100.0/24"])):
            entered = True
            # Poison ONLY the exit path's delete.
            network.security_rules.delete_error = RuntimeError("boom")

        assert entered
        warning = next(w for w in captured_output.warnings if ALLOW_SSH_RULE_NAME in w)
        assert "vm1-nsg" in warning
        assert _DETECTED_PREFIX in warning
        assert "198.51.100.0/24" in warning


class TestEnsureDenyRaises:
    def test_converge_deny_failure_raises_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the deny baseline cannot be ensured, the enter fails loudly
        (a route without the baseline would be the old standing-exposure
        model by accident)."""
        from agentworks.plugins.azure.network import AzureError

        network = _install_fakes(monkeypatch)
        network.security_rules.create_error = RuntimeError("boom")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError), _platform().transient_route(vm):
            pytest.fail("the body must not run when convergence fails")
