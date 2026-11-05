"""Programs started by name never come from the current directory: only an absolute PATH element names them."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import AgentworksError
from agentworks.programs import find_program, resolve_program

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.windows

WINDOWS = sys.platform == "win32"


def _program_file(directory: Path, name: str) -> Path:
    path = directory / (f"{name}.exe" if WINDOWS else name)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_a_program_only_in_the_current_directory_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows searches the current directory first, and an empty or relative PATH element means it on POSIX."""
    _program_file(tmp_path, "tailscale")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative-bin"]))

    with pytest.raises(FileNotFoundError):
        resolve_program("tailscale")


def test_the_first_absolute_path_element_holding_the_program_names_it(tmp_path: Path) -> None:
    decoy_dir, empty, real = tmp_path / "cwd", tmp_path / "empty", tmp_path / "bin"
    for directory in (decoy_dir, empty, real):
        directory.mkdir()
    _program_file(decoy_dir, "tailscale")
    genuine = _program_file(real, "tailscale")

    found = find_program("tailscale", search_path=os.pathsep.join(["", str(empty), str(real)]), windows=WINDOWS)

    assert found == genuine


# -- Every site that starts one -------------------------------------------------------------------


def _preflight() -> None:
    from agentworks.vms.initializer.tailscale_preflight import verify_tailscale_available

    verify_tailscale_available()


def _reachability() -> None:
    from agentworks.vms.manager.tailscale import _is_tailscale_reachable

    _is_tailscale_reachable("100.64.0.1")


def _doctor() -> None:
    from agentworks.doctor import _check_tailscale

    _check_tailscale()


def _profile_query() -> None:
    from agentworks.completions.install import _query_powershell_profile

    _query_powershell_profile()


def _wsl_powershell() -> None:
    from agentworks.capabilities.vm_platform.wsl2 import _powershell

    _powershell("$PROFILE")


@pytest.mark.skipif(WINDOWS, reason="the decoy is a shell script, which only POSIX runs by name")
@pytest.mark.parametrize(
    ("programs", "site"),
    [
        (("tailscale",), _preflight),
        (("tailscale",), _reachability),
        (("tailscale",), _doctor),
        (("pwsh", "powershell"), _profile_query),
        (("powershell",), _wsl_powershell),
    ],
    ids=["preflight", "reachability", "doctor", "completion-profile", "wsl2-powershell"],
)
def test_no_site_runs_a_decoy_from_the_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    programs: tuple[str, ...],
    site: Callable[[], None],
) -> None:
    """Each decoy would leave a marker; PATH's empty element is what a bare name's lookup reads as the current one."""
    marker = tmp_path / "decoy-ran"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for name in programs:
        decoy = cwd / name
        decoy.write_text(f"#!/bin/sh\n: > '{marker}'\n")  # a builtin: PATH holds no touch
        decoy.chmod(stat.S_IRWXU)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep + str(tmp_path / "no-such-bin"))

    # A missing program is the expected outcome; only whether the decoy ran matters.
    with contextlib.suppress(AgentworksError, OSError):
        site()

    assert not marker.exists()
