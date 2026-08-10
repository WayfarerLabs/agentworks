"""LimaPlatform.create wiring: the host-orchestrated restart that applies
the arm64.nosve SVE mask.

Exercised through ``create`` with the backend seams (limactl create/start,
``_run_lima``, transport) mocked, so the test asserts the exact ``limactl``
calls without a real VM.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from agentworks.capabilities.base import RunContext
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


def _request(*, tailscale_auth_key: str | None = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key=tailscale_auth_key,
        # The vm-template layer's resolved defaults, which is the only
        # shape a platform ever sees (the hardware fields are required).
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
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
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

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


def test_sve_sentinel_triggers_one_host_restart(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    ran = _wire(monkeypatch, platform, sentinel_present=True)
    platform.create(_request(), RunContext())
    # Exactly one restart: a regression to a restart loop must fail here, not
    # slip through an at-least-once assertion (the whole point is one restart).
    restarts = [cmd for cmd in ran if "limactl restart myvm" in cmd]
    assert len(restarts) == 1, restarts


def test_no_restart_when_sentinel_absent(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    ran = _wire(monkeypatch, platform, sentinel_present=False)
    platform.create(_request(), RunContext())
    assert not any("limactl restart" in cmd for cmd in ran)


def test_probe_failure_warns_and_does_not_restart(monkeypatch: pytest.MonkeyPatch, warnings: list[str]) -> None:
    """A genuine probe failure is reported, not read as an absent sentinel.

    The probe exits 0 whether or not the sentinel is there, so an SSHError
    means the shell or transport actually broke. Create still completes (the
    VM exists, and Phase A bootstrap follows), but the operator is told.
    """
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if REBOOT_SENTINEL_PATH in cmd:
            raise SSHError("connection reset")
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    platform.create(_request(), RunContext())

    assert not any("limactl restart" in cmd for cmd in ran)
    warned = "\n".join(warnings)
    assert "needs a restart to finish provisioning" in warned
    assert "connection reset" in warned


@pytest.mark.parametrize(
    ("placement", "create_method"),
    [
        ({"mode": "local"}, "_create_local"),
        ({"mode": "ssh", "host": "user@host"}, "_create_remote"),
    ],
    ids=("local", "remote"),
)
def test_submitted_lima_configuration_never_contains_tailscale_key(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    placement: dict[str, str],
    create_method: str,
) -> None:
    """Inspect the exact YAML handed to Lima, its durable provider boundary.

    Lima copies provision scripts into its instance YAML and may return them
    from ``limactl list --json``. Streaming the original template over stdin is
    therefore insufficient; the submitted configuration itself must be safe.
    """
    secret = "tskey-persistence-sentinel"
    submitted: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    if create_method == "_create_local":
        monkeypatch.setattr(
            LimaPlatform,
            "_create_local",
            lambda self, name, lima_yaml: submitted.append(lima_yaml),
        )
    else:
        monkeypatch.setattr(
            LimaPlatform,
            "_create_remote",
            lambda self, name, lima_yaml, *, log_vm_name: submitted.append(lima_yaml),
        )

    def _fake_run(self: LimaPlatform, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        if REBOOT_SENTINEL_PATH in command:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        if "tailscale ip" in command:
            return "100.64.0.1\n"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    result = LimaPlatform("lima", {"placement": placement}).create(
        _request(tailscale_auth_key=secret),
        RunContext(),
    )

    assert result.bootstrap_complete is True
    assert submitted and len(submitted) == 1
    submitted_yaml = submitted[0]
    persisted_config = cast("dict[str, Any]", yaml.safe_load(submitted_yaml))
    provider_list_render = json.dumps({"name": "myvm", "config": persisted_config}, sort_keys=True)
    assert secret not in submitted_yaml
    assert secret not in provider_list_render
    assert all(secret not in step["script"] for step in persisted_config["provision"])

    sensitive_calls = [(command, kwargs) for command, kwargs in calls if kwargs.get("input_text") is not None]
    assert sensitive_calls == [
        (
            "limactl shell myvm sudo -n /bin/bash -c "
            '\'IFS= read -r TAILSCALE_AUTH_KEY && test -n "$TAILSCALE_AUTH_KEY" '
            '&& tailscale up --auth-key "$TAILSCALE_AUTH_KEY"\'',
            {"input_text": f"{secret}\n"},
        )
    ]
    assert secret not in sensitive_calls[0][0]


def test_lima_without_auth_key_keeps_join_deferred_to_phase_a(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    submitted: list[str] = []
    ran: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, lima_yaml: submitted.append(lima_yaml),
    )
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaPlatform, command: str, **kwargs: object) -> str:
        ran.append((command, kwargs))
        if REBOOT_SENTINEL_PATH in command:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    result = LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
        _request(tailscale_auth_key=None),
        RunContext(),
    )

    assert result.bootstrap_complete is False
    assert submitted and "no-op (deferred to Phase A)" in submitted[0]
    assert not any(kwargs.get("input_text") is not None for _command, kwargs in ran)


def test_ip_probe_failure_keeps_ephemeral_join_bootstrap_complete(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    secret = "tskey-ip-probe-sentinel"
    submitted: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, lima_yaml: submitted.append(lima_yaml),
    )
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaPlatform, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        if REBOOT_SENTINEL_PATH in command:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        if "tailscale ip" in command:
            raise SSHError("temporary IP probe failure")
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    result = LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
        _request(tailscale_auth_key=secret),
        RunContext(),
    )

    assert result.bootstrap_complete is True
    assert result.tailscale_ip is None
    assert submitted and secret not in submitted[0]
    assert sum(kwargs.get("input_text") == f"{secret}\n" for _command, kwargs in calls) == 1
    assert "retry IP discovery without the auth key" in "\n".join(warnings)
