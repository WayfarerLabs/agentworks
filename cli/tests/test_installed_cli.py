"""Native entry points and packaged guide assets, exercised outside the source tree."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sysconfig
from pathlib import Path

import pytest

from tests.conftest import windows_home_env

pytestmark = pytest.mark.windows


def _run_installed(home: Path, entry_point: str, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {**os.environ, **windows_home_env(home)}
    environment.pop("AGW_DEBUG", None)
    environment.pop("PYTHONPATH", None)
    suffix = ".exe" if os.name == "nt" else ""
    executable = Path(sysconfig.get_path("scripts")) / f"{entry_point}{suffix}"
    return subprocess.run(
        [str(executable), *arguments],
        cwd=home,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("entry_point", ["agw", "agentworks"])
def test_installed_entry_points_report_distribution_version(tmp_path: Path, entry_point: str) -> None:
    result = _run_installed(tmp_path, entry_point, "--version")

    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert result.stdout.strip().decode("utf-8") == importlib.metadata.version("agentworks-cli")
    assert b"\r" not in result.stdout
    assert not list(tmp_path.iterdir())


def test_installed_guide_reads_packaged_assets_without_operator_state(tmp_path: Path) -> None:
    listing = _run_installed(tmp_path, "agw", "guide", "list")

    assert listing.returncode == 0, listing.stderr
    assert listing.stderr == b""
    assert b"\r" not in listing.stdout
    topics = listing.stdout.decode("utf-8").splitlines()
    assert topics
    rendered = _run_installed(tmp_path, "agw", "guide", "show", topics[0])

    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stderr == b""
    assert rendered.stdout.strip()
    assert b"\r" not in rendered.stdout
    assert not list(tmp_path.iterdir())
