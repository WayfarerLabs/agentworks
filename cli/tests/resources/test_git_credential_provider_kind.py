"""Tests for Phase 2b.1's ``git_credential_provider`` kind."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.git_credentials import PROVIDER_TYPES
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["git_credential_provider"]
    assert kind.kind == "git_credential_provider"
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["git_credential_provider"]
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
            """
            [git_credentials.gh]
            type = "github"
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    github = registry.lookup("git_credential_provider", "github")
    assert github.name == "github"
    assert github.origin.variant == "built-in"
    assert github.origin.source == "agentworks.git_credentials"


def test_unknown_provider_errors_with_framework_shape(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [git_credentials.bad]
            type = "gitlab"
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match=r"references unknown git_credential_provider 'gitlab'"):
        build_registry(cfg)


def test_publisher_publishes_full_known_set(tmp_path: Path) -> None:
    """Round-trip: every name in PROVIDER_TYPES lands in the registry
    even without any operator references.
    """
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    names = {r.name for r in registry.iter_kind("git_credential_provider")}
    assert names == set(PROVIDER_TYPES)
