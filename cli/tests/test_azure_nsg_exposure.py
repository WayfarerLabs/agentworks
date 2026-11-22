"""Azure NSG exposure model: baseline deny, ephemeral scoped allows.

The public IP is permanent for the VM's lifetime (Azure is retiring
default outbound access, so removing the IP takes the VM offline).
Every NSG carries a permanent ``deny-all-inbound`` rule at priority 200
and NO standing allow: SSH happens through ephemeral allows in the band
[100, 199] (always outranking the deny), scoped to the detected operator
egress IP plus the ``operator.ssh_allow_cidrs`` config extras.
``create`` opens the fixed-name bootstrap hole,
``close_provisioning_access`` closes it, and
``_transient_route`` pokes/removes a per-operation nonce-named rule
around each root script or native shell (concurrent ops each own theirs),
converging legacy VMs on the way.

Azure is a real dependency in the test env, so the fakes are installed
by patching the SDK symbols the modules import function-locally
(``monkeypatch.setattr`` on the real modules), matching
test_azure_credential_caching.py; the fakes themselves live in
``tests._azure_platform_support`` (shared with
test_azure_create_interrupt.py). Egress detection is always stubbed:
no test hits the network. The route's lifecycle and the poke's own
cleanup are tested in ``test_azure_transient_route.py``, and the bounds
on its calls in ``test_azure_route_wall_bound.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError, ConnectivityError, ProvisioningError
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.errors import AzureError
from agentworks.plugins.azure.network import (
    ALLOW_PRIORITY_BAND_END,
    ALLOW_PRIORITY_BAND_START,
    ALLOW_RULE_DESCRIPTION_MARKER,
    BOOTSTRAP_ALLOW_RULE_NAME,
    DENY_ALL_INBOUND_RULE_NAME,
    TRANSIENT_ALLOW_RULE_PREFIX,
)
from tests._azure_platform_support import (
    _DETECTED,
    _DETECTED_PREFIX,
    _RESOURCE_ID,
    _assert_allow_shape,
    _assert_deny_shape,
    _fail_detection,
    _fake_vm,
    _FakeSecurityRules,
    _install_fakes,
    _operator_config,
    _platform,
    _Poller,
)
from tests._azure_platform_support import _stub_egress_detection as _stub_egress_detection
from tests.conftest import provisioning_trust_for_tests

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

# The real detector, captured at import time so TestDetectEgressIp can
# exercise it even though the autouse fixture stubs the module attribute
# for every test. Detection and its cache now live in the shared
# ssh_exposure home (hoisted for aws reuse); the stubs target it there,
# since operator_ssh_prefixes resolves the detector in that namespace.
_REAL_DETECT_EGRESS_IP = ssh_exposure.detect_egress_ip


def test_public_ip_lookup_reads_nested_sdk_response() -> None:
    from azure.mgmt.network.models import (
        NetworkInterface,
        NetworkInterfaceIPConfiguration,
        NetworkInterfaceIPConfigurationPropertiesFormat,
        NetworkInterfacePropertiesFormat,
        PublicIPAddress,
        PublicIPAddressPropertiesFormat,
    )

    public_ip_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/vm1-ip"
    network: Any = SimpleNamespace(
        network_interfaces=SimpleNamespace(
            get=lambda resource_group, name, **_options: NetworkInterface(
                properties=NetworkInterfacePropertiesFormat(
                    ip_configurations=[
                        NetworkInterfaceIPConfiguration(
                            properties=NetworkInterfaceIPConfigurationPropertiesFormat(
                                public_ip_address=PublicIPAddress(id=public_ip_id)
                            )
                        )
                    ]
                )
            )
        ),
        public_ip_addresses=SimpleNamespace(
            get=lambda resource_group, name, **_options: PublicIPAddress(
                properties=PublicIPAddressPropertiesFormat(ip_address="203.0.113.10")
            )
        ),
    )
    vm_info = SimpleNamespace(
        network_profile=SimpleNamespace(
            network_interfaces=[
                SimpleNamespace(
                    id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/vm1-nic"
                )
            ]
        )
    )
    compute: Any = SimpleNamespace(
        virtual_machines=SimpleNamespace(get=lambda resource_group, name, **_options: vm_info)
    )

    assert azure_network.get_vm_public_ip(compute, network, "rg", "vm1") == "203.0.113.10"


class TestCreate:
    def _request(self) -> ProvisionRequest:
        return ProvisionRequest(
            vm_name="vm1",
            debian_release=DebianRelease.TRIXIE,
            hostname="vm1",
            system_slug=None,
            admin_username="agentworks",
            ssh_public_key="ssh-ed25519 AAAA test",
            ssh_private_key=Path("/dev/null"),
            provisioning_trust=provisioning_trust_for_tests("vm1"),
            tailscale_auth_key="tskey-test",
            progress=MagicMock(),
            # The vm-template layer's resolved defaults, which is the only
            # shape a platform ever sees (the hardware fields are required).
            cpus=4,
            memory_gib=8,
            disk_gib=50,
            swap_gib=4,
        )

    def test_create_provisions_deny_baseline_and_scoped_bootstrap_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``create`` builds the NSG with exactly two rules: the deny
        baseline at 200 and the FIXED-NAME scoped bootstrap allow at the
        band start (the fresh NSG makes slot 100 free by construction)
        carrying the detected prefix plus the config extras. No standing
        world-open SSH rule exists, and the per-rule ops are untouched
        (closing the hole is the hooks' job)."""
        network = _install_fakes(monkeypatch, vm_exists_lookup=False).network

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
        assert set(rules) == {DENY_ALL_INBOUND_RULE_NAME, BOOTSTRAP_ALLOW_RULE_NAME}
        _assert_deny_shape(rules[DENY_ALL_INBOUND_RULE_NAME])
        _assert_allow_shape(
            rules[BOOTSTRAP_ALLOW_RULE_NAME],
            [_DETECTED_PREFIX, "198.51.100.0/24"],
            name=BOOTSTRAP_ALLOW_RULE_NAME,
            priority=ALLOW_PRIORITY_BAND_START,
        )

        # No per-rule create/delete during create; the hooks own the close.
        assert network.security_rules.events == []

    def test_create_announces_the_bootstrap_route_open(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The NSG create is what opens the bootstrap SSH route, so
        ``create`` announces it with the transient poke's wording, naming
        the scoped prefixes (#350: the close hooks already announced
        their side via ``remove_ssh_allow``; the open was silent)."""
        _install_fakes(monkeypatch, vm_exists_lookup=False)

        _platform().create(
            self._request(),
            RunContext(config=_operator_config(["198.51.100.0/24"])),
        )

        assert f"Opening SSH route (allow scoped to {_DETECTED_PREFIX}, 198.51.100.0/24)..." in captured_output.info

    def test_create_fails_typed_when_detection_fails_and_no_extras(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Detection failure with no configured extras is a typed error
        BEFORE any resource exists, hinting at operator.ssh_allow_cidrs."""
        network = _install_fakes(monkeypatch, vm_exists_lookup=False).network
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
    def test_the_close_deletes_the_bootstrap_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hook deletes the fixed-name bootstrap allow (no in-process
        state needed); the deny baseline is permanent, so there is nothing
        to restore."""
        network = _install_fakes(monkeypatch).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().close_provisioning_access(vm, RunContext())

        assert network.security_rules.deletes() == [("rg1", "vm1-nsg", BOOTSTRAP_ALLOW_RULE_NAME)]
        assert network.security_rules.creates() == []

    def test_the_close_tolerates_a_missing_allow_silently(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: Any
    ) -> None:
        """A 404 on the delete is fine: every root-channel route closes the provisioning access again."""
        from azure.core.exceptions import ResourceNotFoundError

        network = _install_fakes(monkeypatch).network
        network.security_rules.delete_error = ResourceNotFoundError("already gone")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().close_provisioning_access(vm, RunContext())  # no raise

        assert captured_output.info == []
        assert captured_output.warnings == []

    def test_a_close_whose_removal_does_not_finish_in_time_warns_instead_of_waiting(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: Any
    ) -> None:
        """The removal runs with interrupts deferred, so an unbounded wait on a hung operation made Ctrl-C useless."""
        network = _install_fakes(monkeypatch).network
        waits: list[float | None] = []

        class _HungPoller(_Poller):
            def result(self, timeout: float | None = None) -> object:
                waits.append(timeout)
                return super().result(timeout)

        monkeypatch.setattr(
            network.security_rules, "begin_delete", lambda *_args, **_options: _HungPoller(None, finishes=False)
        )
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        _platform().close_provisioning_access(vm, RunContext())  # no raise

        assert len(waits) == 1
        assert waits[0] is not None
        assert captured_output.info == []  # not reported closed
        (warning,) = captured_output.warnings
        assert BOOTSTRAP_ALLOW_RULE_NAME in warning

    def test_a_close_that_cannot_build_its_client_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller warns with what it raises, so the rule left open is named there, not lost."""
        _install_fakes(monkeypatch)
        platform = _platform()

        def no_client(*_args: object, **_kwargs: object) -> None:
            raise ConfigError("the site's client secret is unset")

        monkeypatch.setattr(platform, "_network_client", no_client)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(ProvisioningError):
            platform.close_provisioning_access(vm, RunContext())


class TestPerOperationAllows:
    def test_concurrent_pokes_coexist_and_remove_only_their_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two overlapping routes on one VM each own an independent rule:
        distinct nonce names, distinct band priorities, duplicate
        prefixes (expected: the rules are independent). The inner exit
        removes only its own rule, leaving the outer's standing: the
        cross-removal bug of the old single well-known rule is gone."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform()._transient_route(vm, RunContext()):
            outer_name = rules.transient_names()[0]
            with _platform()._transient_route(vm, RunContext()):
                inner_name = rules.transient_names()[1]
                assert inner_name != outer_name
                outer_rule, inner_rule = rules.rules[outer_name], rules.rules[inner_name]
                assert {outer_rule.priority, inner_rule.priority} == {
                    ALLOW_PRIORITY_BAND_START,
                    ALLOW_PRIORITY_BAND_START + 1,
                }
                # Duplicate prefixes across concurrent rules: fine and expected.
                assert outer_rule.source_address_prefixes == inner_rule.source_address_prefixes
            # Inner exited: its rule is gone, the outer's still stands.
            assert inner_name not in rules.rules
            assert outer_name in rules.rules
        assert outer_name not in rules.rules

    def test_slot_collision_retries_next_free_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Losing the slot race to a concurrent allocation (Azure rejects
        the duplicate priority) re-lists and takes the next free slot."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update
        collided = {"fired": False}

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX) and not collided["fired"]:
                # A concurrent operation wins slot 100 between our list
                # and our create: their rule lands, ours is rejected.
                collided["fired"] = True
                rules.rules["allow-ssh-transient-c0ffee00"] = SimpleNamespace(
                    name="allow-ssh-transient-c0ffee00",
                    priority=ALLOW_PRIORITY_BAND_START,
                    direction="Inbound",
                )
                raise RuntimeError("SecurityRuleConflict: priority is already in use")
            return real_create(rg, nsg, rule_name, rule)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        rule_name, _prefixes = _platform()._poke_ssh_allow(vm, RunContext())

        assert rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX)
        assert rules.rules[rule_name].priority == ALLOW_PRIORITY_BAND_START + 1
        # Exactly one of OUR rules is live (the winner's competitor rule
        # aside); the collided attempt left nothing behind.
        ours = [
            n for n in rules.rules if n.startswith(TRANSIENT_ALLOW_RULE_PREFIX) and n != "allow-ssh-transient-c0ffee00"
        ]
        assert ours == [rule_name]

    def test_band_exhaustion_raises_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A full band [100, 199] (100 concurrent ops, or stale rules
        leaked by killed processes) is a clear typed error whose hint
        points at the stale transient rules."""
        from agentworks.errors import StateError

        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        for priority in range(ALLOW_PRIORITY_BAND_START, ALLOW_PRIORITY_BAND_END + 1):
            rules.rules[f"squatter-{priority}"] = SimpleNamespace(
                name=f"squatter-{priority}", priority=priority, direction="Inbound"
            )
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(StateError) as exc:
            _platform()._poke_ssh_allow(vm, RunContext())
        assert "no free NSG priority" in str(exc.value)
        assert TRANSIENT_ALLOW_RULE_PREFIX in (exc.value.hint or "")

    def test_description_carries_timestamp_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every ephemeral allow's description carries the marker and a
        created-at UTC timestamp: the future doctor stale-rule sweep
        keys off them (no auto-pruning by age happens here)."""
        import re

        network = _install_fakes(monkeypatch).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        rule_name, _prefixes = _platform()._poke_ssh_allow(vm, RunContext())

        description = network.security_rules.rules[rule_name].description
        assert description.startswith(ALLOW_RULE_DESCRIPTION_MARKER)
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", description)


