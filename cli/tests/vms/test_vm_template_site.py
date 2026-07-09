"""The vm-template ``site`` field: TOML parse, manifest parity, the
reference edge, inheritance merge semantics, and the vm-create site
selection precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.vms.sites import select_site
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict

BASE = """\
[operator]
ssh_public_key = "{key}.pub"
ssh_private_key = "{key}"
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


def test_toml_parses_site(write_config) -> None:
    config = load_config(
        write_config('[vm_templates.gpu]\nsite = "azure"\n'),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.vm_templates["gpu"].site == "azure"
    # Omitted means inherit (the raw-template None sentinel).
    config = load_config(
        write_config("[vm_templates.gpu]\ncpus = 8\n"),
        warn_issues=False,
        warn_deprecations=False,
    )
    assert config.vm_templates["gpu"].site is None


def test_toml_rejects_empty_site(write_config) -> None:
    with pytest.raises(ConfigError, match="site must be a non-empty site name"):
        load_config(
            write_config('[vm_templates.gpu]\nsite = ""\n'),
            warn_issues=False,
            warn_deprecations=False,
        )


def test_manifest_decode_parity(tmp_path: Path) -> None:
    from agentworks.manifests.loader import load_manifests

    (tmp_path / "template.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: vm-template\n"
        "metadata:\n"
        "  name: gpu\n"
        "spec:\n"
        "  site: azure\n"
    )
    manifests = load_manifests(tmp_path)
    assert not manifests.issues, manifests.issues
    (entry,) = manifests.entries
    resource = entry.resource
    assert isinstance(resource, VMTemplate)
    assert resource.site == "azure"


def test_site_emits_the_vm_site_reference_edge() -> None:
    refs = VMTemplate(name="gpu", site="azure").referenced_resources()
    site_refs = [r for r in refs if r.kind == "vm-site"]
    assert [(r.name, r.source) for r in site_refs] == [
        ("azure", ("vm-template", "gpu"))
    ]
    # No site, no edge.
    refs = VMTemplate(name="gpu").referenced_resources()
    assert not [r for r in refs if r.kind == "vm-site"]


def test_inheritance_child_overrides_parent() -> None:
    templates = {
        "base": VMTemplate(name="base", site="azure"),
        "child": VMTemplate(name="child", inherits=["base"]),
        "override": VMTemplate(name="override", inherits=["base"], site="proxmox"),
    }
    assert resolve_from_dict(templates, "child").site == "azure"
    assert resolve_from_dict(templates, "override").site == "proxmox"


def test_inheritance_later_parent_clobbers_scalar() -> None:
    """Pin the existing scalar merge semantics: with multiple parents,
    the LAST parent's value wins even when it is unset (None). Lists
    append; scalars -- site included -- do not deep-merge."""
    templates = {
        "sited": VMTemplate(name="sited", site="azure"),
        "plain": VMTemplate(name="plain"),
        "both": VMTemplate(name="both", inherits=["sited", "plain"]),
    }
    assert resolve_from_dict(templates, "both").site is None


def test_select_site_precedence() -> None:
    """SDD R2: flag, then template, then defaults.site, then lima."""
    assert select_site("flagged", "templated", "defaulted") == "flagged"
    assert select_site(None, "templated", "defaulted") == "templated"
    assert select_site(None, None, "defaulted") == "defaulted"
    assert select_site(None, None, None) == "lima"
