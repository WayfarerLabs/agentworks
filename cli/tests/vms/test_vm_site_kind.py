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
from agentworks.resources.graph import BuildContext
from agentworks.vms.sites import VMSiteDecl

SITE_DOC = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
  description: Dev subscription
spec:
  platform: azure-vm
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
    assert site.platform == "azure-vm"
    assert site.platform_config == {
        "subscription_id": "0000",
        "resource_group": "agw-dev",
        "region": "eastus",
    }
    assert site.description == "Dev subscription"


def test_site_names_obey_the_freeform_name_rules(tmp_path: Path) -> None:
    """Site names hit no OS identifier limit (registry key + display only, NOT
    derived into hostnames or SSH aliases: VM names are), so they use the
    freeform cap (64). They still obey validate_name's character rules
    (lowercase, no double hyphen) and reject a name past the freeform cap."""
    from agentworks.config import MAX_FREEFORM_NAME_LENGTH

    # Character rules still hold: uppercase is rejected regardless of length.
    doc = SITE_DOC.replace("name: azure-dev", "name: MY_Site")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="lowercase"):
        load_manifests(tmp_path)

    # Consecutive hyphens are still rejected.
    doc = SITE_DOC.replace("name: azure-dev", "name: azure--dev")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="consecutive hyphens"):
        load_manifests(tmp_path)

    # A name past the freeform cap is rejected as too long.
    doc = SITE_DOC.replace("name: azure-dev", f"name: {'a' * (MAX_FREEFORM_NAME_LENGTH + 1)}")
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="too long"):
        load_manifests(tmp_path)

    # A name that the old 30-char cap rejected but the freeform cap allows now
    # decodes cleanly (40 chars, lowercase, no double hyphen).
    site = _load_one(tmp_path, SITE_DOC.replace("name: azure-dev", f"name: {'a' * 40}"))
    assert site.name == "a" * 40


def test_platform_named_site_must_declare_that_platform(tmp_path: Path) -> None:
    """A site `vm-site/azure-vm` backed by lima would make
    `--site azure-vm` mean something other than it says.

    The shadow check reads ``VM_PLATFORM_REGISTRY``, so the precondition is
    that ``azure-vm`` is seated there. It ships in the opt-in ``azure`` system
    plugin, and importing the plugins package seats every shipped plugin's
    platform at import (``SYSTEM_PLUGINS`` build). Establish that here so the
    check fires whether this file runs solo or in-suite; a full-suite run gets
    the same seating incidentally from an earlier plugin test, which is why
    this test passed in-suite but not solo before (issue #302).
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "azure" in SYSTEM_PLUGINS
    assert "azure-vm" in VM_PLATFORM_REGISTRY

    doc = "apiVersion: agentworks/v1\nkind: vm-site\nmetadata:\n  name: azure-vm\nspec:\n  platform: lima\n"
    (tmp_path / "site.yaml").write_text(doc)
    with pytest.raises(ConfigError, match="shadows a platform name"):
        load_manifests(tmp_path)


def test_decode_requires_platform(tmp_path: Path) -> None:
    doc = SITE_DOC.replace("  platform: azure-vm\n", "")
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


def test_build_registry_validates_the_blob_via_the_capability(tmp_path: Path) -> None:
    """The platform_config blob's shape check moved out of decode into
    the finalize ``validate`` pass (R3): a malformed blob decodes fine
    and fails at build_registry, framed by the site name (``azure-dev``)
    with the source location re-attached from the origin. ``azure-vm`` ships
    in the opt-in ``azure`` system plugin, whose validation is deferred while
    disabled, so the plugin is enabled here for the blob check to fire."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{pub.as_posix()}"\nssh_private_key = "{priv.as_posix()}"\n'
        '[plugins]\nsystem = ["azure"]\n'
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "site.yaml").write_text(SITE_DOC.replace('    subscription_id: "0000"\n', ""))
    with pytest.raises(ConfigError, match="subscription_id") as exc:
        build_registry(load_config(cfg, warn_issues=False))
    assert "site.yaml" in str(exc.value)


