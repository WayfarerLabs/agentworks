"""Platform-owned vm-platform v2 locator observations."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import ConnectivityError, LimitExceededError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.proxmox.platform import ProxmoxPlatform


def _vm(*, metadata: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="test-vm",
        admin_username="agentworks",
        platform_metadata=metadata or {"distro_name": "test-distro"},
    )


def _proxmox() -> ProxmoxPlatform:
    return ProxmoxPlatform(
        "proxmox",
        {
            "api_url": "https://pve.example.test:8006",
            "node": "pve1",
            "token_id": "agentworks@pam!agw",
        },
    )


def test_lima_locator_is_unavailable_without_provider_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = LimaPlatform("lima", {})
    monkeypatch.setattr(platform, "_run_lima", lambda *_args, **_kwargs: pytest.fail("unexpected lookup"))

    assert (
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))
        == ProviderLocatorUnavailable()
    )


def test_proxmox_locator_is_unavailable_without_provider_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = _proxmox()
    monkeypatch.setattr(platform, "_api", lambda _ctx: pytest.fail("unexpected lookup"))

    assert (
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))
        == ProviderLocatorUnavailable()
    )


def test_wsl2_locator_uses_one_bounded_registration_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "machine_guid": "2f1b1ed8-a629-4fc5-93ef-504e9a780a37",
                    "user_sid": "S-1-5-21-100-200-300-1001",
                    "registration_guid": "613ea898-8445-4e6e-82c7-f6e9ae8d7235",
                }
            ),
        )

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)

    result = WSL2Platform("wsl2", {}).observe_provider_locator(
        _vm(metadata={"distro_name": "test-distro"}),
        RunContext(),
        deadline=Deadline.after(10),
    )

    assert result == ProviderLocator(
        "wsl2:2f1b1ed8-a629-4fc5-93ef-504e9a780a37:S-1-5-21-100-200-300-1001:613ea898-8445-4e6e-82c7-f6e9ae8d7235"
    )
    assert len(calls) == 1
    args = cast(tuple[object, ...], calls[0]["args"])
    kwargs = cast(dict[str, object], calls[0]["kwargs"])
    env = cast(dict[str, str], kwargs["env"])
    command = cast(list[str], args[0])
    assert cast(float, kwargs["timeout"]) > 0
    assert "test-distro" not in command
    assert env["AGENTWORKS_WSL_DISTRO"] == "test-distro"


def test_wsl2_locator_rejects_invalid_provider_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout='{"machine_guid":"not-guid"}'),
    )

    with pytest.raises(StateError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))


def test_wsl2_locator_maps_process_failure_to_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(ConnectivityError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))


def test_wsl2_locator_maps_missing_or_duplicate_registration_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform import wsl2

    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=wsl2._WSL2_LOCATOR_REGISTRATION_MISMATCH_EXIT,
            stdout="",
        ),
    )

    with pytest.raises(StateError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))


def test_wsl2_locator_maps_process_timeout_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired("powershell", 1)

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", timeout)

    with pytest.raises(LimitExceededError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))


def test_wsl2_locator_rejects_a_late_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform import wsl2

    calls = 0

    def remaining(_deadline: Deadline, *, vm_name: str) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired", entity_kind="vm", entity_name=vm_name)
        return 1

    monkeypatch.setattr(wsl2, "provider_locator_remaining", remaining)
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="{}"),
    )

    with pytest.raises(LimitExceededError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))

    assert calls == 2


def test_wsl2_locator_keeps_observed_process_failure_over_late_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform import wsl2

    calls = 0

    def remaining(*_args: object, **_kwargs: object) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired")
        return 1

    monkeypatch.setattr(wsl2, "provider_locator_remaining", remaining)
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(ConnectivityError):
        WSL2Platform("wsl2", {}).observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10))

    assert calls == 1
