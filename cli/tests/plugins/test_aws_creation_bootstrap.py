"""Native listening custody and strict read-only early guest observations."""

from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierReport, CapturedOutput, Deadline, Dispatch, ExitStatus
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHConnection
from agentworks.plugins.aws import _creation_bootstrap


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
    monkeypatch.setattr(_creation_bootstrap.socket, "socket", lambda *args: original)
    readiness = _creation_bootstrap.SSHReadiness()
    with pytest.raises(StateError if isinstance(fault, OSError) else KeyboardInterrupt):
        readiness.wait("192.0.2.7", Deadline.after(5))
    assert readiness._socket is original
    with pytest.raises(StateError):
        readiness.wait("192.0.2.7", Deadline.after(5))
    assert sum(name == "connect" for name, _ in calls) == 1
    current_fault[0] = None
    assert readiness.close() and readiness._socket is None


def test_listening_probe_retries_only_connections_inside_original_budget(monkeypatch):
    clock = [100.0]
    sockets = []
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(_creation_bootstrap.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def socket(*args):
        original = SimpleNamespace(closed=False, settimeout=lambda seconds: None, connect_ex=lambda endpoint: 111)

        def close():
            original.closed = True

        original.close = close
        sockets.append(original)
        return original

    monkeypatch.setattr(_creation_bootstrap.socket, "socket", socket)
    readiness = _creation_bootstrap.SSHReadiness()
    with pytest.raises(TimeoutError):
        readiness.wait("192.0.2.7", Deadline.after(0.25))
    assert sockets and all(original.closed for original in sockets)
    assert readiness._socket is None


@pytest.mark.parametrize("failure", [None, "wrong_nonce", "nonzero", "incomplete", "late"])
@pytest.mark.parametrize("admin", ["root", "operator"])
def test_prerequisite_gate_requires_complete_matching_strict_ssh_observation(monkeypatch, failure, admin):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    custody = LocalDeliveryCustody()
    connection = SSHConnection(
        "192.0.2.7", admin, Path("/unused/identity"), ManagedSSHTrust(Path("/unused/trust"))
    )
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
            Dispatch.SENT, completion=ExitStatus(code=1 if failure == "nonzero" else 0),
            stdout=CapturedOutput((output + "\n").encode("ascii"), complete=failure != "incomplete"),
        )

    monkeypatch.setattr(_creation_bootstrap.SSHCarrier, "execute", execute)
    if failure is None:
        _creation_bootstrap.observe_early_guest(connection, "a" * 32, Deadline.after(5), custody)
    else:
        with pytest.raises(StateError):
            _creation_bootstrap.observe_early_guest(connection, "a" * 32, Deadline.after(5), custody)
    assert len(calls) == 1


@pytest.mark.parametrize("admin", ["root", "operator"])
def test_public_cloud_init_intentionally_selects_root_access_without_password_auth(admin):
    payload = _creation_bootstrap.early_user_data(
        admin=admin, public_key="ssh-ed25519 public-key", hostname="vm-one", marker="a" * 32,
    )
    config = yaml.safe_load(gzip.decompress(payload))
    assert config["disable_root"] is (admin != "root")
    assert config["ssh_pwauth"] is False
    assert config["users"][0]["name"] == admin
    assert config["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 public-key"]
