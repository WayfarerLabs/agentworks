"""Dual-path legacy TOML: [azure] / [proxmox] load as vm-site resources
with the aggregated deprecation warning, and the defaults.site /
defaults.platform alias behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError

BASE = """\
[operator]
ssh_public_key = "{key}.pub"
ssh_private_key = "{key}"
"""

AZURE_SECTION = """
[azure]
subscription_id = "0000"
resource_group = "agw"
region = "eastus"
"""

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""


@pytest.fixture
def write_config(tmp_path: Path):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")

    def _write(extra: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(BASE.format(key=key) + extra)
        return path

    return _write


def test_legacy_sections_load_as_vm_sites(write_config) -> None:
    config = load_config(
        write_config(AZURE_SECTION + PROXMOX_SECTION),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert set(config.vm_sites) == {"azure", "proxmox"}
    azure = config.vm_sites["azure"]
    # The SITE keeps its legacy section name; the platform underneath
    # is the renamed azure-vm capability.
    assert azure.platform == "azure-vm"
    assert azure.platform_config["resource_group"] == "agw"
    proxmox = config.vm_sites["proxmox"]
    assert proxmox.platform == "proxmox"
    assert proxmox.platform_config["node"] == "pve1"
    # The flat sections warn as deprecated resource sections.
    joined = "\n".join(config.deprecation_issues)
    assert "[azure]" in joined
    assert "[proxmox]" in joined
    assert "[azure]" in config.deprecated_sections
    assert "[proxmox]" in config.deprecated_sections


def test_legacy_sections_publish_and_finalize(write_config) -> None:
    from agentworks.bootstrap import build_registry
    from agentworks.manifests import ManifestSet

    config = load_config(
        write_config(AZURE_SECTION),
        warn_issues=False,
        warn_deprecations=False,
    )
    registry = build_registry(config, ManifestSet.empty())
    row = registry.lookup("vm-site", "azure")
    assert row.platform == "azure-vm"
    assert row.origin is not None
    assert row.origin.variant == "operator-declared"


def test_legacy_section_blob_validates(write_config) -> None:
    broken = AZURE_SECTION.replace('subscription_id = "0000"\n', "")
    with pytest.raises(ConfigError, match="subscription_id"):
        load_config(write_config(broken), warn_issues=False, warn_deprecations=False)


def test_settings_only_load_skips_legacy_sites(write_config) -> None:
    config = load_config(
        write_config(AZURE_SECTION),
        warn_issues=False,
        warn_deprecations=False,
        resources=False,
    )
    assert config.vm_sites == {}


def test_defaults_site_parses(write_config) -> None:
    config = load_config(
        write_config('[defaults]\nsite = "lima"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.defaults.site == "lima"


def test_defaults_platform_alias_maps_to_site(write_config) -> None:
    """The alias carries over unchanged for the non-lima values; the
    old ``lima`` (which meant local Lima) translates to the bundled
    site's new name, ``lima-local``."""
    config = load_config(
        write_config('[defaults]\nplatform = "lima"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.defaults.site == "lima-local"
    assert any("defaults.platform is deprecated" in issue for issue in config.deprecation_issues)

    config = load_config(
        write_config('[defaults]\nplatform = "azure"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.defaults.site == "azure"


def test_defaults_alias_disagreement_prefers_site(write_config) -> None:
    config = load_config(
        write_config('[defaults]\nsite = "azure"\nplatform = "lima"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.defaults.site == "azure"
    assert any("site wins" in issue for issue in config.config_issues)


def test_defaults_vm_host_is_a_hard_error(write_config) -> None:
    with pytest.raises(ConfigError, match="defaults.vm_host has been removed"):
        load_config(
            write_config('[defaults]\nvm_host = "gpu-box"\n'),
            warn_issues=False,
            warn_deprecations=False,
        )


def test_legacy_toml_and_manifest_decode_agree(write_config, tmp_path: Path) -> None:
    """Decode parity: a flat [proxmox] section and the equivalent
    vm-site manifest produce the same resource fields. The Phase 5
    migrator's flat-to-nested emission leans on this equivalence.
    """
    from agentworks.manifests.loader import load_manifests
    from agentworks.vms.sites import VMSiteDecl

    config = load_config(
        write_config(PROXMOX_SECTION),
        warn_issues=False,
        warn_deprecations=False,
    )
    toml_site = config.vm_sites["proxmox"]

    manifest_dir = tmp_path / "resources"
    manifest_dir.mkdir()
    (manifest_dir / "site.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        "  name: proxmox\n"
        "spec:\n"
        "  platform: proxmox\n"
        "  platform_config:\n"
        '    api_url: "https://pve:8006"\n'
        "    node: pve1\n"
        '    token_id: "agw@pam!agw"\n'
        "    template_vmid: 9000\n"
    )
    manifests = load_manifests(manifest_dir)
    assert not manifests.issues, manifests.issues
    (entry,) = manifests.entries
    yaml_site = entry.resource
    assert isinstance(yaml_site, VMSiteDecl)

    assert toml_site.name == yaml_site.name
    assert toml_site.platform == yaml_site.platform
    assert toml_site.platform_config == yaml_site.platform_config
    assert toml_site.description == yaml_site.description
