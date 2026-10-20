"""Native listening custody and strict read-only early guest observations."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CapturedOutput, CarrierReport, Deadline, Dispatch, ExitStatus
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHConnection
from agentworks.vms import _creation_ssh


@pytest.mark.parametrize("fault", [OSError("close failed"), KeyboardInterrupt("close interrupted")])
def test_listening_probe_retains_the_original_socket(monkeypatch, fault):
    calls = []
    current_fault = [fault]
    original = SimpleNamespace(
        settimeout=lambda seconds: calls.append(("timeout", seconds)),
        connect_ex=lambda endpoint: calls.append(("connect", endpoint)) or 0,
    )

    def close():
        calls.append(("close", original))
        if current_fault[0] is not None:
            raise current_fault[0]

    original.close = close
    monkeypatch.setattr(socket, "socket", lambda *args: original)
    readiness = _creation_ssh.SSHReadiness()
    with pytest.raises(StateError if isinstance(fault, OSError) else KeyboardInterrupt):
        readiness.wait("192.0.2.7", Deadline.after(5))
    assert readiness._socket is not None
    assert calls[-1] == ("close", original)
    with pytest.raises(StateError):
        readiness.wait("192.0.2.7", Deadline.after(5))
    assert sum(name == "connect" for name, _ in calls) == 1
    current_fault[0] = None
    assert readiness.close() and readiness._socket is None


def test_listening_probe_retries_only_connections_inside_original_budget(monkeypatch):
    clock = [100.0]
    sockets = []
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def make_socket(*args):
        original = SimpleNamespace(closed=False, settimeout=lambda seconds: None, connect_ex=lambda endpoint: 111)

        def close():
            original.closed = True

        original.close = close
        sockets.append(original)
        return original

    monkeypatch.setattr(socket, "socket", make_socket)
    readiness = _creation_ssh.SSHReadiness()
    with pytest.raises(TimeoutError):
        readiness.wait("192.0.2.7", Deadline.after(0.25))
    assert sockets and all(original.closed for original in sockets)
    assert readiness._socket is None


@pytest.mark.parametrize("failure", [None, "wrong_nonce", "nonzero", "incomplete", "late", "not_sent"])
@pytest.mark.parametrize("admin", ["root", "operator"])
def test_prerequisite_gate_requires_complete_matching_strict_ssh_observation(monkeypatch, failure, admin):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    custody = LocalDeliveryCustody()
    connection = SSHConnection("192.0.2.7", admin, Path("/unused/identity"), ManagedSSHTrust(Path("/unused/trust")))
    calls = []

    def execute(carrier, invocation, *, io, deadline, custody):
        calls.append((carrier, invocation))
        assert carrier._connection is connection and deadline.expires_at == 105.0
        assert invocation.argv[0] == ("/usr/bin/env" if admin == "root" else "/usr/bin/sudo")
        nonce = invocation.argv[-1]
        output = "other-nonce" if failure == "wrong_nonce" else nonce
        if failure == "late":
            clock[0] = 106.0
        return CarrierReport(
            Dispatch.NOT_SENT if failure == "not_sent" else Dispatch.SENT,
            completion=None if failure == "not_sent" else ExitStatus(code=1 if failure == "nonzero" else 0),
            stdout=CapturedOutput((output + "\n").encode("ascii"), complete=failure != "incomplete"),
        )

    monkeypatch.setattr(SSHCarrier, "execute", execute)
    if failure is None:
        _creation_ssh.observe_early_guest(
            connection, "a" * 32, Deadline.after(5), custody, bootstrap_check=_creation_ssh.CLOUD_INIT_CHECK
        )
    else:
        with pytest.raises(StateError):
            _creation_ssh.observe_early_guest(
                connection, "a" * 32, Deadline.after(5), custody, bootstrap_check=_creation_ssh.CLOUD_INIT_CHECK
            )
    assert len(calls) == 1


@pytest.mark.parametrize("expires_at,error", [(None, ValidationError), (0, TimeoutError)])
def test_invalid_budget_refuses_before_listening_or_ssh_delivery(monkeypatch, expires_at, error):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid budget reached native delivery")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(SSHCarrier, "execute", forbidden)
    with pytest.raises(error):
        _creation_ssh.SSHReadiness().wait("192.0.2.7", Deadline(expires_at))
    connection = SSHConnection(
        "192.0.2.7", "operator", Path("/unused/identity"), ManagedSSHTrust(Path("/unused/trust"))
    )
    with pytest.raises(error):
        _creation_ssh.observe_early_guest(
            connection,
            "a" * 32,
            Deadline(expires_at),
            LocalDeliveryCustody(),
            bootstrap_check=_creation_ssh.CLOUD_INIT_CHECK,
        )
