"""Tests for config loading and validation."""

from __future__ import annotations

import warnings
from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import ConfigError, load_config


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    # Create fake SSH keys
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        [vm_templates.default]
        apt = ["zsh", "tmux"]

        [admin.config]
        shell = "zsh"
        user_install_commands = ["hello"]

        [user_install_commands.hello]
        command = "echo hello"
        path = ["~/.local/bin"]

        [workspace_templates.default]

        [workspace_templates.gruntweave]
        repo = "https://example.com/org/repo.git"

        [workspace_templates.child]
        inherits = ["gruntweave"]
        tmuxinator = false

        [git_credentials.github]
        type = "github"

        [git_credentials.azdo]
        type = "azdo"
        org = "my-org"

        [defaults]
        git_credentials = ["github"]
    """))
    return config_file


def test_load_valid_config(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.admin.shell == "zsh"
    assert cfg.vm.apt == ["zsh", "tmux"]
    assert cfg.admin.user_install_commands == ["hello"]
    assert "hello" in cfg.user_install_commands
    assert "default" in cfg.workspace_templates
    assert "gruntweave" in cfg.workspace_templates
    assert cfg.workspace_templates["child"].inherits == ["gruntweave"]
    assert cfg.workspace_templates["child"].tmuxinator is False
    assert cfg.git_credentials["github"].type == "github"
    assert cfg.git_credentials["azdo"].org == "my-org"
    assert cfg.defaults.git_credentials == ["github"]


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")


def test_cycle_detection(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        [workspace_templates.a]
        inherits = ["b"]

        [workspace_templates.b]
        inherits = ["a"]
    """))
    with pytest.raises(ConfigError, match="cycle"):
        load_config(config_file)


def test_invalid_git_credential_type(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        [git_credentials.bad]
        type = "gitlab"
    """))
    with pytest.raises(ConfigError, match="git_credentials.bad.type"):
        load_config(config_file)


def test_unexpected_top_level_keys_warns(tmp_path: Path) -> None:
    """Bare keys before any section header land at top level."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    # 'oops' appears before any [section] header
    config_file.write_text(dedent(f"""\
        oops = true

        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"
    """))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        load_config(config_file)
        assert len(w) == 1
        assert "oops" in str(w[0].message)


def test_orphaned_key_under_commented_section(tmp_path: Path) -> None:
    """Keys under commented-out section headers land in the previous section."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        # [defaults]          <-- commented out!
        platform = "lima"     # orphaned in [user], not [defaults]
    """))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = load_config(config_file)
        assert len(w) == 1
        assert "platform" in str(w[0].message)
        assert "user" in str(w[0].message).lower()
    # The orphaned key means defaults.platform stays at default (None)
    assert cfg.defaults.platform is None


def test_extra_ssh_public_keys(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    extra1 = tmp_path / "extra1.pub"
    extra2 = tmp_path / "extra2.pub"
    pub.write_text("ssh-ed25519 AAAA-primary")
    priv.write_text("key")
    extra1.write_text("ssh-ed25519 AAAA-extra1")
    extra2.write_text("ssh-rsa BBBB-extra2")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"
        extra_ssh_public_keys = ["{extra1}", "{extra2}"]
    """))
    cfg = load_config(config_file)
    assert len(cfg.user.extra_ssh_public_keys) == 2
    assert cfg.user.extra_ssh_public_keys[0] == extra1
    assert cfg.user.extra_ssh_public_keys[1] == extra2


def test_extra_ssh_public_keys_missing_file(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"
        extra_ssh_public_keys = ["/nonexistent/key.pub"]
    """))
    with pytest.raises(ConfigError, match="extra_ssh_public_keys.*does not exist"):
        load_config(config_file)


def test_extra_ssh_public_keys_defaults_empty(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.user.extra_ssh_public_keys == []
