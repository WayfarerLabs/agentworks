"""WSL2's root channel: wsl.exe execs /bin/bash -s as root, with the script only on stdin."""

from __future__ import annotations

from pathlib import PureWindowsPath
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
    assert argv[-3:] == ["--exec", "/bin/bash", "-s"]


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
