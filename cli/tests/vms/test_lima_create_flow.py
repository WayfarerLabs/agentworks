"""LimaPlatform.create wiring: the host-orchestrated restart that applies
the arm64.nosve SVE mask.

Exercised through ``create`` with the backend seams (limactl create/start,
``_run_lima``, transport) mocked, so the test asserts the exact ``limactl``
calls without a real VM.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.bootstrap_script import (
    REBOOT_SENTINEL_PATH,
)
from agentworks.capabilities.vm_platform.lima import (
    _REBOOT_CLEAR_MARKER,
    _REBOOT_PENDING_MARKER,
    LimaPlatform,
)
from agentworks.ssh import SSHError


def _request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key="tskey-test",
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    platform: LimaPlatform,
    *,
    sentinel_present: bool,
) -> list[str]:
    """Mock the backend seams; return the ``_run_lima`` commands issued."""
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(
        LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace()
    )

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if REBOOT_SENTINEL_PATH in cmd:
            # The real probe exits 0 either way and reports on stdout.
            marker = _REBOOT_PENDING_MARKER if sentinel_present else _REBOOT_CLEAR_MARKER
            return f"{marker}\n"
        if "tailscale ip" in cmd:
            return "100.64.0.1"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)
    return ran


def test_sve_sentinel_triggers_one_host_restart(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    platform = LimaPlatform("lima", {})
    ran = _wire(monkeypatch, platform, sentinel_present=True)
    platform.create(_request())
    # Exactly one restart: a regression to a restart loop must fail here, not
    # slip through an at-least-once assertion (the whole point is one restart).
    restarts = [cmd for cmd in ran if "limactl restart myvm" in cmd]
    assert len(restarts) == 1, restarts


def test_no_restart_when_sentinel_absent(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    platform = LimaPlatform("lima", {})
    ran = _wire(monkeypatch, platform, sentinel_present=False)
    platform.create(_request())
    assert not any("limactl restart" in cmd for cmd in ran)


def test_probe_failure_warns_and_does_not_restart(
    monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    """A genuine probe failure is reported, not read as an absent sentinel.

    The probe exits 0 whether or not the sentinel is there, so an SSHError
    means the shell or transport actually broke. Create still completes (the
    VM exists, and Phase A bootstrap follows), but the operator is told.
    """
    platform = LimaPlatform("lima", {})
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(
        LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace()
    )

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if REBOOT_SENTINEL_PATH in cmd:
            raise SSHError("connection reset")
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    platform.create(_request())

    assert not any("limactl restart" in cmd for cmd in ran)
    warned = "\n".join(warnings)
    assert "needs a restart to finish provisioning" in warned
    assert "connection reset" in warned
