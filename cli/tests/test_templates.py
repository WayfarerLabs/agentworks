"""Tests for workspace template resolution."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.workspaces.templates import resolve_template


@pytest.fixture()
def config(tmp_path: Path):  # type: ignore[no-untyped-def]
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [workspace_templates.default]

        [workspace_templates.base]
        repo = "https://example.com/org/base.git"

        [workspace_templates.child]
        inherits = ["base"]
        tmuxinator = false

        [workspace_templates.grandchild]
        inherits = ["child"]
        repo = "https://example.com/org/override.git"
    """)
    )
    return load_config(config_file)


def test_explicit_template(config):  # type: ignore[no-untyped-def]
    result = resolve_template(build_registry(config), "base")
    assert result.name == "base"
    assert result.repo == "https://example.com/org/base.git"
    assert result.tmuxinator is True


def test_default_template(config):  # type: ignore[no-untyped-def]
    result = resolve_template(build_registry(config))
    assert result.name == "default"
    assert result.repo is None
    assert result.tmuxinator is True


def test_inheritance_overrides(config):  # type: ignore[no-untyped-def]
    result = resolve_template(build_registry(config), "child")
    assert result.name == "child"
    assert result.repo == "https://example.com/org/base.git"  # inherited from base
    assert result.tmuxinator is False  # overridden by child


def test_deep_inheritance(config):  # type: ignore[no-untyped-def]
    result = resolve_template(build_registry(config), "grandchild")
    assert result.name == "grandchild"
    assert result.repo == "https://example.com/org/override.git"  # overridden
    assert result.tmuxinator is False  # inherited from child


def test_builtin_fallback(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    cfg = load_config(config_file)
    result = resolve_template(build_registry(cfg))
    assert result.name == "default"
    assert result.repo is None
    assert result.tmuxinator is True


def test_unknown_template(config):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="Unknown"):
        resolve_template(build_registry(config), "nonexistent")


def _identity_config(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body)
    )
    return config_file


def test_git_identity_resolves(tmp_path: Path) -> None:
    cfg = load_config(
        _identity_config(
            tmp_path,
            """
            [workspace_templates.default]
            git_user_name = "Ada Lovelace"
            git_user_email = "ada@example.com"
            """,
        )
    )
    result = resolve_template(build_registry(cfg))
    assert result.git_user_name == "Ada Lovelace"
    assert result.git_user_email == "ada@example.com"


def test_git_identity_defaults_to_none(config):  # type: ignore[no-untyped-def]
    result = resolve_template(build_registry(config), "base")
    assert result.git_user_name is None
    assert result.git_user_email is None


def test_git_identity_inherits_and_overrides(tmp_path: Path) -> None:
    cfg = load_config(
        _identity_config(
            tmp_path,
            """
            [workspace_templates.base]
            git_user_name = "Base Bot"
            git_user_email = "base@example.com"

            [workspace_templates.child]
            inherits = ["base"]
            git_user_email = "child@example.com"
            """,
        )
    )
    registry = build_registry(cfg)
    result = resolve_template(registry, "child")
    assert result.git_user_name == "Base Bot"  # inherited
    assert result.git_user_email == "child@example.com"  # overridden


def test_unknown_workspace_template_key_warns(tmp_path: Path) -> None:
    cfg = load_config(
        _identity_config(
            tmp_path,
            """
            [workspace_templates.default]
            git_user_emial = "typo@example.com"
            """,
        ),
        warn_issues=False,
    )
    assert any(
        "workspace_templates.default" in issue and "git_user_emial" in issue
        for issue in cfg.config_issues
    )
