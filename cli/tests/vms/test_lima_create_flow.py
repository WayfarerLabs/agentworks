"""LimaPlatform.create wiring: the host-orchestrated restart that applies
the arm64.nosve SVE mask.

Exercised through ``create`` with the backend seams (limactl create/start,
``LimaHost.run``, transport) mocked, so the test asserts the exact ``limactl``
calls without a real VM.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

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
from agentworks.capabilities.vm_platform.lima_host import LimaCommandFailed, LimaHost
from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.debian import DebianRelease
from agentworks.errors import ConnectivityError
from tests._root_channel_fakes import ADDRESS, Answer, FakeGuest, answer_by_payload, install_at_run_process
from tests.conftest import provisioning_trust_for_tests


def _request(*, tailscale_auth_key: str = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        debian_release=DebianRelease.TRIXIE,
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("myvm"),
        tailscale_auth_key=tailscale_auth_key,
        progress=MagicMock(),
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
    """Mock the backend seams; return the ``LimaHost.run`` commands issued."""
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())
    install_at_run_process(monkeypatch, FakeGuest())

    def _fake_run(self: LimaHost, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if REBOOT_SENTINEL_PATH in cmd:
            # The real probe exits 0 either way and reports on stdout.
            marker = _REBOOT_PENDING_MARKER if sentinel_present else _REBOOT_CLEAR_MARKER
            return f"{marker}\n"
        return ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)
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

    The probe exits 0 whether or not the sentinel is there, so a LimaCommandFailed
    means the shell or transport actually broke. Create still completes (the
    VM exists, and Phase A bootstrap follows), but the operator is told.
    """
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())
    install_at_run_process(monkeypatch, FakeGuest())

    def _fake_run(self: LimaHost, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        if REBOOT_SENTINEL_PATH in cmd:
            raise LimaCommandFailed("connection reset")
        return ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)

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
    guest = install_at_run_process(monkeypatch, FakeGuest())

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
            lambda self, name, lima_yaml, *, progress: submitted.append(lima_yaml),
        )

    def _fake_run(self: LimaHost, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        if REBOOT_SENTINEL_PATH in command:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        return ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)

    result = LimaPlatform("lima", {"placement": placement}).create(
        _request(tailscale_auth_key=secret),
        RunContext(),
    )

    assert result.tailscale_ip == ADDRESS
    assert submitted and len(submitted) == 1
    submitted_yaml = submitted[0]
    persisted_config = cast("dict[str, Any]", yaml.safe_load(submitted_yaml))
    provider_list_render = json.dumps({"name": "myvm", "config": persisted_config}, sort_keys=True)
    assert secret not in submitted_yaml
    assert secret not in provider_list_render
    assert all(secret not in step["script"] for step in persisted_config["provision"])

    # The key reaches the guest only in the join's script, on the root channel's stdin.
    assert secret not in repr(calls)
    assert [secret.encode() in script for script in guest.scripts] == [True, False]
    assert guest.payloads == [JOIN_FRAGMENT, "tailscale ip -4"]
    assert not any(secret in arg for argv in guest.argvs for arg in argv)


def test_an_address_that_cannot_be_read_logs_the_node_out_and_removes_the_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tskey-ip-probe-sentinel"
    ran: list[str] = []
    guest = install_at_run_process(
        monkeypatch, FakeGuest(answer_by_payload(answers={"tailscale ip -4": Answer(status=1)}))
    )
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda seconds: None)
    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, lima_yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaHost, command: str, **kwargs: object) -> str:
        ran.append(command)
        return f"{_REBOOT_CLEAR_MARKER}\n" if REBOOT_SENTINEL_PATH in command else ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)

    with pytest.raises(ConnectivityError, match="logged out"):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
            _request(tailscale_auth_key=secret),
            RunContext(),
        )

    assert guest.joins == 1
    assert guest.count("tailscale logout") == 1
    assert "limactl delete --force myvm" in ran
