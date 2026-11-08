"""Resolved secrets stay private when embedded in shell-quoted assignments."""

from __future__ import annotations

import shlex
from pathlib import Path
from uuid import uuid4

import pytest

from agentworks.sessions.tmux import _tmux_env_flags
from agentworks.ssh import SSHLogger, SSHResult


@pytest.mark.parametrize("value_shape", ["quoted", "spaced", "multiline"])
def test_actual_tmux_assignment_cannot_leak_a_registered_secret_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value_shape: str
) -> None:
    marker = uuid4().hex
    value = {
        "quoted": f"prefix'{marker}'suffix",
        "spaced": f"prefix {marker} suffix",
        "multiline": f"prefix'{marker}\nsecond part",
    }[value_shape]
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("redaction", "startup", redactions=(value,))
    command = "tmux new-session" + _tmux_env_flags({"TOKEN": value, "PLAIN": "/home/agent"})
    logger.log_command(command, SSHResult(1, "", ""))
    logger.close()

    assert marker not in logger.path.read_text()
    assert "/home/agent" in logger.path.read_text()


@pytest.mark.windows
@pytest.mark.parametrize("suffix", ["_'both\"_end", r"_back\slash", "_first\nsecond"])
@pytest.mark.parametrize("interior", [False, True])
@pytest.mark.parametrize("shell_assignment", [False, True])
def test_python_diagnostic_representation_cannot_leak_a_registered_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, interior: bool, shell_assignment: bool
) -> None:
    marker = uuid4().hex
    value = marker + suffix
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("redaction", "startup", redactions=(value,))
    command = "tmux new-session -e " + shlex.quote("TOKEN=" + value)
    rendered = repr(command if shell_assignment else value)
    if interior:
        rendered = rendered[1:-1]
    logger.log_error("guest failure: " + rendered + " /home/agent/native.json")
    logger.close()

    assert marker not in logger.path.read_text()
    assert "/home/agent/native.json" in logger.path.read_text()
