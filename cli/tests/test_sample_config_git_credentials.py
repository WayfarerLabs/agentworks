"""Tests for the ``git-credential`` manifest decoder's token handling: the
token secret auto-declares (or stays at its default ``git-token-<name>`` name
when the operator doesn't override), and bad token shapes are rejected.

config.toml is settings only now (ADR 0022): git-credentials are declared as
``resources/*.yaml`` manifests, and the decode + auto-declare that used to run
at ``load_config`` now runs at ``build_registry`` (manifest decode + finalize).
The token lives inside the manifest's ``provider`` table, not a flat ``token``
key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
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
    *docs: ManifestDoc | str,
) -> Path:
    """Write a settings-only config.toml plus its git-credential manifests and
    return the config path."""
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n')
    if docs:
        write_manifests(tmp_path, *docs)
    return p


def test_default_token_secret_auto_declares(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """A bare ``git-credential/github`` manifest (no ``token`` in the provider
    table) decodes with the default ``token = "git-token-github"``; the
    framework's finalize pass auto-declares that secret via
    ``GitCredentialConfig.dependencies``.
    """
    cfg = _write_cfg(tmp_path, ssh_keys, ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}))
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    # No token in provider_config -> the provider defaults the secret.
    assert "token" not in registry.lookup("git-credential", "github").provider_config

    decl = registry.lookup("secret", "git-token-github")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("git-credential", "github")


def test_custom_token_secret_auto_declares(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """An operator-typed ``token = "custom"`` in the provider table overrides
    the default secret name; auto-declare uses the custom name.
    """
    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "token": "custom-tok"}}),
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    assert registry.lookup("git-credential", "github").provider_config["token"] == "custom-tok"

    decl = registry.lookup("secret", "custom-tok")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"


def test_empty_token_string_rejected(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """An empty-string ``token = ""`` is a usability footgun (would
    derive ``AW_SECRET_`` env-var name and prompt for a secret called
    ``""``); the decoder rejects it explicitly at build.
    """
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "token": ""}}),
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="token must not be empty"):
        build_registry(config)


def test_non_string_token_rejected(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """``token`` must be a bare string; the decoder rejects inline
    tables (``{ secret = "..." }`` polymorphism not permitted).
    """
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        ssh_keys,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "token": {"secret": "x"}}}),
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="must be a bare secret"):
        build_registry(config)
