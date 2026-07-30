"""The vm-platform capability kind: read-only rows, not declarable."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.capabilities import vm_platform as vm_platforms
from agentworks.errors import ConfigError
from agentworks.manifests.loader import load_manifests
from agentworks.resources import KIND_REGISTRY, Registry


def test_kind_flags() -> None:
    kind = KIND_REGISTRY["vm-platform"]
    assert kind.category == "capability"
    assert kind.miss_policy == "error"
    site_kind = KIND_REGISTRY["vm-site"]
    assert site_kind.category == "declarable"
    assert site_kind.miss_policy == "error"
    assert site_kind.builtin_override == "reserved"


def test_publisher_adds_one_row_per_installed_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    vm_platforms.publish_to(registry)
    names = {entry.name for entry in registry.iter_kind("vm-platform")}
    assert names == {"lima", "wsl2", "azure-vm", "proxmox"}
    row = registry.lookup("vm-platform", "azure-vm")
    assert row.origin is not None
    assert row.origin.variant == "built-in"
    assert row.description


def test_publisher_publishes_unsupported_platform_unconditionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R13: an installed platform whose host requirements aren't met (the
    platform's own ``unsupported_reason``) still publishes its capability
    row. Host support becomes the node's readiness (the fold), not its
    presence, so a site referencing it is not-ready rather than dangling
    on an absent row."""
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    monkeypatch.setattr(
        WSL2Platform,
        "unsupported_reason",
        classmethod(lambda cls: "Windows only"),
    )
    registry = Registry.empty()
    vm_platforms.publish_to(registry)
    names = {entry.name for entry in registry.iter_kind("vm-platform")}
    assert names == {"lima", "wsl2", "azure-vm", "proxmox"}


def test_vm_platform_is_not_manifest_declarable(tmp_path: Path) -> None:
    (tmp_path / "cap.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: vm-platform\nmetadata:\n  name: my-cloud\nspec: {}\n"
    )
    with pytest.raises(ConfigError, match="provided by the app"):
        load_manifests(tmp_path)
