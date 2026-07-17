"""Azure VM size selection: the standard compute/memory model resolves
to the smallest fitting SKU from the built-in B-series ladder or the
site's ``platform_config.vm_sizes`` override (issue #178)."""

from __future__ import annotations

import pytest

from agentworks.capabilities.vm_platform.azure_vm import (
    _DEFAULT_VM_SIZES,
    AzureVMPlatform,
    _parse_size_catalog,
    _select_vm_size,
)
from agentworks.errors import ConfigError


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
        # over-provisioned on memory, which is what the create() warn keys on
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
        """Selection sorts, so an unsorted (operator) catalog still
        yields the true smallest fit."""
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
