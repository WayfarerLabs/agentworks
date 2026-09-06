"""Cloud-init readiness behavior for the Proxmox platform."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ProvisioningError
from agentworks.output import Role
from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


class _CloudInitAPI:
    def __init__(
        self,
        *results: dict[str, Any] | None | BaseException,
        advance: Callable[[float], None] | None = None,
    ) -> None:
        self.results = list(results)
        self.advance = advance
        self.calls = 0
        self.timeouts: list[float] = []

    def guest_agent_exec_wait(
        self,
        node: str,
        vmid: int,
        command: str,
        args: list[str] | None = None,
        *,
        timeout: float = 60,
    ) -> dict[str, Any] | None:
        del node, vmid, command, args
        self.calls += 1
        self.timeouts.append(timeout)
        if self.advance is not None:
            self.advance(timeout / 2)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _platform(api: _CloudInitAPI) -> ProxmoxPlatform:
    platform = ProxmoxPlatform(
        "pve-site",
        {
            "api_url": "https://pve.example.com:8006",
            "node": "pve1",
            "token_id": "agw@pam!agw",
            "template_vmids": {"trixie": 9001},
        },
    )
    platform._api_cached = cast(ProxmoxAPI, api)
    return platform


def test_cloud_init_success_returns_without_warning(captured_output: CapturedOutput) -> None:
    api = _CloudInitAPI({"exited": True, "exitcode": 0})

    _platform(api)._wait_for_cloud_init("pve1", 101, RunContext(), timeout=1)

    assert api.calls == 1
    assert all(role is not Role.WARNING for role, _level, _message in captured_output.lines)


def test_cloud_init_degraded_completion_returns_with_warning(captured_output: CapturedOutput) -> None:
    api = _CloudInitAPI({"exited": True, "exitcode": 2})

    _platform(api)._wait_for_cloud_init("pve1", 101, RunContext(), timeout=1)

    assert api.calls == 1
    assert [role for role, _level, _message in captured_output.lines] == [Role.WARNING]


@pytest.mark.parametrize(
    "result",
    [
        {"exited": True, "exitcode": 1},
        {"exited": True, "signal": 15},
    ],
    ids=("exit-code", "signal"),
)
def test_cloud_init_completed_failure_is_immediate(result: dict[str, Any]) -> None:
    api = _CloudInitAPI(result)

    with pytest.raises(ProvisioningError):
        _platform(api)._wait_for_cloud_init("pve1", 101, RunContext(), timeout=1)

    assert api.calls == 1


def test_cloud_init_timeout_retains_last_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_error = ProxmoxAPIError("provider diagnostic token")
    api = _CloudInitAPI(provider_error)
    now = 0.0

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.monotonic", lambda: now)
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.sleep", advance)

    with pytest.raises(ProvisioningError) as caught:
        _platform(api)._wait_for_cloud_init("pve1", 101, RunContext(), timeout=1)

    assert api.calls == 1
    assert caught.value.__cause__ is provider_error
    assert "provider diagnostic token" in str(caught.value)


def test_cloud_init_wait_does_not_exceed_small_outer_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    sleeps: list[float] = []

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        advance(seconds)

    api = _CloudInitAPI(ProxmoxAPIError("not ready"), advance=advance)
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.monotonic", lambda: now)
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.sleep", sleep)

    with pytest.raises(ProvisioningError):
        _platform(api)._wait_for_cloud_init("pve1", 101, RunContext(), timeout=0.25)

    assert api.timeouts == [0.25]
    assert sleeps == [0.125]
    assert now == pytest.approx(0.25)
