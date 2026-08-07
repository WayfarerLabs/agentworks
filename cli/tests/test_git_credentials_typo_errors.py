"""Tests for ``GitCredentialKind``'s error miss policy: a typo'd or
undeclared git-credential name in ``admin.git_credentials`` or an
agent template surfaces as a clear ``ConfigError`` at config load
(via the framework's finalize pass), with the requirement source
named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    *,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` under this file's keyword spelling. ``ssh_keys`` is
    accepted and ignored; the shared helper writes the keypair itself."""
    return write_cfg(tmp_path, *manifests, filename="c.toml")


def test_admin_referencing_undeclared_git_credential_errors_at_finalize(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        manifests=[
            ManifestDoc(
                "admin-template",
                "default",
                {"git_credentials": ["githb-prod"], "claude_marketplaces": [], "claude_plugins": []},
            )
        ],
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError) as exc:
        build_registry(config)
    # The error must name the typo'd credential and the source
    # (admin-template:default) so operators can find the offending line.
    assert "githb-prod" in str(exc.value)
    assert "git-credential" in str(exc.value)
    assert "admin-template" in str(exc.value)


def test_agent_template_referencing_undeclared_git_credential_errors(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        manifests=[ManifestDoc("agent-template", "claude", {"git_credentials": ["github-typo"]})],
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError) as exc:
        build_registry(config)
    assert "github-typo" in str(exc.value)
    assert "agent-template" in str(exc.value)


def test_declared_git_credential_does_not_error(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """The positive case: a declared credential resolves cleanly
    through the framework.
    """
    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        manifests=[
            ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}),
            ManifestDoc(
                "admin-template",
                "default",
                {"git_credentials": ["github"], "claude_marketplaces": [], "claude_plugins": []},
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    # The git_credentials Resource is published and reachable.
    cred = registry.lookup("git-credential", "github")
    assert cred.name == "github"
