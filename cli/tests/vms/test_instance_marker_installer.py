"""Behavioral coverage for the creation-only VM marker installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.bootstrap_script import _render_instance_marker_installer

_MARKER = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _require_rootless_linux_namespace() -> None:
    if sys.platform != "linux" or shutil.which("unshare") is None:
        pytest.skip("marker installer behavior test requires Linux unshare")
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("marker installer behavior test requires an unprivileged user namespace")


def _install(marker_path: Path) -> subprocess.CompletedProcess[str]:
    script = _render_instance_marker_installer(instance_marker=_MARKER, marker_path=str(marker_path))
    return subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "/bin/bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_writes_exact_marker_and_replaces_ordinary_clone(tmp_path: Path) -> None:
    marker_path = tmp_path / "agentworks" / "instance-id"
    marker_path.parent.mkdir()
    marker_path.write_text("template-marker\n")
    marker_path.chmod(0o666)

    result = _install(marker_path)

    assert result.returncode == 0, result.stderr
    assert marker_path.read_bytes() == (_MARKER + "\n").encode()
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(marker_path.parent.stat().st_mode) == 0o755


def test_installer_creates_missing_parent_and_marker_leaf(tmp_path: Path) -> None:
    marker_path = tmp_path / "agentworks" / "instance-id"

    result = _install(marker_path)

    assert result.returncode == 0, result.stderr
    assert marker_path.read_bytes() == (_MARKER + "\n").encode()
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(marker_path.parent.stat().st_mode) == 0o755


def test_installer_fails_when_the_fixed_parent_cannot_be_created() -> None:
    result = _install(Path("/proc/agentworks-instance-marker-test/instance-id"))

    assert result.returncode != 0


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "directory"))
def test_installer_refuses_unsafe_marker_leaf(tmp_path: Path, kind: str) -> None:
    marker_path = tmp_path / "agentworks" / "instance-id"
    marker_path.parent.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    if kind == "symlink":
        marker_path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, marker_path)
    elif kind == "fifo":
        os.mkfifo(marker_path)
    else:
        marker_path.mkdir()

    result = _install(marker_path)

    assert result.returncode != 0
    assert outside.read_bytes() == b"outside"
    if kind == "hardlink":
        assert marker_path.stat().st_nlink == 2
    elif kind == "fifo":
        assert stat.S_ISFIFO(marker_path.stat().st_mode)
    elif kind == "directory":
        assert marker_path.is_dir()
    else:
        assert marker_path.is_symlink()


@pytest.mark.parametrize("kind", ("symlink", "file"))
def test_installer_refuses_unsafe_marker_parent(tmp_path: Path, kind: str) -> None:
    parent = tmp_path / "agentworks"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "instance-id"
    sentinel.write_bytes(b"outside")
    if kind == "symlink":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.write_bytes(b"not-a-directory")

    result = _install(parent / "instance-id")

    assert result.returncode != 0
    assert sentinel.read_bytes() == b"outside"
