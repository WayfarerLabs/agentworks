"""A failed wsl.exe command is a typed error that quotes what wsl.exe printed, wherever it printed it."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import wsl2
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform, WSLCommandFailed
from agentworks.errors import ExternalError


def _wsl_exits(monkeypatch: pytest.MonkeyPatch, *, stdout: str, stderr: str = "") -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)


def test_a_start_interrupted_by_a_shutdown_says_why_and_what_to_do(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """``wsl --shutdown`` during ``vm start`` gave 'RuntimeError: wsl command failed:' with no text."""
    # wsl.exe writes its own errors to stdout, in UTF-16 when redirected.
    _wsl_exits(monkeypatch, stdout="\x00".join("The operation was aborted.") + "\x00\r\n")
    vm: Any = SimpleNamespace(name="dev", platform_metadata={"distro_name": "team-dev"})

    with pytest.raises(WSLCommandFailed) as caught:
        WSL2Platform("wsl2", {"name": "wsl2"}).start(vm, RunContext())

    assert isinstance(caught.value, ExternalError)
    assert "The operation was aborted." in str(caught.value)
    assert "--distribution team-dev" in str(caught.value)
    assert caught.value.hint


def test_a_failure_with_no_output_is_still_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _wsl_exits(monkeypatch, stdout="", stderr="")

    with pytest.raises(WSLCommandFailed):
        wsl2._wsl(["--terminate", "team-dev"])
