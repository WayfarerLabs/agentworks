"""Platform-owned vm-platform v2 locator observations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import ConfigError, ConnectivityError, LimitExceededError, StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Capture, CapturedOutput, Deadline, Failure
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.plugins.proxmox.platform import ProxmoxPlatform


def _process_result(status: int = 0, stdout: bytes | str = b"", failure: Failure | None = None) -> ProcessResult:
    data = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    return ProcessResult(
        True,
        status,
        status if failure is None else None,
        CapturedOutput(data, complete=True),
        CapturedOutput(complete=True),
        failure,
    )


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
    local_delivery = LocalDeliveryCustody()
    platform = LimaPlatform("lima", {})
    monkeypatch.setattr(platform, "_run_lima", lambda *_args, **_kwargs: pytest.fail("unexpected lookup"))

    assert (
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
        == ProviderLocatorUnavailable()
    )


def test_proxmox_locator_requires_scoped_configuration_without_legacy_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _proxmox()
    monkeypatch.setattr(platform, "_api", lambda _ctx: pytest.fail("unexpected lookup"))

    with pytest.raises(ConfigError):
        platform.observe_provider_locator(
            _vm(metadata={"vmid": "123"}), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )


def test_wsl2_locator_uses_one_bounded_registration_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    calls: list[dict[str, object]] = []

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"args": args, "kwargs": kwargs})
        return _process_result(
            status=0,
            stdout=json.dumps(
                {
                    "machine_guid": "2f1b1ed8-a629-4fc5-93ef-504e9a780a37",
                    "user_sid": "S-1-5-21-100-200-300-1001",
                    "registration_guid": "613ea898-8445-4e6e-82c7-f6e9ae8d7235",
                }
            ),
        )

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.run_process", run)

    result = WSL2Platform("wsl2", {}).observe_provider_locator(
        _vm(metadata={"distro_name": "test-distro"}),
        RunContext(),
        deadline=Deadline.after(10),
        custody=local_delivery,
    )

    assert result == ProviderLocator(
        "wsl2:2f1b1ed8-a629-4fc5-93ef-504e9a780a37:S-1-5-21-100-200-300-1001:613ea898-8445-4e6e-82c7-f6e9ae8d7235"
    )
    assert len(calls) == 1
    args = cast(tuple[object, ...], calls[0]["args"])
    kwargs = cast(dict[str, object], calls[0]["kwargs"])
    env = cast(dict[str, str], kwargs["env"])
    command = cast(list[str], args[0])
    assert isinstance(kwargs["io"].output, Capture)
    assert kwargs["custody"] is local_delivery
    assert cast(Deadline, kwargs["deadline"]).remaining > 0
    assert "test-distro" not in command
    assert env["AGENTWORKS_WSL_DISTRO"] == "test-distro"


def test_wsl2_locator_rejects_invalid_provider_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.run_process",
        lambda *_args, **_kwargs: _process_result(status=0, stdout='{"machine_guid":"not-guid"}'),
    )

    with pytest.raises(StateError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )


def test_wsl2_locator_maps_process_failure_to_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.run_process",
        lambda *_args, **_kwargs: _process_result(status=1, stdout=""),
    )

    with pytest.raises(ConnectivityError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )


def test_wsl2_locator_maps_missing_or_duplicate_registration_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    from agentworks.capabilities.vm_platform import wsl2

    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2.run_process",
        lambda *_args, **_kwargs: _process_result(
            status=wsl2._WSL2_LOCATOR_REGISTRATION_MISMATCH_EXIT,
            stdout="",
        ),
    )

    with pytest.raises(StateError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )


def test_wsl2_locator_maps_process_timeout_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()

    def timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return _process_result(failure=Failure.DEADLINE)

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.run_process", timeout)

    with pytest.raises(LimitExceededError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )


def test_wsl2_locator_rejects_a_late_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
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
        "agentworks.capabilities.vm_platform.wsl2.run_process",
        lambda *_args, **_kwargs: _process_result(status=0, stdout="{}"),
    )

    with pytest.raises(LimitExceededError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )

    assert calls == 2


def test_wsl2_locator_keeps_observed_process_failure_over_late_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
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
        "agentworks.capabilities.vm_platform.wsl2.run_process",
        lambda *_args, **_kwargs: _process_result(status=1, stdout=""),
    )

    with pytest.raises(ConnectivityError):
        WSL2Platform("wsl2", {}).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )

    assert calls == 1
