"""Behavioral tests for convergent Git configuration updates."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agentworks.git_config import ensure_safe_directory_wildcard


class _LocalGitTarget:
    def __init__(self, config_path: Path) -> None:
        self._env = os.environ | {"GIT_CONFIG_GLOBAL": str(config_path)}

    def run(self, command: str) -> None:
        subprocess.run(command, shell=True, check=True, env=self._env)


def _add_safe_directory(config_path: Path, value: str) -> None:
    subprocess.run(
        ["git", "config", "--file", str(config_path), "--add", "safe.directory", value],
        check=True,
    )


def _safe_directories(config_path: Path) -> list[str]:
    return subprocess.run(
        ["git", "config", "--file", str(config_path), "--get-all", "safe.directory"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def test_safe_directory_wildcard_repairs_duplicates_without_replacing_other_values(tmp_path: Path) -> None:
    config_path = tmp_path / "gitconfig"
    target = _LocalGitTarget(config_path)
    _add_safe_directory(config_path, "/operator/repo")
    _add_safe_directory(config_path, "*")
    _add_safe_directory(config_path, "*")

    ensure_safe_directory_wildcard(target)
    ensure_safe_directory_wildcard(target)

    assert _safe_directories(config_path) == ["/operator/repo", "*"]


def test_safe_directory_wildcard_is_added_when_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "gitconfig"
    target = _LocalGitTarget(config_path)

    ensure_safe_directory_wildcard(target)
    ensure_safe_directory_wildcard(target)

    assert _safe_directories(config_path) == ["*"]