def _ip_response(body: bytes) -> MagicMock:
    """A mock urllib response context manager (the proxmox API suite's
    pattern) whose read() yields ``body``."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestDetectEgressIp:
    """The real detector (via the import-time alias, bypassing the
    autouse stub), with ``urllib.request.urlopen`` patched: no test
    performs live network IO. The autouse fixture resets the per-process
    cache between tests."""

    @patch("urllib.request.urlopen")
    def test_garbage_body_raises_and_never_caches(self, mock_urlopen: MagicMock) -> None:
        """An HTML error page (or any non-IPv4 body) is a detection
        failure, never a prefix: the strict parse raises and nothing is
        cached for a later call to reuse."""
        mock_urlopen.return_value = _ip_response(b"<html>rate limited</html>")
        with pytest.raises(ValueError):
            _REAL_DETECT_EGRESS_IP()
        assert ssh_exposure._egress_ip_cache is None

    @patch("urllib.request.urlopen")
    def test_whitespace_padded_ipv4_parses(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _ip_response(b"  198.51.100.7\n")
        assert _REAL_DETECT_EGRESS_IP() == "198.51.100.7"

    @patch("urllib.request.urlopen")
    def test_second_call_serves_from_cache(self, mock_urlopen: MagicMock) -> None:
        """One probe per process: the second call never re-opens."""
        mock_urlopen.return_value = _ip_response(b"198.51.100.7\n")
        assert _REAL_DETECT_EGRESS_IP() == "198.51.100.7"
        assert _REAL_DETECT_EGRESS_IP() == "198.51.100.7"
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_probe_uses_bounded_timeout(self, mock_urlopen: MagicMock) -> None:
        """The what's-my-ip probe carries the 5-second timeout: a hung
        service must not hang every azure op behind it."""
        mock_urlopen.return_value = _ip_response(b"198.51.100.7")
        _REAL_DETECT_EGRESS_IP()
        assert mock_urlopen.call_args.kwargs["timeout"] == 5


class TestEnsureDenyRaises:
    def test_converge_deny_failure_raises_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the deny baseline cannot be ensured, the enter fails loudly
        (a route without the baseline would be the old standing-exposure
        model by accident)."""

        network = _install_fakes(monkeypatch).network
        network.security_rules.create_error = RuntimeError("boom")
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when convergence fails")
