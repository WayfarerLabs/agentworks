"""The vm-site declarable kind: manifest decode, spec shape rules,
reserved built-in names, unknown-platform deferral, and reference
emission.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests.loader import load_manifests
from agentworks.resources import Origin, Registry
from agentworks.vms.sites import VMSiteDecl

SITE_DOC = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
  description: Dev subscription
spec:
  platform: azure
  platform_config:
    subscription_id: "0000"
    resource_group: agw-dev
    region: eastus
"""


def _load_one(tmp_path: Path, text: str) -> VMSiteDecl:
    (tmp_path / "site.yaml").write_text(text)
    manifests = load_manifests(tmp_path)
    assert not manifests.issues, manifests.issues
    (entry,) = manifests.entries
    assert entry.kind == "vm-site"
    resource = entry.resource
    assert isinstance(resource, VMSiteDecl)
    return resource


def test_decode_nests_platform_config(tmp_path: Path) -> None:
    site = _load_one(tmp_path, SITE_DOC)
    assert site.name == "azure-dev"
    assert site.platform == "azure"
    assert site.platform_config == {
        "subscription_id": "0000",
        "resource_group": "agw-dev",
        "region": "eastus",
    }
    assert site.description == "Dev subscription"


def test_site_names_follow_the_vm_name_rules(tmp_path: Path) -> None:
    """Site names appear in hostnames and SSH aliases, so they
    obey validate_name (lowercase, length cap, no double hyphen)."""
    doc = SITE_DOC.replace("name: azure-dev", "name: MY_Site_With_A_Very_Long_Name_Indeed")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="too long"):
        load_manifests(tmp_path)

    doc = SITE_DOC.replace("name: azure-dev", "name: azure--dev")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="consecutive hyphens"):
        load_manifests(tmp_path)


def test_platform_named_site_must_declare_that_platform(tmp_path: Path) -> None:
    """A site `vm-site/azure` backed by lima would make `--site azure`
    mean something other than it says."""
    doc = (
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        "  name: azure\n"
        "spec:\n"
        "  platform: lima\n"
    )
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="shadows a platform name"):
        load_manifests(tmp_path)


def test_decode_requires_platform(tmp_path: Path) -> None:
    doc = SITE_DOC.replace("  platform: azure\n", "")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="spec.platform"):
        load_manifests(tmp_path)


def test_decode_rejects_blob_shadowing(tmp_path: Path) -> None:
    doc = SITE_DOC + "    platform: lima\n"
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="kind-owned field"):
        load_manifests(tmp_path)


def test_decode_rejects_stray_spec_keys(tmp_path: Path) -> None:
    doc = SITE_DOC + "  region: eastus\n"
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="unknown vm-site spec field"):
        load_manifests(tmp_path)


def test_decode_validates_the_blob_via_the_capability(tmp_path: Path) -> None:
    doc = SITE_DOC.replace('    subscription_id: "0000"\n', "")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="subscription_id"):
        load_manifests(tmp_path)


def test_unknown_platform_defers_to_the_miss_policy(tmp_path: Path) -> None:
    """Decode must not error on an unregistered platform; the framework's
    error miss policy reports it uniformly at finalize."""
    doc = SITE_DOC.replace("platform: azure", "platform: nope").replace(
        "platform_config:", "ignored_config:"
    )
    # Rebuild a minimal valid doc for an unknown platform (no blob).
    doc = (
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        "  name: mystery\n"
        "spec:\n"
        "  platform: nope\n"
    )
    site = _load_one(tmp_path, doc)
    assert site.platform == "nope"

    registry = Registry.empty()
    registry.add("vm-site", "mystery", site, Origin.built_in(source="test"))
    with pytest.raises(ConfigError, match="unknown vm-platform 'nope'"):
        registry.finalize()


def test_reference_emission(tmp_path: Path) -> None:
    site = _load_one(tmp_path, SITE_DOC)
    refs = site.referenced_resources()
    assert [(r.kind, r.name) for r in refs] == [("vm-platform", "azure")]
    assert refs[0].source == ("vm-site", "azure-dev")


def test_proxmox_site_emits_the_token_secret_reference() -> None:
    site = VMSiteDecl(
        name="px",
        platform="proxmox",
        platform_config={
            "api_url": "https://pve:8006",
            "node": "pve1",
            "token_id": "t",
            "template_vmid": 9000,
        },
    )
    refs = site.referenced_resources()
    assert [(r.kind, r.name) for r in refs] == [
        ("vm-platform", "proxmox"),
        ("secret", "proxmox-token-secret"),
    ]
    assert all(r.source == ("vm-site", "px") for r in refs)


def test_bundled_sites_are_reserved(tmp_path: Path) -> None:
    """An operator manifest redeclaring a bundled site name errors with
    the declare-a-sibling shape (builtin_override = reserved)."""
    from agentworks.manifests import builtin as builtin_manifests

    (tmp_path / "site.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        "  name: lima\n"
        "spec:\n"
        "  platform: lima\n"
    )
    manifests = load_manifests(tmp_path)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    with pytest.raises(ConfigError, match="lima"):
        manifests.publish_to(registry)


def test_bundled_sites_finalize_against_the_platform_rows() -> None:
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.vms import platforms as vm_platforms

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()
    assert registry.lookup("vm-site", "lima").platform == "lima"
    assert registry.lookup("vm-site", "wsl2").platform == "wsl2"
    assert registry.lookup("vm-platform", "azure").name == "azure"
