"""Tests for ``paths.vm_workspaces`` placement validation (issue #231).

A workspace tree under ``/home`` would collide with the Linux user-home
namespace and force the 0750 admin/agent homes back to world-traversable, so
config load rejects it. The unit cases exercise
``validate_vm_workspaces`` directly (rejects at/under ``/home``, accepts
siblings that merely share the prefix characters), and one case drives the real
``load_config`` -> ``_load_paths`` path to prove the guard is wired in.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import ConfigError, load_config
from agentworks.config.validation import validate_vm_workspaces


@pytest.mark.parametrize(
    "path",
    [
        "/home",
        "/home/",
        "/home//",
        "/home/foo",
        "/home/foo/bar",
        "/home/foo/",
        "/home/./foo",
    ],
)
def test_rejects_home_paths(path: str) -> None:
    """A normalized path equal to ``/home`` or under ``/home/`` is rejected."""
    with pytest.raises(ConfigError, match="must not be at or under /home"):
        validate_vm_workspaces(path)


@pytest.mark.parametrize(
    "path",
    [
        "/opt/agentworks/workspaces",
        "/srv/ws",
        "/homelab/ws",
        "/home2/ws",
        "/mnt/data/workspaces",
    ],
)
def test_accepts_non_home_paths(path: str) -> None:
    """Siblings that merely start with the ``/home`` characters (``/homelab``,
    ``/home2``) and any path outside ``/home`` are accepted."""
    validate_vm_workspaces(path)


def _write_config(tmp_path: Path, vm_workspaces: str) -> Path:
    """Minimal valid config with a ``[paths]`` table for the load-path test."""
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [paths]
        vm_workspaces = "{vm_workspaces}"
        """)
    )
    return config_file


def test_load_config_rejects_home_vm_workspaces(tmp_path: Path) -> None:
    """The guard fires through the real ``load_config`` -> ``_load_paths`` path,
    not just when called directly."""
    config_file = _write_config(tmp_path, "/home/agentworks/ws")
    with pytest.raises(ConfigError, match="must not be at or under /home"):
        load_config(config_file)


def test_load_config_accepts_non_home_vm_workspaces(tmp_path: Path) -> None:
    """A valid data-volume path loads cleanly and round-trips."""
    config_file = _write_config(tmp_path, "/srv/ws")
    cfg = load_config(config_file)
    assert cfg.paths.vm_workspaces == "/srv/ws"
