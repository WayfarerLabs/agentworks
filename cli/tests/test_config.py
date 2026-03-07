"""Tests for config loading and validation."""

from __future__ import annotations

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
        shell = "zsh"

        [vm.config]
        apt = ["zsh", "tmux"]
        install_commands = ["echo hello"]

        [workspace_templates.default]

        [workspace_templates.gruntweave]
        repo = "git@example.com:org/repo.git"

        [workspace_templates.child]
        inherits = ["gruntweave"]
        tmuxinator = false

        [git_hosts.github]
        type = "github"

        [git_hosts.azdo]
        type = "azdo"
        org = "my-org"

        [defaults]
        git_hosts = ["github"]
    """))
    return config_file


def test_load_valid_config(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.user.shell == "zsh"
    assert cfg.vm.apt == ["zsh", "tmux"]
    assert cfg.vm.install_commands == ["echo hello"]
    assert "default" in cfg.workspace_templates
    assert "gruntweave" in cfg.workspace_templates
    assert cfg.workspace_templates["child"].inherits == ["gruntweave"]
    assert cfg.workspace_templates["child"].tmuxinator is False
    assert cfg.git_hosts["github"].type == "github"
    assert cfg.git_hosts["azdo"].org == "my-org"
    assert cfg.defaults.git_hosts == ["github"]


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


def test_invalid_git_host_type(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [user]
        ssh_public_key = "{pub}"
        ssh_private_key = "{priv}"

        [git_hosts.bad]
        type = "gitlab"
    """))
    with pytest.raises(ConfigError, match="git_hosts.bad.type"):
        load_config(config_file)
