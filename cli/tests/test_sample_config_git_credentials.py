"""Tests that the updated sample-config.toml parses cleanly through the
framework's finalize pass and that each ``[git_credentials.<name>]``
stanza's token secret auto-declares (or stays at its default
``git-token-<name>`` name when the operator doesn't override).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config


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


def test_default_token_secret_auto_declares(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A bare ``[git_credentials.github]`` stanza (no ``token`` field)
    parses with the default ``token = "git-token-github"``; the
    framework's finalize pass auto-declares that secret via
    ``GitCredentialConfig.referenced_resources``.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.git_credentials["github"].token == "git-token-github"

    registry = build_registry(config)
    decl = registry.lookup("secret", "git-token-github")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("git-credential", "github")


def test_custom_token_secret_auto_declares(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An operator-typed ``token = "custom"`` overrides the default
    secret name; auto-declare uses the custom name.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"
        token = "custom-tok"
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.git_credentials["github"].token == "custom-tok"

    registry = build_registry(config)
    decl = registry.lookup("secret", "custom-tok")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"


def test_empty_token_string_rejected_at_load(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An empty-string ``token = ""`` is a usability footgun (would
    derive ``AW_SECRET_`` env-var name and prompt for a secret called
    ``""``); the loader rejects it explicitly.
    """
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"
        token = ""
        """,
        ssh_keys,
    )
    with pytest.raises(ConfigError, match="token must not be empty"):
        load_config(cfg, warn_issues=False)


def test_non_string_token_rejected_at_load(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """``token`` must be a bare string; the loader rejects inline
    tables (``{ secret = "..." }`` polymorphism not permitted).
    """
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"
        token = { secret = "x" }
        """,
        ssh_keys,
    )
    with pytest.raises(ConfigError, match="must be a bare secret"):
        load_config(cfg, warn_issues=False)
