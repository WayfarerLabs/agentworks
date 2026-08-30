"""Tests for Phase 2b.1's ``git-credential-provider`` kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError
from tests.conftest import ManifestDoc, write_cfg


def _write_cfg(path: Path, *manifests: ManifestDoc) -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, *manifests, filename=path.name)


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["git-credential-provider"]
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["git-credential-provider"]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


def test_known_providers_resolve(tmp_path: Path) -> None:
    """``type = "github"`` or ``type = "azdo"`` finalize cleanly because
    the publisher (``agentworks.git_credentials.publish_to``) seeded
    rows for both before Config.publish_to runs.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            ManifestDoc("git-credential", "gh", {"provider": {"name": "github", "source": {"mode": "secret"}}}),
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    github = registry.lookup("git-credential-provider", "github")
    assert github.name == "github"
    assert github.origin.variant == "built-in"
    assert github.origin.source == "agentworks.capabilities.git_credential"


def test_unknown_provider_errors_with_framework_shape(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            ManifestDoc("git-credential", "bad", {"provider": {"name": "gitlab"}}),
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match=r"references unknown git-credential-provider 'gitlab'"):
        build_registry(cfg)


def test_publisher_publishes_full_known_set(tmp_path: Path) -> None:
    """Round-trip: every registered provider lands in the registry
    even without any operator references.
    """
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    names = {r.name for r in registry.iter_kind("git-credential-provider")}
    assert names == set(GIT_CREDENTIAL_PROVIDER_REGISTRY)
