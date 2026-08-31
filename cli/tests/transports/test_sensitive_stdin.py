"""Sensitive stdin semantics across the four current command transports."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from agentworks.ssh import SSHResult
from agentworks.transports.lima import LimaTransport
from agentworks.transports.remote_lima import RemoteLimaTransport
from agentworks.transports.ssh import SSHTransport
from agentworks.transports.wsl2 import WSL2Transport

_SENTINEL = "transport-stdin-swordfish"
_COMMAND = 'IFS= read -r VALUE && printf "%s" "$VALUE"'


def _completed() -> subprocess.CompletedProcess[bytes]:
    """A byte-mode result, matching how these transports run a stdin command."""
    return subprocess.CompletedProcess(
        [], 0, stdout=f"reflected {_SENTINEL}".encode(), stderr=f"error {_SENTINEL}".encode()
    )


def test_ssh_transport_streams_sensitive_input_without_exposing_it(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n".encode()
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_lima_transport_streams_sensitive_input_and_logs_only_empty_output(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.lima.subprocess.run", process)

    result = LimaTransport("vm1", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n".encode()
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_wsl2_transport_streams_sensitive_input_and_logs_only_empty_output(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.wsl2.subprocess.run", process)

    result = WSL2Transport("Debian", logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n".encode()
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
        "discard_output": False,
        "retries": None,
        "on_retry": None,
    }


def test_ssh_transport_discards_output_without_disabling_forced_tty(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=None, stderr=None))
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", force_tty=True, logger=logger).run(_COMMAND, discard_output=True)

    assert (result.stdout, result.stderr) == ("", "")
    assert "-tt" in process.call_args.args[0]
    assert process.call_args.kwargs["stdout"] is subprocess.DEVNULL
    assert process.call_args.kwargs["stderr"] is subprocess.DEVNULL
    logger.log_command.assert_called_once_with(_COMMAND, result)


@pytest.mark.parametrize(
    ("transport", "run_path"),
    [
        (LimaTransport("vm1"), "agentworks.transports.lima.subprocess.run"),
        (WSL2Transport("Debian"), "agentworks.transports.wsl2.subprocess.run"),
    ],
)
def test_local_transport_discards_output_at_process_boundary(
    monkeypatch,  # noqa: ANN001
    transport: LimaTransport | WSL2Transport,
    run_path: str,
) -> None:
    process = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=None, stderr=None))
    logger = MagicMock()
    transport.logger = logger
    monkeypatch.setattr(run_path, process)

    result = transport.run(_COMMAND, discard_output=True)

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["stdout"] is subprocess.DEVNULL
    assert process.call_args.kwargs["stderr"] is subprocess.DEVNULL
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_remote_lima_transport_forwards_output_discard(monkeypatch) -> None:  # noqa: ANN001
    transport = RemoteLimaTransport("vm1", "vm-host")
    run = MagicMock(return_value=SSHResult(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(transport._host_login, "run", run)

    transport.run(_COMMAND, discard_output=True)

    assert run.call_args.kwargs["discard_output"] is True


@pytest.mark.parametrize(
    "transport",
    [
        SSHTransport("vm-host"),
        LimaTransport("vm1"),
        WSL2Transport("Debian"),
        RemoteLimaTransport("vm1", "vm-host"),
    ],
)
def test_transport_rejects_sensitive_input_with_output_discard(
    transport: SSHTransport | LimaTransport | WSL2Transport | RemoteLimaTransport,
) -> None:
    secret = "transport-combined-mode-secret"

    with pytest.raises(ValueError) as exc_info:
        transport.run(_COMMAND, input_text=secret, discard_output=True)

    assert secret not in str(exc_info.value)
