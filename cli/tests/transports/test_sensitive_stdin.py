"""Sensitive stdin semantics across the four current command transports."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from agentworks.ssh import SSHResult
from agentworks.transports.lima import LimaTransport
from agentworks.transports.remote_lima import RemoteLimaTransport
from agentworks.transports.ssh import SSHTransport
from agentworks.transports.wsl2 import WSL2Transport

_SENTINEL = "transport-stdin-swordfish"
_COMMAND = 'IFS= read -r VALUE && printf "%s" "$VALUE"'


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=f"reflected {_SENTINEL}", stderr=f"error {_SENTINEL}")


def test_ssh_transport_streams_sensitive_input_without_exposing_it(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n"
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_lima_transport_streams_sensitive_input_and_logs_only_empty_output(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.lima.subprocess.run", process)

    result = LimaTransport("vm1", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n"
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_wsl2_transport_streams_sensitive_input_and_logs_only_empty_output(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.wsl2.subprocess.run", process)

    result = WSL2Transport("Debian", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n"
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_remote_lima_transport_forwards_sensitive_input_to_the_safe_ssh_hop(monkeypatch) -> None:  # noqa: ANN001
    transport = RemoteLimaTransport("vm1", "vm-host")
    run = MagicMock(return_value=SSHResult(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(transport._host_login, "run", run)

    transport.run(_COMMAND, sudo=True, input_text=f"{_SENTINEL}\n", timeout=30)

    forwarded_command = run.call_args.args[0]
    assert _SENTINEL not in forwarded_command
    assert forwarded_command.startswith("limactl shell vm1 -- sudo -n bash -c ")
    assert run.call_args.kwargs == {
        "check": True,
        "timeout": 30,
        "input_text": f"{_SENTINEL}\n",
        "retries": None,
        "on_retry": None,
    }
