"""Azure VM size selection: the standard compute/memory model resolves
to the smallest fitting SKU from the built-in B-series ladder or the
site's ``platform_config.vm_sizes`` override (issue #178)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.azure_vm import (
    _DEFAULT_VM_SIZES,
    AzureVMPlatform,
    _parse_size_catalog,
    _select_vm_size,
)
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


class TestSelectVMSize:
    def test_exact_match_wins(self) -> None:
        """A request that lands exactly on a SKU picks that SKU."""
        size = _select_vm_size(_DEFAULT_VM_SIZES, cpus=4, memory_gib=16)
        assert size.name == "Standard_B4ms"
        assert (size.cpus, size.memory_gib) == (4, 16)

    def test_off_ratio_rounds_up_to_smallest_fit(self) -> None:
        """4 vCPU / 8 GiB has no exact B-series SKU (they are 1:2 or
        1:4); it rounds up to the smallest entry satisfying BOTH axes,
        over-provisioning memory."""
        size = _select_vm_size(_DEFAULT_VM_SIZES, cpus=4, memory_gib=8)
        assert size.name == "Standard_B4ms"  # 4 vCPU / 16 GiB
        # over-provisioned on memory, which is what the create() round-up warn
        # keys on
        assert size.memory_gib > 8

    def test_picks_smallest_across_both_axes(self) -> None:
        """Among several fitting entries the minimum by (cpus, memory)
        wins, not merely the first that clears cpus."""
        size = _select_vm_size(_DEFAULT_VM_SIZES, cpus=2, memory_gib=8)
        assert size.name == "Standard_B2ms"  # 2/8 beats 4/16

    def test_no_fit_raises_with_largest_in_message(self) -> None:
        """A request larger than every entry errors, naming the ceiling."""
        with pytest.raises(ConfigError) as exc:
            _select_vm_size(_DEFAULT_VM_SIZES, cpus=64, memory_gib=256)
        assert "Standard_B20ms" in str(exc.value)
        assert exc.value.hint is not None

    def test_selection_independent_of_catalog_order(self) -> None:
        """Selection is order-independent (the minimum by (cpus, memory)),
        so an unsorted (operator) catalog still yields the true smallest
        fit."""
        from agentworks.capabilities.vm_platform.azure_vm import _VMSize

        unsorted = (
            _VMSize(8, 32, "big"),
            _VMSize(2, 8, "small"),
            _VMSize(4, 16, "mid"),
        )
        assert _select_vm_size(unsorted, cpus=2, memory_gib=8).name == "small"


class TestParseSizeCatalog:
    def test_no_override_returns_builtin(self) -> None:
        assert _parse_size_catalog({}, "vm-site/az") is _DEFAULT_VM_SIZES

    def test_valid_override_parses(self) -> None:
        cfg = {"vm_sizes": [{"cpus": 4, "memory": 16, "size": "Standard_D4s_v5"}]}
        catalog = _parse_size_catalog(cfg, "vm-site/az")
        assert catalog == ((4, 16, "Standard_D4s_v5"),)

    @pytest.mark.parametrize(
        "bad",
        [
            {"vm_sizes": "Standard_B2s"},  # not a list
            {"vm_sizes": []},  # empty
            {"vm_sizes": [{"cpus": 4, "memory": 16}]},  # missing size
            {"vm_sizes": [{"cpus": 0, "memory": 16, "size": "x"}]},  # non-positive
            {"vm_sizes": [{"cpus": True, "memory": 16, "size": "x"}]},  # bool cpus
            {"vm_sizes": [{"cpus": 4, "memory": 16, "size": ""}]},  # empty size
            {"vm_sizes": [{"cpus": 4, "memory": 16, "size": "x", "gpu": 1}]},  # unknown
            {"vm_sizes": ["Standard_B2s"]},  # entry not a table
        ],
    )
    def test_malformed_override_raises(self, bad: dict[str, object]) -> None:
        with pytest.raises(ConfigError):
            _parse_size_catalog(bad, "vm-site/az")


class TestValidateConfig:
    _BASE = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "region": "eastus",
    }

    def test_accepts_without_vm_sizes(self) -> None:
        assert AzureVMPlatform.validate_config("vm-site/az", dict(self._BASE)) == ()

    def test_accepts_valid_vm_sizes(self) -> None:
        cfg = {
            **self._BASE,
            "vm_sizes": [{"cpus": 2, "memory": 4, "size": "Standard_B2s"}],
        }
        assert AzureVMPlatform.validate_config("vm-site/az", cfg) == ()

    def test_rejects_malformed_vm_sizes_at_load(self) -> None:
        cfg = {**self._BASE, "vm_sizes": [{"cpus": 2, "size": "Standard_B2s"}]}
        with pytest.raises(ConfigError, match="memory"):
            AzureVMPlatform.validate_config("vm-site/az", cfg)

    def test_still_rejects_unknown_field(self) -> None:
        cfg = {**self._BASE, "bogus": "x"}
        with pytest.raises(ConfigError, match="unknown"):
            AzureVMPlatform.validate_config("vm-site/az", cfg)


class TestCreateProvisioningOutput:
    """The `vm create` provisioning line always names the selected SKU and
    its spec; a round-up additionally warns (issue #178 follow-up). The
    Azure SDK client factories are faked so ``create`` reaches its
    provisioning line without touching Azure."""

    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        def _collection(result: object) -> SimpleNamespace:
            poller = SimpleNamespace(result=lambda: result)
            return SimpleNamespace(
                begin_create_or_update=lambda *a, **k: poller,
                begin_delete=lambda *a, **k: poller,
            )

        fake_network = SimpleNamespace(
            public_ip_addresses=_collection(
                SimpleNamespace(ip_address="10.0.0.4", id="/pip")
            ),
            network_security_groups=_collection(SimpleNamespace(id="/nsg")),
            virtual_networks=_collection(
                SimpleNamespace(subnets=[SimpleNamespace(id="/subnet")])
            ),
            network_interfaces=_collection(SimpleNamespace(id="/nic")),
        )
        fake_compute = SimpleNamespace(
            virtual_machines=_collection(SimpleNamespace(id="/vm-id")),
        )

        # The SDK clients are per-instance cached accessors (keyed by
        # subscription); patch them on the class so the fakes are returned
        # without building a credential or touching Azure.
        monkeypatch.setattr(
            AzureVMPlatform, "_compute_client", lambda self, az: fake_compute
        )
        monkeypatch.setattr(
            AzureVMPlatform, "_network_client", lambda self, az: fake_network
        )
        monkeypatch.setattr(
            AzureVMPlatform, "_vm_exists", lambda self, compute, rg, name: False
        )

    @staticmethod
    def _request(*, cpus: int, memory: int) -> ProvisionRequest:
        # tailscale_auth_key=None keeps create() on the minimal-cloud-init
        # path, so it never waits for a bootstrap that has no VM to reach.
        return ProvisionRequest(
            vm_name="dev",
            hostname="dev",
            system_slug=None,
            admin_username="agw",
            ssh_public_key="ssh-ed25519 AAAA test",
            ssh_private_key=None,
            tailscale_auth_key=None,
            cpus=cpus,
            memory_gib=memory,
        )

    @staticmethod
    def _platform(vm_sizes: list[dict[str, object]] | None = None) -> AzureVMPlatform:
        config: dict[str, object] = {
            "subscription_id": "sub",
            "resource_group": "rg",
            "region": "eastus",
        }
        if vm_sizes is not None:
            config["vm_sizes"] = vm_sizes
        return AzureVMPlatform("azure", config)

    @staticmethod
    def _provisioning_line(captured: CapturedOutput) -> str:
        # The provisioning announcement is a primary (info/BODY) line; the
        # concrete resource-creation sub-steps are the DETAIL lines below it.
        return next(
            m for m in captured.info if m.startswith("Provisioning Azure VM")
        )

    def test_exact_match_emits_spec_without_requested(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        self._wire(monkeypatch)
        self._platform().create(self._request(cpus=2, memory=8), RunContext())
        line = self._provisioning_line(captured_output)
        assert line == (
            "Provisioning Azure VM 'dev' in eastus: "
            "size Standard_B2ms (2 vCPU / 8 GiB)..."
        )
        assert "for requested" not in line
        assert not captured_output.warnings

    def test_round_up_warns_and_line_shows_selected_spec(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        self._wire(monkeypatch)
        # 4 vCPU / 8 GiB has no exact B-series SKU; it rounds up to B4ms.
        self._platform().create(self._request(cpus=4, memory=8), RunContext())
        line = self._provisioning_line(captured_output)
        # The line carries only the selected spec; the round-up detail is in
        # the warning, not doubled into the line.
        assert line == (
            "Provisioning Azure VM 'dev' in eastus: "
            "size Standard_B4ms (4 vCPU / 16 GiB)..."
        )
        assert "for requested" not in line
        assert captured_output.warnings == [
            "Rounded up to Standard_B4ms (4 vCPU / 16 GiB) "
            "for requested 4 vCPU / 8 GiB."
        ]

    def test_non_burstable_override_selected_and_emitted(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A site override to a non-burstable SKU (the experiment behind
        this knob) is selected and its spec surfaced in the same shape."""
        self._wire(monkeypatch)
        sizes = [{"cpus": 2, "memory": 8, "size": "Standard_D2s_v5"}]
        self._platform(vm_sizes=sizes).create(
            self._request(cpus=2, memory=8), RunContext()
        )
        line = self._provisioning_line(captured_output)
        assert line == (
            "Provisioning Azure VM 'dev' in eastus: "
            "size Standard_D2s_v5 (2 vCPU / 8 GiB)..."
        )
        assert "for requested" not in line
