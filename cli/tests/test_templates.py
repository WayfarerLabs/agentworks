"""Tests for workspace template resolution."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config
from agentworks.workspaces.templates import resolve_template


@pytest.fixture()
def config(tmp_path: Path):  # type: ignore[no-untyped-def]
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        [workspace_templates.default]

        [workspace_templates.base]
        repo = "https://example.com/org/base.git"

        [workspace_templates.child]
        inherits = ["base"]
        tmuxinator = false

        [workspace_templates.grandchild]
        inherits = ["child"]
        repo = "https://example.com/org/override.git"
    """))
    return load_config(config_file)


def test_explicit_template(config):  # type: ignore[no-untyped-def]
    result = resolve_template(config, "base")
    assert result.name == "base"
    assert result.repo == "https://example.com/org/base.git"
    assert result.tmuxinator is True


def test_default_template(config):  # type: ignore[no-untyped-def]
    result = resolve_template(config)
    assert result.name == "default"
    assert result.repo is None
    assert result.tmuxinator is True


def test_inheritance_overrides(config):  # type: ignore[no-untyped-def]
    result = resolve_template(config, "child")
    assert result.name == "child"
    assert result.repo == "https://example.com/org/base.git"  # inherited from base
    assert result.tmuxinator is False  # overridden by child


def test_deep_inheritance(config):  # type: ignore[no-untyped-def]
    result = resolve_template(config, "grandchild")
    assert result.name == "grandchild"
    assert result.repo == "https://example.com/org/override.git"  # overridden
    assert result.tmuxinator is False  # inherited from child


def test_builtin_fallback(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"
    """))
    cfg = load_config(config_file)
    result = resolve_template(cfg)
    assert result.name == "default"
    assert result.repo is None
    assert result.tmuxinator is True


def test_unknown_template(config):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="Unknown"):
        resolve_template(config, "nonexistent")
