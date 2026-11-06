"""WSL2's root channel: wsl.exe execs /bin/bash -s as root, with the script only on stdin."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform, wsl_program
from agentworks.execution import root_script
from agentworks.execution.root_script import RawRun


def test_the_script_reaches_bash_as_root_on_stdin_with_no_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bytes, float]] = []

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        calls.append((list(argv), script, deadline))
        return RawRun(b"out", b"err")

    monkeypatch.setattr(root_script, "run_process", run_process)
    vm: Any = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={"distro_name": "team-dev"})
    script = b"{\nAGW_SECRET=tskey-private\n}\n"

    WSL2Platform("wsl2", {"name": "wsl2"}).run_as_root(vm, RunContext(), script, 30)

    ((argv, sent, _deadline),) = calls
    assert sent == script
    assert not any("tskey-private" in argument for argument in argv)
    assert argv[argv.index("--user") + 1] == "root"
    assert argv[argv.index("--exec") + 1 :][-2:] == ["/bin/bash", "-s"]


def _in_distro_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What ``wsl.exe --exec`` runs inside the distro: the root channel's argv after ``--exec``."""
    argvs: list[list[str]] = []

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        argvs.append(list(argv))
        return RawRun(b"", b"")

    monkeypatch.setattr(root_script, "run_process", run_process)
    vm: Any = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={"distro_name": "team-dev"})
    WSL2Platform("wsl2", {"name": "wsl2"}).run_as_root(vm, RunContext(), b"", 30)
    (argv,) = argvs
    return argv[argv.index("--exec") + 1 :]


@pytest.mark.skipif(
    sys.platform != "linux" or not Path("/usr/bin/setsid").exists() or not Path("/bin/bash").exists(),
    reason="runs the in-distro argv on this Linux host, as wsl.exe would in the distro",
)
def test_the_script_has_no_controlling_terminal_even_when_wsl_gives_its_program_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wsl.exe`` makes its program a session leader on a pseudo-terminal (I34, vector W7); the script must not see it.

    The in-distro argv runs here as that program would: a session leader whose
    controlling terminal is a fresh pty, with the script on a stdin pipe.
    """
    in_distro = _in_distro_argv(monkeypatch)
    leader, follower = os.openpty()

    def as_wsl_starts_it() -> None:
        os.setsid()
        import fcntl
        import termios

        fcntl.ioctl(follower, termios.TIOCSCTTY, 0)

    script = b"if { : </dev/tty; } 2>/dev/null; then echo tty; else echo no-tty; fi\necho err >&2\nexit 7\n"
    try:
        completed = subprocess.run(
            in_distro,
            input=script,
            capture_output=True,
            preexec_fn=as_wsl_starts_it,  # noqa: PLW1509 - the child must own the pty before it execs
            timeout=30,
            check=False,
        )
    finally:
        os.close(leader)
        os.close(follower)

    assert (completed.stdout, completed.stderr, completed.returncode) == (b"no-tty\n", b"err\n", 7)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"SystemRoot": r"D:\WINDOWS"}, r"D:\WINDOWS\System32\wsl.exe"),
        ({}, r"C:\Windows\System32\wsl.exe"),
    ],
)
def test_on_windows_wsl_exe_is_named_by_absolute_path(environ: dict[str, str], expected: str) -> None:
    program = wsl_program(windows=True, environ=environ)

    assert PureWindowsPath(program).is_absolute()
    assert program == expected


def test_the_native_shell_is_a_login_shell_as_the_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs: list[list[str]] = []
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.ssh_process.run_terminal",
        lambda argv: argvs.append(list(argv)) or 7,
    )
    vm: Any = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={"distro_name": "team-dev"})

    assert WSL2Platform("wsl2", {"name": "wsl2"}).native_shell(vm, RunContext()) == 7

    (argv,) = argvs
    # No --exec, so the admin's login shell starts with its startup files.
    assert argv[1:] == ["--distribution", "team-dev", "--user", "agw"]
