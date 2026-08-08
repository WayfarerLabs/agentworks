"""The ``harness-integration`` capability kind: registration, the publisher's rows,
the capability-kind envelope rejection, and the operator inspection
surfaces (``resource list`` / ``kinds`` / ``describe``).

Mirrors ``test_git_credential_provider_kind.py``; the harness-integration kind is
the git-credential-provider kind's twin (``category="capability"``,
``miss_policy="error"``).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
from agentworks.config import load_config
from agentworks.errors import ConfigError, NotFoundError
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.resources.inspect import (
    describe_resource,
    list_kinds,
    list_resources,
)
from tests.conftest import write_cfg


def _write_cfg(path: Path) -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, filename=path.name)


def _write_manifest(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text))


# -- kind registration -------------------------------------------------------


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["harness-integration"]
    assert "harness" not in KIND_REGISTRY
    assert kind.category == "capability"
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None
    assert kind.builtin_override == "reserved"
    assert kind.description


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["harness-integration"]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


def test_capability_kind_envelope_rejection(tmp_path: Path) -> None:
    """``harness-integration`` is a capability descriptor kind; declaring it in a
    YAML manifest gets the permanent envelope error (it is provided by
    the app, not operator-declared)."""
    from agentworks.manifests import load_manifests

    root = tmp_path / "resources"
    _write_manifest(
        root,
        "a.yaml",
        """
        apiVersion: agentworks/v1
        kind: harness-integration
        metadata:
          name: shell
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="provided by the app"):
        load_manifests(root)


# -- the publisher's rows ----------------------------------------------------


def test_publisher_publishes_full_known_set(tmp_path: Path) -> None:
    """Round-trip: every registered harness integration lands in the registry even
    without any operator references."""
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    names = {r.name for r in registry.iter_kind("harness-integration")}
    assert names == set(HARNESS_INTEGRATION_REGISTRY)


def test_shell_row_carries_the_builtin_origin(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    shell = registry.lookup("harness-integration", "shell")
    assert shell.name == "shell"
    assert shell.origin.variant == "built-in"
    assert shell.origin.source == "agentworks.capabilities.harness_integration"


# -- the operator inspection surfaces ----------------------------------------


def test_resource_list_surfaces_the_shell_row(tmp_path: Path) -> None:
    """``shell`` is the only harness integration listed by default: ``claude-code`` and
    ``codex`` ship as opt-in system plugins, so their rows are
    present-but-disabled and hidden from the default list until enabled
    (``--include-disabled`` surfaces them)."""
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    listing = list_resources(registry, kinds=("harness-integration",))
    assert [(r.kind, r.name) for r in listing.rows] == [("harness-integration", "shell")]

    with_disabled = list_resources(registry, kinds=("harness-integration",), include_disabled=True)
    assert [(r.kind, r.name) for r in with_disabled.rows] == [
        ("harness-integration", "claude-code"),
        ("harness-integration", "codex"),
        ("harness-integration", "shell"),
    ]


def test_resource_kinds_lists_harness_integration(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    (row,) = [r for r in list_kinds(registry) if r.kind == "harness-integration"]
    assert row.category == "capability"
    assert row.resources == len(HARNESS_INTEGRATION_REGISTRY)
    assert row.description


def test_resource_describe_renders_the_shell_row(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    desc = describe_resource(registry, "harness-integration", "shell")
    assert desc.kind == "harness-integration"
    assert desc.name == "shell"
    assert desc.origin is not None
    assert desc.origin.variant == "built-in"


def test_old_kind_is_not_an_alias(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)

    with pytest.raises(NotFoundError, match="unknown kind 'harness'"):
        describe_resource(registry, "harness", "shell")
