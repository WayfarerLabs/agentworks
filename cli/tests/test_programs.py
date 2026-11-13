"""Programs started by name never come from the current directory: only an absolute PATH element names them."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import AgentworksError
from agentworks.programs import find_program, resolve_program

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

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


def test_a_name_with_a_directory_part_is_already_a_path(tmp_path: Path) -> None:
    """``EDITOR=/opt/bin/vim`` names its program outright, as a shell would run it."""
    path = str(tmp_path / "bin" / "vim")

    assert resolve_program(path) == path


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


def _onepassword_read() -> None:
    from agentworks.plugins.onepassword.backend import _bounded_read

    _bounded_read(["read", "--no-newline", "op://Work/item/password"], timeout=5)


def _git_capture() -> None:
    from agentworks.package_sources import PackageCapture

    with PackageCapture() as capture:
        capture._run(capture.root, "init", "--bare")


def _taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from agentworks.package_sources import _kill_process_tree

    monkeypatch.setattr(sys, "platform", "win32")
    _kill_process_tree(SimpleNamespace(pid=4242, kill=lambda: None))  # type: ignore[arg-type]


def _vscode(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.workspaces.manager.create import _open_in_vscode

    monkeypatch.setattr(sys, "platform", "linux")  # the platforms agw starts VS Code on
    _open_in_vscode(Path("workspace.code-workspace"))


def _legacy_ssh() -> None:
    from agentworks.execution.trust import HOST_TRUST
    from agentworks.transports import SSHTransport

    SSHTransport(trust=HOST_TRUST, host="vm1", user="agentworks").run("true", check=False)


def _legacy_scp() -> None:
    from agentworks.execution.trust import HOST_TRUST
    from agentworks.transports import SSHTransport

    SSHTransport(trust=HOST_TRUST, host="vm1", user="agentworks").copy_to(__file__, "/tmp/copy")


def _local_limactl() -> None:
    from agentworks.capabilities.vm_platform.lima_host import LimaHost

    LimaHost(None).run("limactl list", check=False)


SITES: list[tuple[tuple[str, ...], Callable[..., None]]] = [
    (("tailscale",), _preflight),
    (("tailscale",), _reachability),
    (("tailscale",), _doctor),
    (("pwsh", "powershell"), _profile_query),
    (("powershell",), _wsl_powershell),
    (("op",), _onepassword_read),
    (("git",), _git_capture),
    (("taskkill",), _taskkill),
    (("code",), _vscode),
    (("ssh",), _legacy_ssh),
    (("scp",), _legacy_scp),
    (("limactl",), _local_limactl),
]
SITE_IDS = [
    "preflight",
    "reachability",
    "doctor",
    "completion-profile",
    "wsl2-powershell",
    "onepassword",
    "git",
    "taskkill",
    "vscode",
    "legacy-ssh",
    "legacy-scp",
    "local-limactl",
]


def _plant(directory: Path, names: tuple[str, ...]) -> None:
    """Each program under both its POSIX name and its Windows one, so whichever the lookup asks for is there."""
    directory.mkdir()
    for name in names:
        for filename in (name, f"{name}.exe"):
            path = directory / filename
            path.write_text("")
            path.chmod(stat.S_IRWXU)


@pytest.mark.parametrize(("programs", "site"), SITES, ids=SITE_IDS)
def test_no_site_starts_a_decoy_from_the_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    programs: tuple[str, ...],
    site: Callable[..., None],
) -> None:
    """A decoy waits in the current directory, which the empty and ``.`` elements of ``PATH`` also name.

    Each start is recorded instead of run, so the test reads the same on
    every platform: the program it names must be the genuine one, by an
    absolute path, since a bare name is what the system would look up in the
    current directory first.
    """
    cwd, genuine = tmp_path / "cwd", tmp_path / "bin"
    _plant(cwd, programs)
    _plant(genuine, programs)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(genuine)]))
    started: list[str] = []

    def record(args: list[str], *_args: object, **_kwargs: object) -> None:
        started.append(str(args[0]))
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "Popen", record)

    # Whether the site copes with a program that cannot start is not the question here.
    with contextlib.suppress(AgentworksError, OSError):
        if site is _taskkill or site is _vscode:
            site(monkeypatch)
        else:
            site()

    assert started
    assert all(Path(program).parent == genuine for program in started), started


def test_vscode_is_not_started_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Its ``code`` is a batch file, which only cmd.exe can start, parsing the workspace path by its own rules."""
    from agentworks.workspaces.manager.create import _open_in_vscode

    _plant(tmp_path / "bin", ("code",))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("VS Code was started"))

    _open_in_vscode(tmp_path / "project.code-workspace")

    (warning,) = captured_output.warnings
    assert "project.code-workspace" in warning  # names the file to open instead


def _onepassword_ready() -> bool:
    from agentworks.plugins.onepassword.backend import OnePasswordBackend

    return OnePasswordBackend.backend_readiness().is_ready


def _powershell_available() -> bool:
    from agentworks.doctor import _shell_available

    return _shell_available("powershell")


def _local_lima_ready() -> bool:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    return LimaPlatform.not_ready({"placement": {"mode": "local"}}).is_ready


def _remote_lima_ready() -> bool:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    return LimaPlatform.not_ready({"placement": {"mode": "ssh", "host": "box"}}).is_ready


@pytest.mark.parametrize(
    ("names", "installed"),
    [
        (("op",), _onepassword_ready),
        (("pwsh", "powershell"), _powershell_available),
        (("limactl",), _local_lima_ready),
        (("ssh",), _remote_lima_ready),
    ],
    ids=["onepassword-readiness", "doctor-shell", "local-lima-readiness", "remote-lima-readiness"],
)
def test_a_decoy_in_the_current_directory_does_not_count_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...], installed: Callable[[], bool]
) -> None:
    """A check that found the decoy would vouch for a program the start then refuses, or worse."""
    _plant(tmp_path / "cwd", names)
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("PATH", os.pathsep.join(["", "."]))

    assert not installed()
