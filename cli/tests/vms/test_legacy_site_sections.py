"""Legacy TOML [azure] / [proxmox] sections are a hard error now (ADR 0022:
config.toml is settings only), plus the defaults.site
alias behavior (settings, unaffected) and the YAML vm-site manifest path.

The old dual-path (flat TOML sections loading as vm-site resources) is gone:
an operator rewrites those sections as vm-site manifests by hand, which is
what the load error tells them to do and what the resources guide walks
through. The token_secret nonconforming-name warning that the flat TOML
loader emitted is now surfaced only on the YAML manifest path (tested here).
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


def test_legacy_sections_are_a_hard_error(write_config) -> None:
    """[azure] / [proxmox] are resource-declaring sections now, so a normal
    load hard-errors, naming the sections and the vm-site clause (the kind
    they become, which neither section name would suggest)."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(AZURE_SECTION + PROXMOX_SECTION), warn_issues=False)
    message = str(excinfo.value)
    assert "[azure]" in message
    assert "[proxmox]" in message
    assert "become vm-site manifests" in message


def test_settings_only_load_reads_a_config_with_legacy_sections(write_config) -> None:
    """The escape hatch loads a config carrying [azure] without error
    (`resource sample --write` uses it, so an operator can author the
    replacement manifests before deleting the sections); settings load
    identically."""
    config = load_config(write_config(AZURE_SECTION), warn_issues=False, resources=False)
    assert config.operator is not None


def test_defaults_site_parses(write_config) -> None:
    config = load_config(
        write_config('[defaults]\nsite = "lima"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.defaults.site == "lima"


def test_defaults_platform_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match=r"unexpected keys in \[defaults\]: platform"):
        load_config(
            write_config('[defaults]\nplatform = "lima"\n'),
            warn_issues=False,
            warn_deprecations=False,
        )


def test_defaults_vm_host_is_a_hard_error(write_config) -> None:
    with pytest.raises(ConfigError, match="defaults.vm_host has been removed"):
        load_config(
            write_config('[defaults]\nvm_host = "gpu-box"\n'),
            warn_issues=False,
            warn_deprecations=False,
        )


def _write_site_manifest(manifest_dir: Path, token_secret: str) -> None:
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "site.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        "  name: proxmox\n"
        "spec:\n"
        "  platform:\n"
        "    name: proxmox\n"
        '    api_url: "https://pve:8006"\n'
        "    node: pve1\n"
        f'    token_secret: "{token_secret}"\n'
    )


def test_manifest_token_secret_nonconforming_warns(tmp_path: Path) -> None:
    """The YAML manifest path emits the token_secret warning: a non-conforming
    ``token_secret`` decodes with an issue whose shape-neutral location names
    the key and the bad secret."""
    from agentworks.manifests.loader import load_manifests

    manifest_dir = tmp_path / "resources"
    _write_site_manifest(manifest_dir, "GITHUB_TOKEN")
    manifests = load_manifests(manifest_dir)
    assert any("GITHUB_TOKEN" in issue and "vm-site/proxmox" in issue for issue in manifests.issues), manifests.issues
    (entry,) = manifests.entries
    assert entry.resource.platform.config["token_secret"] == "GITHUB_TOKEN"


def test_manifest_token_secret_conforming_emits_no_warning(tmp_path: Path) -> None:
    """A conforming ``token_secret`` in a YAML vm-site manifest emits no
    secret-naming warning."""
    from agentworks.manifests.loader import load_manifests

    manifest_dir = tmp_path / "resources"
    _write_site_manifest(manifest_dir, "proxmox-token")
    manifests = load_manifests(manifest_dir)
    assert not any("secret naming rules" in issue for issue in manifests.issues), manifests.issues
