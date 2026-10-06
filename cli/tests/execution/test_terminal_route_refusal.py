"""Terminal input cannot enter carriers without native terminal adapters."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import CarrierIO, Deadline, PreparedInvocation, SinkOutput, TerminalInput
from agentworks.execution.carriers import _subprocess, wsl2
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers.ssh import client as ssh_client
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection


class _Source:
    def try_read(self, limit: int) -> bytes | None:
        raise AssertionError("unsupported carrier read terminal bootstrap")


class _Sink:
    def try_write(self, data: memoryview) -> int:
        raise AssertionError("unsupported carrier wrote terminal output")


def _io() -> CarrierIO:
    return CarrierIO(input=TerminalInput(0, 1, "xterm", _Source()), output=SinkOutput(_Sink(), _Sink()))


def test_proxmox_refuses_terminal_before_provider_access(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example", "node1", 123, "token", "secret"))
    request = MagicMock(side_effect=AssertionError("provider accessed"))
    monkeypatch.setattr(carrier._wire, "request", request)
    invocation = PreparedInvocation(("/bin/true",))
    io = _io()
    assert not carrier.features.terminal
    with pytest.raises(ValidationError):
        carrier.validate(invocation, io=io)
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline(None))
    request.assert_not_called()


def test_wsl2_refuses_terminal_before_local_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = WSL2Carrier(WSL2Connection("debian", "user", "wsl.exe"))
    run = MagicMock(side_effect=AssertionError("local client spawned"))
    monkeypatch.setattr(wsl2, "run_process", run)
    invocation = PreparedInvocation(("/bin/true",))
    io = _io()
    assert not carrier.features.terminal
    with pytest.raises(ValidationError):
        carrier.validate(invocation, io=io)
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline(None))
    run.assert_not_called()


def test_buffered_ssh_refuses_terminal_before_connection_access(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = SSHCarrier(
        SSHConnection("host.example", "user", Path("/missing/key"), SSHTrustFiles((Path("/missing/hosts"),)))
    )
    validate_files = MagicMock(side_effect=AssertionError("connection files accessed"))
    run = MagicMock(side_effect=AssertionError("local client spawned"))
    monkeypatch.setattr(ssh_client, "admit_connection", validate_files)
    monkeypatch.setattr(ssh_client, "run_process", run)
    invocation = PreparedInvocation(("/bin/true",))
    io = _io()
    assert not carrier.features.terminal
    with pytest.raises(ValidationError):
        carrier.validate(invocation, io=io)
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline(None))
    validate_files.assert_not_called()
    run.assert_not_called()


def test_generic_subprocess_refuses_terminal_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(side_effect=AssertionError("local child spawned"))
    monkeypatch.setattr(_subprocess, "run_owned_process", run)
    with pytest.raises(ValidationError):
        _subprocess.run_process(["/bin/true"], io=_io(), deadline=Deadline(None), live_stdio=True)
    run.assert_not_called()
