"""Resolved secrets stay private when embedded in shell-quoted assignments."""

from __future__ import annotations

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
