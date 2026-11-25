"""Behavioral tests for convergent Git configuration updates, run as an account on this machine (the L lane)."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.git_config import ensure_safe_directory_wildcard
from tests.vm_access_support import account_shell, local_vm_access

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.files import Files

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the L lane runs agw-launch, a Linux guest program, on this machine"
)


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Files:
    return local_vm_access(monkeypatch, home=tmp_path, shell=account_shell(tmp_path)).files(deadline=60)


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


def test_safe_directory_wildcard_repairs_duplicates_without_replacing_other_values(
    files: Files, tmp_path: Path
) -> None:
    config_path = tmp_path / ".gitconfig"
    _add_safe_directory(config_path, "/operator/repo")
    _add_safe_directory(config_path, "*")
    _add_safe_directory(config_path, "*")

    ensure_safe_directory_wildcard(files)
    ensure_safe_directory_wildcard(files)

    assert _safe_directories(config_path) == ["/operator/repo", "*"]


def test_safe_directory_wildcard_is_added_when_absent(files: Files, tmp_path: Path) -> None:
    ensure_safe_directory_wildcard(files)
    ensure_safe_directory_wildcard(files)

    assert _safe_directories(tmp_path / ".gitconfig") == ["*"]
