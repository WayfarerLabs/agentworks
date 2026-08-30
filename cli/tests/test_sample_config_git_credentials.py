"""Tests for the ``git-credential`` manifest decoder's secret source handling.

config.toml is settings only now (ADR 0022): git-credentials are declared as
``resources/*.yaml`` manifests, and the decode + auto-declare that used to run
at ``load_config`` now runs at ``build_registry`` (manifest decode + finalize).
The source lives inside the manifest's ``provider`` table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from pathlib import Path


def _write_cfg(tmp_path: Path, *docs: ManifestDoc | str) -> Path:
    """``write_cfg`` under this file's varargs spelling."""
    return write_cfg(tmp_path, *docs, filename="c.toml")


def test_default_token_secret_auto_declares(tmp_path: Path) -> None:
    """A secret source with no inner reference defaults to ``git-token-github``; the
    framework's finalize pass auto-declares that secret via
    ``GitCredentialConfig.dependencies``.
    """
    cfg = _write_cfg(
        tmp_path,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "source": {"mode": "secret"}}}),
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    assert registry.lookup("git-credential", "github").provider.config["source"] == {"mode": "secret"}

    decl = registry.lookup("secret", "git-token-github")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("git-credential", "github")


def test_custom_token_secret_auto_declares(tmp_path: Path) -> None:
    """An explicit inner secret reference overrides
    the default secret name; auto-declare uses the custom name.
    """
    cfg = _write_cfg(
        tmp_path,
        ManifestDoc(
            "git-credential",
            "github",
            {"provider": {"name": "github", "source": {"mode": "secret", "secret": "custom-tok"}}},
        ),
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    assert registry.lookup("git-credential", "github").provider.config["source"]["secret"] == "custom-tok"

    decl = registry.lookup("secret", "custom-tok")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"


def test_empty_secret_name_rejected(tmp_path: Path) -> None:
    """An empty secret name is a usability footgun (would
    derive ``AW_SECRET_`` env-var name and prompt for a secret called
    ``""``); the decoder rejects it explicitly at build.
    """
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        ManifestDoc(
            "git-credential",
            "github",
            {"provider": {"name": "github", "source": {"mode": "secret", "secret": ""}}},
        ),
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="source.secret: must not be empty"):
        build_registry(config)


def test_a_source_table_requires_its_union_tag(tmp_path: Path) -> None:
    """The source is a real tagged union, so an untagged
    table is refused rather than inferred as the sole arm."""
    from agentworks.errors import ConfigError

    cfg = _write_cfg(
        tmp_path,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "source": {"secret": "x"}}}),
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError):
        build_registry(config)
