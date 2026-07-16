"""LimaPlatform.create wiring: the opt-in nested-virtualization line and
the host-orchestrated restart that applies the arm64.nosve SVE mask.

Both are exercised through ``create`` with the backend seams (limactl
create/start, ``_run_lima``, transport) mocked, so the test asserts the
rendered Lima YAML and the exact ``limactl`` calls without a real VM.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.bootstrap_script import (
    SVE_REBOOT_SENTINEL_PATH,
)
from agentworks.capabilities.vm_platform.lima import (
    _SVE_CLEAR_MARKER,
    _SVE_PENDING_MARKER,
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
) -> tuple[list[str], list[str]]:
    """Mock the backend seams; return (captured_yaml, ran_commands)."""
    captured_yaml: list[str] = []
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: captured_yaml.append(yaml),
    )
    monkeypatch.setattr(
        LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace()
    )

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if SVE_REBOOT_SENTINEL_PATH in cmd:
            # The real probe exits 0 either way and reports on stdout.
            marker = _SVE_PENDING_MARKER if sentinel_present else _SVE_CLEAR_MARKER
            return f"{marker}\n"
        if "tailscale ip" in cmd:
            return "100.64.0.1"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)
    return captured_yaml, ran


def test_nested_virtualization_line_emitted_only_when_requested(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    on = LimaPlatform("lima", {"nested_virtualization": True})
    yaml_on, _ = _wire(monkeypatch, on, sentinel_present=False)
    on.create(_request())
    assert "nestedVirtualization: true" in yaml_on[0]

    off = LimaPlatform("lima", {})
    yaml_off, _ = _wire(monkeypatch, off, sentinel_present=False)
    off.create(_request())
    assert "nestedVirtualization" not in yaml_off[0]


def test_sve_sentinel_triggers_one_host_restart(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    platform = LimaPlatform("lima", {})
    _, ran = _wire(monkeypatch, platform, sentinel_present=True)
    platform.create(_request())
    assert any("limactl restart myvm" in cmd for cmd in ran)


def test_no_restart_when_sentinel_absent(
    monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    platform = LimaPlatform("lima", {})
    _, ran = _wire(monkeypatch, platform, sentinel_present=False)
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
        if SVE_REBOOT_SENTINEL_PATH in cmd:
            raise SSHError("connection reset")
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    platform.create(_request())

    assert not any("limactl restart" in cmd for cmd in ran)
    warned = "\n".join(warnings)
    assert "SVE mask needs a restart" in warned
    assert "connection reset" in warned
