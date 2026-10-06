"""Passive bounded power admission for native existing-VM work."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import VMRow, VMStatus
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline


def _vm(name: str = "日本語 Distro") -> VMRow:
    return cast("VMRow", SimpleNamespace(name="vm-one", platform_metadata={"distro_name": name}))


def _platform() -> WSL2Platform:
    return WSL2Platform("wsl2", {})


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ("* 日本語 Distro          Running         2\r\n", VMStatus.RUNNING),
        ("  日本語 Distro          Stopped         2\r\n", VMStatus.STOPPED),
        ("  日本語 Distro          Installing      2\r\n", VMStatus.UNKNOWN),
        ("  Other                  Running         2\r\n", VMStatus.UNKNOWN),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-8"])
def test_wsl2_power_observer_preserves_name_and_never_enters_guest(
    monkeypatch: pytest.MonkeyPatch, listing: str, expected: VMStatus, encoding: str
) -> None:
    local_delivery = LocalDeliveryCustody()
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=listing.encode(encoding)))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)
    deadline = Deadline.after(10)

    assert (
        _platform().observe_execution_power(_vm(), RunContext(), deadline=deadline, custody=local_delivery) is expected
    )

    run.assert_called_once()
    assert run.call_args.args[0] == ["wsl", "--list", "--verbose"]
    assert run.call_args.kwargs["capture_output"] is True
    assert 0 < run.call_args.kwargs["timeout"] <= 10


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00", b"x\x00", b"x" * 65_537])
def test_wsl2_malformed_listing_is_unknown(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    local_delivery = LocalDeliveryCustody()
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=raw))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)
    assert (
        _platform().observe_execution_power(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
        is VMStatus.UNKNOWN
    )


def test_wsl2_expired_power_budget_refuses_before_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    run = MagicMock()
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)
    with pytest.raises(LimitExceededError):
        _platform().observe_execution_power(_vm(), RunContext(), deadline=Deadline.after(0), custody=local_delivery)
    run.assert_not_called()
    with pytest.raises(ValidationError):
        _platform().observe_execution_power(_vm(), RunContext(), deadline=Deadline(None), custody=local_delivery)
    run.assert_not_called()


def test_wsl2_late_power_result_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=b"vm-one  Running  2\n"))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)
    calls = 0

    def remaining(deadline: Deadline, *, vm_name: str) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired", entity_kind="vm", entity_name=vm_name)
        return 1

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.execution_power_remaining", remaining)
    with pytest.raises(LimitExceededError):
        _platform().observe_execution_power(
            _vm("vm-one"), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )
    run.assert_called_once()


def test_wsl2_power_timeout_refuses_without_guest_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    run = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="wsl", timeout=0.1))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2.subprocess.run", run)
    with pytest.raises(LimitExceededError):
        _platform().observe_execution_power(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)


def test_platform_without_finite_observer_does_not_fall_back_to_status() -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _platform()
    with pytest.raises(StateError):
        VMPlatform.observe_execution_power(
            platform, _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )
