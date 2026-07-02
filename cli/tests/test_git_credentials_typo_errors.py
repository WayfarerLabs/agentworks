"""Tests for ``GitCredentialKind``'s error miss policy: a typo'd or
undeclared git-credential name in ``admin.git_credentials`` or an
agent template surfaces as a clear ``ConfigError`` at config load
(via the framework's finalize pass), with the requirement source
named.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


def test_admin_referencing_undeclared_git_credential_errors_at_finalize(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.config]
        git_credentials = ["githb-prod"]
        claude_marketplaces = []
        claude_plugins = []
        """,
        ssh_keys,
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
        """\
        [agent_templates.claude]
        git_credentials = ["github-typo"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError) as exc:
        build_registry(config)
    assert "github-typo" in str(exc.value)
    assert "agent-template" in str(exc.value)


def test_declared_git_credential_does_not_error(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The positive case: a declared credential resolves cleanly
    through the framework.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [admin.config]
        git_credentials = ["github"]
        claude_marketplaces = []
        claude_plugins = []
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    # The git_credentials Resource is published and reachable.
    cred = registry.lookup("git-credential", "github")
    assert cred.name == "github"