def test_unknown_platform_site_hard_errors_at_finalize(tmp_path: Path) -> None:
    """R9.2: decode still must not error on an unregistered platform (the
    total-``dependencies`` contract), but finalize now DOES: with the
    edge-suppression removed the site emits its platform edge
    unconditionally, and the absent ``vm-platform`` row is the error miss
    policy's unknown-reference. A typo no longer silently self-disables."""
    doc = "apiVersion: agentworks/v1\nkind: vm-site\nmetadata:\n  name: mystery\nspec:\n  platform: nope\n"
    site = _load_one(tmp_path, doc)
    assert site.platform == "nope"
    # The platform edge is always emitted now (suppression removed).
    assert [(r.kind, r.name) for r in site.dependencies(BuildContext())] == [("vm-platform", "nope")]

    registry = Registry.empty()
    registry.add("vm-site", "mystery", site, Origin.built_in(source="test"))
    with pytest.raises(ConfigError, match="unknown vm-platform 'nope'"):
        registry.finalize()


def test_reference_emission(tmp_path: Path) -> None:
    site = _load_one(tmp_path, SITE_DOC)
    refs = site.dependencies(BuildContext())
    assert [(r.kind, r.name) for r in refs] == [("vm-platform", "azure-vm")]
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
    refs = site.dependencies(BuildContext())
    assert [(r.kind, r.name) for r in refs] == [
        ("vm-platform", "proxmox"),
        ("secret", "proxmox-token"),
    ]
    assert all(r.source == ("vm-site", "px") for r in refs)


def test_azure_service_principal_secret_reaches_the_site_node(tmp_path: Path) -> None:
    """The whole hop, end to end: a manifest-declared azure site with a
    ``service_principal`` block reaches ``vm_site_node(...).secret_refs()``
    carrying its client secret.

    Three links that the per-layer unit tests each cover only one side
    of: the platform's ``dependencies`` derives the reference from the
    blob, finalize records it as a ``secret`` edge on the retained graph,
    and the node factory reads THAT graph (not a re-walk of the decl) to
    build ``secret_refs``. Those refs are what put the client secret into
    an operation's boundary union, so a break anywhere along here would
    surface as "the site authenticates fine until the secret is actually
    needed", which is exactly the failure this pins against.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.vms.nodes import vm_site_node

    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{pub.as_posix()}"\nssh_private_key = "{priv.as_posix()}"\n'
        '[plugins]\nsystem = ["azure"]\n'
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "site.yaml").write_text(
        SITE_DOC + "    service_principal:\n      tenant_id: tenant-1\n      client_id: client-1\n      secret: az-sp\n"
    )

    registry = build_registry(load_config(cfg, warn_issues=False))

    assert vm_site_node(registry, "azure-dev").secret_refs() == ("az-sp",)
    # And a site WITHOUT the block declares nothing, so the ambient-path
    # sites every existing operator runs stay edge-free.
    (resources / "site.yaml").write_text(SITE_DOC.replace("name: azure-dev", "name: azure-ambient"))
    ambient = build_registry(load_config(cfg, warn_issues=False))
    assert vm_site_node(ambient, "azure-ambient").secret_refs() == ()


def test_host_unsupported_site_still_emits_its_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R13/R12: a host-unsupported site emits its edges UNCONDITIONALLY
    now (the suppression is gone). ``dependencies`` is total and blind to
    host support; keeping a not-ready site's config-implied secret out of
    the registry is the readiness-gated MATERIALIZATION pass's job (R12),
    not the edge walk's. Pinned against the first plugin that ships a
    host-gated platform WITH a config secret."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "unsupported_reason", classmethod(lambda cls: "no cluster os"))
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
    assert [(r.kind, r.name) for r in site.dependencies(BuildContext())] == [
        ("vm-platform", "proxmox"),
        ("secret", "proxmox-token"),
    ]


def test_bundled_sites_are_reserved(tmp_path: Path) -> None:
    """An operator manifest redeclaring a bundled site name errors with
    the declare-a-sibling shape (builtin_override = reserved)."""
    from agentworks.manifests import builtin as builtin_manifests

    (tmp_path / "site.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: vm-site\nmetadata:\n  name: lima-local\nspec:\n  platform: lima\n"
    )
    manifests = load_manifests(tmp_path)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    with pytest.raises(ConfigError, match="lima-local"):
        manifests.publish_to(registry)


def test_bundled_sites_finalize_against_the_platform_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.manifests import builtin as builtin_manifests
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()
    assert registry.lookup("vm-site", "lima-local").platform == "lima"
    assert registry.lookup("vm-site", "wsl2").platform == "wsl2"
    # ``azure-vm`` is no longer a core built-in row; it publishes from the
    # ``azure`` system plugin (Phase 11), so the core publisher above does not
    # emit it. Its platform row is exercised in tests/plugins/test_azure.py.
    assert registry.lookup("vm-platform", "lima").name == "lima"
