"""Sensitive stdin semantics across the four current command transports."""

from __future__ import annotations

import shlex
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


@pytest.mark.parametrize(
    ("transport", "run_path"),
    [
        (LimaTransport("vm1"), "agentworks.transports.lima.subprocess.run"),
        (WSL2Transport("Debian"), "agentworks.transports.wsl2.subprocess.run"),
    ],
)
def test_local_transport_closes_stdin_when_no_payload(
    monkeypatch,  # noqa: ANN001
    transport: LimaTransport | WSL2Transport,
    run_path: str,
) -> None:
    """The no-payload stdin contract for the local transports: hand the
    subprocess an empty (closed) stdin, not the caller's inherited console, so a
    stdin-reading guest command sees EOF instead of hanging. SSH does this with
    ``-n``; these transports have no such flag and must pass an empty pipe."""
    process = MagicMock(return_value=_completed())
    monkeypatch.setattr(run_path, process)

    transport.run("tmux list-sessions", tty=False)

    assert process.call_args.kwargs["input"] == b""


def test_remote_lima_transport_forwards_sensitive_input_to_the_safe_ssh_hop(monkeypatch) -> None:  # noqa: ANN001
    transport = RemoteLimaTransport("vm1", "vm-host")
    run = MagicMock(return_value=SSHResult(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(transport._host_login, "run", run)

    transport.run(_COMMAND, sudo=True, input_text=f"{_SENTINEL}\n", timeout=30)

    forwarded_command = run.call_args.args[0]
    assert _SENTINEL not in forwarded_command
    forwarded_argv = shlex.split(forwarded_command)
    assert forwarded_argv[:4] == ["limactl", "shell", "vm1", "bash"]
    assert forwarded_argv[4] == "-lc"
    assert forwarded_argv[5].startswith("sudo -n bash -c ")
    assert run.call_args.kwargs == {
        "check": True,
        "tty": None,
        "timeout": 30,
        "input_text": f"{_SENTINEL}\n",
        "input_data": None,
        "discard_output": False,
        "retries": None,
        "on_retry": None,
    }


@pytest.mark.parametrize(
    ("transport", "run_path"),
    [
        (SSHTransport("vm-host"), "agentworks.transports.ssh.subprocess.run"),
        (LimaTransport("vm1"), "agentworks.transports.lima.subprocess.run"),
        (WSL2Transport("Debian"), "agentworks.transports.wsl2.subprocess.run"),
    ],
)
def test_transport_streams_non_sensitive_data_and_preserves_output(
    monkeypatch,  # noqa: ANN001
    transport: SSHTransport | LimaTransport | WSL2Transport,
    run_path: str,
) -> None:
    process = MagicMock(return_value=_completed())
    monkeypatch.setattr(run_path, process)

    result = transport.run(_COMMAND, input_data="public protocol row\n", tty=False)

    assert result.stdout == f"reflected {_SENTINEL}"
    assert result.stderr == f"error {_SENTINEL}"
    assert process.call_args.kwargs["input"] == b"public protocol row\n"


def test_remote_lima_forwards_non_sensitive_input_data(monkeypatch) -> None:  # noqa: ANN001
    transport = RemoteLimaTransport("vm1", "vm-host")
    run = MagicMock(return_value=SSHResult(returncode=0, stdout="frame", stderr=""))
    monkeypatch.setattr(transport._host_login, "run", run)

    result = transport.run(_COMMAND, input_data="public protocol row\n", tty=False)

    assert result.stdout == "frame"
    assert run.call_args.kwargs["input_data"] == "public protocol row\n"
    assert run.call_args.kwargs["tty"] is False


def test_ssh_transport_discards_output_without_disabling_forced_tty(monkeypatch) -> None:  # noqa: ANN001
    """``discard_output`` sends both process streams to the null device but
    leaves TTY selection alone: a per-call ``tty=True`` still forces ``-tt``
    (and never combines it with the stdin-closing ``-n``)."""
    process = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=None, stderr=None))
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", logger=logger).run(_COMMAND, discard_output=True, tty=True)

    assert (result.stdout, result.stderr) == ("", "")
    assert "-tt" in process.call_args.args[0]
    assert "-n" not in process.call_args.args[0]
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
