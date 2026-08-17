"""The vm-platform capability kind: read-only rows, not declarable."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.publish import publish_capability_rows
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


def test_publisher_adds_one_row_per_core_built_in_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core ``publish_to`` publishes one built-in row per core platform.
    ``proxmox`` and ``azure-vm`` are INSTALLED (re-seated into
    ``VM_PLATFORM_REGISTRY`` by their opt-in plugins) but their rows are
    published by ``plugins.publish_plugins`` with a ``system-plugin`` origin, so
    the core publisher skips them to avoid a built-in-vs-system-plugin collision
    at ``Registry.add``."""
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    publish_capability_rows(registry, descriptor_for("vm-platform"))
    names = {entry.name for entry in registry.iter_kind("vm-platform")}
    assert names == {"lima", "wsl2"}
    row = registry.lookup("vm-platform", "lima")
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
    publish_capability_rows(registry, descriptor_for("vm-platform"))
    names = {entry.name for entry in registry.iter_kind("vm-platform")}
    # Core built-ins only; proxmox's and azure-vm's rows come from their plugins.
    assert names == {"lima", "wsl2"}


def test_vm_platform_is_not_manifest_declarable(tmp_path: Path) -> None:
    (tmp_path / "cap.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: vm-platform\nmetadata:\n  name: my-cloud\nspec: {}\n"
    )
    with pytest.raises(ConfigError):
        load_manifests(tmp_path)
