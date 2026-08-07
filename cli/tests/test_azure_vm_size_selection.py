"""Azure VM size selection: the standard compute/memory model resolves
to the smallest fitting SKU from the built-in B-series ladder or the
site's ``platform_config.vm_sizes`` override (issue #178)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.errors import ConfigError
from agentworks.plugins.azure.platform import (
    _DEFAULT_VM_SIZES,
    IMAGE_OFFER,
    IMAGE_OS_DISK_FLOOR_GIB,
    IMAGE_PUBLISHER,
    IMAGE_SKU,
    IMAGE_VERSION,
    AzureVMConfig,
    AzureVMPlatform,
    _select_vm_size,
    _size_catalog,
)
from agentworks.schema import RefOwner

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
        from agentworks.plugins.azure.platform import _VMSize

        unsorted = (
            _VMSize(8, 32, "big"),
            _VMSize(2, 8, "small"),
            _VMSize(4, 16, "mid"),
        )
        assert _select_vm_size(unsorted, cpus=2, memory_gib=8).name == "small"


class TestSizeCatalog:
    """The catalog RESOLVER: the shape is the model's business now, so
    what is left here is the default (domain knowledge) and the mapping
    onto the selection tuple. The shape rejections move with the shape,
    to the config-contract suite, but they are still asserted here
    through the core so this file keeps proving that a bad catalog never
    reaches selection."""

    def test_no_override_returns_builtin(self) -> None:
        assert _size_catalog(_config({})) is _DEFAULT_VM_SIZES

    def test_valid_override_parses(self) -> None:
        catalog = _size_catalog(_config({"vm_sizes": [{"cpus": 4, "memory": 16, "size": "Standard_D4s_v5"}]}))
        assert catalog == ((4, 16, "Standard_D4s_v5"),)

    @pytest.mark.parametrize(
        "bad",
        [
            {"vm_sizes": "Standard_B2s"},  # not a list
            {"vm_sizes": []},  # empty: a site on which no VM can be created
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
            _config(bad)


class TestValidateConfig:
    """The catalog's shape as an operator writes it, through the core."""

    def test_accepts_without_vm_sizes(self) -> None:
        _config({})
        assert (
            capability_config_references(
                kind="vm-platform", config={"name": "azure-vm", **_BASE}, owner=RefOwner(kind="vm-site", name="az")
            )
            == ()
        )

    def test_rejects_malformed_vm_sizes_at_load(self) -> None:
        with pytest.raises(ConfigError, match=r"vm_sizes\[0\].memory: is required"):
            _config({"vm_sizes": [{"cpus": 2, "size": "Standard_B2s"}]})

    def test_still_rejects_unknown_field(self) -> None:
        with pytest.raises(ConfigError, match="bogus: unknown field"):
            _config({"bogus": "x"})


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
            public_ip_addresses=_collection(SimpleNamespace(ip_address="10.0.0.4", id="/pip")),
            network_security_groups=_collection(SimpleNamespace(id="/nsg")),
            virtual_networks=_collection(SimpleNamespace(subnets=[SimpleNamespace(id="/subnet")])),
            network_interfaces=_collection(SimpleNamespace(id="/nic")),
        )
        fake_compute = SimpleNamespace(
            virtual_machines=_collection(SimpleNamespace(id="/vm-id")),
        )

        # The SDK clients are per-instance cached accessors (keyed by
        # subscription); patch them on the class so the fakes are returned
        # without building a credential or touching Azure.
        monkeypatch.setattr(AzureVMPlatform, "_compute_client", lambda self, az, ctx: fake_compute)
        monkeypatch.setattr(AzureVMPlatform, "_network_client", lambda self, az, ctx: fake_network)
        monkeypatch.setattr(AzureVMPlatform, "_vm_exists", lambda self, compute, rg, name: False)

    @staticmethod
    def _request(*, cpus: int, memory: int, disk: int = 50, swap: int = 4) -> ProvisionRequest:
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
            disk_gib=disk,
            swap_gib=swap,
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
        return next(m for m in captured.info if m.startswith("Provisioning Azure VM"))

    def test_exact_match_emits_spec_without_requested(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        self._wire(monkeypatch)
        self._platform().create(self._request(cpus=2, memory=8), RunContext())
        line = self._provisioning_line(captured_output)
        assert line == ("Provisioning Azure VM 'dev' in eastus: size Standard_B2ms (2 vCPU / 8 GiB)...")
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
        assert line == ("Provisioning Azure VM 'dev' in eastus: size Standard_B4ms (4 vCPU / 16 GiB)...")
        assert "for requested" not in line
        assert captured_output.warnings == [
            "Rounded up to Standard_B4ms (4 vCPU / 16 GiB) for requested 4 vCPU / 8 GiB."
        ]

    def test_non_burstable_override_selected_and_emitted(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A site override to a non-burstable SKU (the experiment behind
        this knob) is selected and its spec surfaced in the same shape."""
        self._wire(monkeypatch)
        sizes = [{"cpus": 2, "memory": 8, "size": "Standard_D2s_v5"}]
        self._platform(vm_sizes=sizes).create(self._request(cpus=2, memory=8), RunContext())
        line = self._provisioning_line(captured_output)
        assert line == ("Provisioning Azure VM 'dev' in eastus: size Standard_D2s_v5 (2 vCPU / 8 GiB)...")
        assert "for requested" not in line


class TestCreateOSDiskClamp:
    """`create` clamps a below-floor vm-template disk up to the image's minimum
    (IMAGE_OS_DISK_FLOOR_GIB) and warns, mirroring the cpu/memory round-up; an
    at-or-above-floor disk passes through untouched with no warning (issue #322
    follow-up). The recorded OS-disk shape also pins ``delete_option`` (issue
    #334). The Azure clients are faked so the flow never touches Azure (same
    pattern as ``TestCreateProvisioningOutput``)."""

    class _RecordingVMs:
        """A fake ``virtual_machines`` recording the OS-disk shape ``create`` sends."""

        def __init__(self) -> None:
            self.disk_gib: int | None = None
            self.delete_option: str | None = None

        def begin_create_or_update(self, rg: str, name: str, params: object) -> SimpleNamespace:
            os_disk = params.storage_profile.os_disk  # type: ignore[attr-defined]
            self.disk_gib = os_disk.disk_size_gb
            self.delete_option = os_disk.delete_option
            return SimpleNamespace(result=lambda: SimpleNamespace(id="/vm-id"))

    @staticmethod
    def _fake_network() -> SimpleNamespace:
        def _collection(result: object) -> SimpleNamespace:
            poller = SimpleNamespace(result=lambda: result)
            return SimpleNamespace(
                begin_create_or_update=lambda *a, **k: poller,
                begin_delete=lambda *a, **k: poller,
            )

        return SimpleNamespace(
            public_ip_addresses=_collection(SimpleNamespace(ip_address="10.0.0.4", id="/pip")),
            network_security_groups=_collection(SimpleNamespace(id="/nsg")),
            virtual_networks=_collection(SimpleNamespace(subnets=[SimpleNamespace(id="/subnet")])),
            network_interfaces=_collection(SimpleNamespace(id="/nic")),
        )

    def _run(self, monkeypatch: pytest.MonkeyPatch, *, disk_gib: int) -> _RecordingVMs:
        vms = self._RecordingVMs()
        fake_compute = SimpleNamespace(virtual_machines=vms)
        fake_network = self._fake_network()
        monkeypatch.setattr(AzureVMPlatform, "_compute_client", lambda self, az, ctx: fake_compute)
        monkeypatch.setattr(AzureVMPlatform, "_network_client", lambda self, az, ctx: fake_network)
        monkeypatch.setattr(AzureVMPlatform, "_vm_exists", lambda self, compute, rg, name: False)
        request = ProvisionRequest(
            vm_name="dev",
            hostname="dev",
            system_slug=None,
            admin_username="agw",
            ssh_public_key="ssh-ed25519 AAAA test",
            ssh_private_key=None,
            tailscale_auth_key=None,
            cpus=2,
            memory_gib=8,
            disk_gib=disk_gib,
            swap_gib=4,
        )
        config: dict[str, object] = {
            "subscription_id": "sub",
            "resource_group": "rg",
            "region": "eastus",
        }
        AzureVMPlatform("azure", config).create(request, RunContext())
        return vms

    def test_below_floor_clamps_up_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        vms = self._run(monkeypatch, disk_gib=10)
        assert vms.disk_gib == IMAGE_OS_DISK_FLOOR_GIB
        assert captured_output.warnings == [
            f"Rounded up to {IMAGE_OS_DISK_FLOOR_GIB} GiB OS disk (image minimum) for requested 10 GiB."
        ]

    def test_at_or_above_floor_unchanged_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        vms = self._run(monkeypatch, disk_gib=50)
        assert vms.disk_gib == 50
        assert not captured_output.warnings

    def test_os_disk_delete_option_is_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The OS disk is declared VM-lifetime (``delete_option="Delete"``) so
        Azure removes it with the VM regardless of the tag-based cleanup sweep
        (issue #334)."""
        vms = self._run(monkeypatch, disk_gib=50)
        assert vms.delete_option == "Delete"


class TestImageOSDiskFloorConstant:
    """The OS-disk floor is pinned by hand (it is not exposed on the marketplace
    image model), so it is coupled here to the exact image it describes: changing
    the image without revisiting the floor fails this test loudly."""

    def test_floor_matches_pinned_image(self) -> None:
        # Debian 12 (12-gen2) ships a 30 GiB OS disk. If any part of the image
        # identity changes (an offer bump to debian-13 is the likely one),
        # confirm the new image's floor and update both together.
        assert (IMAGE_PUBLISHER, IMAGE_OFFER, IMAGE_SKU, IMAGE_VERSION) == (
            "Debian",
            "debian-12",
            "12-gen2",
            "latest",
        )
        assert IMAGE_OS_DISK_FLOOR_GIB == 30


#: The three keys every azure-vm site needs, so the tests below can talk
#: about the catalog alone.
_BASE = {"subscription_id": "sub", "resource_group": "rg", "region": "eastus"}


def _config(blob: dict[str, object]) -> AzureVMConfig:
    """``blob`` validated as an azure-vm site's config, through the core."""
    validated = validate_capability_config(
        kind="vm-platform",
        config={"name": "azure-vm", **_BASE, **blob},
        owner=RefOwner(kind="vm-site", name="az"),
    )
    assert isinstance(validated, AzureVMConfig)
    return validated
