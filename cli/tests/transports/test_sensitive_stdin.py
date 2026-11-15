"""Sensitive and non-sensitive stdin semantics of the SSH transport."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from agentworks.execution.trust import HOST_TRUST
from agentworks.transports.ssh import SSHTransport

_SENTINEL = "transport-stdin-swordfish"
_COMMAND = 'IFS= read -r VALUE && printf "%s" "$VALUE"'


def _completed() -> subprocess.CompletedProcess[bytes]:
    """A byte-mode result, matching how the transport runs a stdin command."""
    return subprocess.CompletedProcess(
        [], 0, stdout=f"reflected {_SENTINEL}".encode(), stderr=f"error {_SENTINEL}".encode()
    )


def test_ssh_transport_streams_sensitive_input_without_exposing_it(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    logger = MagicMock()
    monkeypatch.setattr("agentworks.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", trust=HOST_TRUST, logger=logger).run(_COMMAND, input_text=f"{_SENTINEL}\n")

    assert (result.stdout, result.stderr) == ("", "")
    assert process.call_args.kwargs["input"] == f"{_SENTINEL}\n".encode()
    assert _SENTINEL not in repr(process.call_args.args[0])
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_ssh_transport_streams_non_sensitive_data_and_preserves_output(monkeypatch) -> None:  # noqa: ANN001
    process = MagicMock(return_value=_completed())
    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", trust=HOST_TRUST).run(_COMMAND, input_data="public protocol row\n", tty=False)

    assert result.stdout == f"reflected {_SENTINEL}"
    assert result.stderr == f"error {_SENTINEL}"
    assert process.call_args.kwargs["input"] == b"public protocol row\n"


def test_ssh_transport_discards_output_without_disabling_forced_tty(monkeypatch) -> None:  # noqa: ANN001
    """``discard_output`` sends both process streams to the null device but
    leaves TTY selection alone: a per-call ``tty=True`` still forces ``-tt``
    (and never combines it with the stdin-closing ``-n``)."""
    process = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=None, stderr=None))
    logger = MagicMock()
    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", process)

    result = SSHTransport("vm-host", trust=HOST_TRUST, logger=logger).run(_COMMAND, discard_output=True, tty=True)

    assert (result.stdout, result.stderr) == ("", "")
    assert "-tt" in process.call_args.args[0]
    assert "-n" not in process.call_args.args[0]
    assert process.call_args.kwargs["stdout"] is subprocess.DEVNULL
    assert process.call_args.kwargs["stderr"] is subprocess.DEVNULL
    logger.log_command.assert_called_once_with(_COMMAND, result)


def test_ssh_transport_rejects_sensitive_input_with_output_discard() -> None:
    secret = "transport-combined-mode-secret"

    with pytest.raises(ValueError) as caught:
        SSHTransport("vm-host", trust=HOST_TRUST).run(_COMMAND, input_text=secret, discard_output=True)

    assert secret not in str(caught.value)
