"""The flagged ops' idempotency guards: a platform whose backend verb
errors on an already-in-state resource must land in that state itself
(the ABC's ``@idempotent_op`` contract on ``start`` / ``stop``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import StateError


def _vm() -> object:
    return SimpleNamespace(
        name="v1",
        platform_metadata={"distro_name": "v1", "instance_name": "v1"},
    )


def test_lima_start_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, cmd, **k: (_ for _ in ()).throw(AssertionError(f"must not run: {cmd}")),
    )
    platform.start(_vm(), RunContext())  # type: ignore[arg-type]


def test_lima_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, cmd, **k: (_ for _ in ()).throw(AssertionError(f"must not run: {cmd}")),
    )
    platform.stop(_vm(), RunContext())  # type: ignore[arg-type]


def test_lima_status_uses_bounded_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    calls: list[dict[str, object]] = []

    def run(_command: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return '{"status":"Running"}\n'

    monkeypatch.setattr(platform, "_run_lima", run)

    assert platform.status(_vm(), RunContext()) is VMStatus.RUNNING  # type: ignore[arg-type]
    assert calls == [{"check": False, "timeout": 10}]


def test_remote_lima_status_rejects_corrupt_stored_instance_name_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform import lima as lima_mod
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    def refuse_transport(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("transport must not receive corrupt persisted state")

    monkeypatch.setattr(lima_mod, "ssh_run", refuse_transport)
    platform = LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "host.example"}})
    vm = SimpleNamespace(name="v1", platform_metadata={"instance_name": "v1; touch /tmp/pwned"})

    with pytest.raises(StateError) as exc_info:
        platform.status(vm, RunContext())  # type: ignore[arg-type]

    assert exc_info.value.entity_kind == "vm"
    assert exc_info.value.entity_name == "v1"
    with pytest.raises(StateError):
        platform.display_backend_name(vm)  # type: ignore[arg-type]


def test_remote_lima_status_accepts_valid_historical_instance_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform import lima as lima_mod
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    calls: list[str] = []

    def run(_target: object, command: str, **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(stdout='{"status":"Running"}\n')

    monkeypatch.setattr(lima_mod, "ssh_run", run)
    platform = LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "host.example"}})
    vm = SimpleNamespace(name="v1", platform_metadata={"instance_name": "legacy--team_vm"})

    assert platform.status(vm, RunContext()) is VMStatus.RUNNING  # type: ignore[arg-type]
    assert calls == ["limactl list --json legacy--team_vm"]
    assert platform.display_backend_name(vm) == "legacy--team_vm@host.example"  # type: ignore[arg-type]


def test_lima_status_launch_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(
        platform,
        "_run_lima",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("limactl")),
    )

    assert platform.status(_vm(), RunContext()) is VMStatus.UNKNOWN  # type: ignore[arg-type]


def test_lima_start_proceeds_when_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED)
    ran: list[str] = []
    monkeypatch.setattr(LimaPlatform, "_run_lima", lambda self, cmd, **k: ran.append(cmd) or "")
    platform.start(_vm(), RunContext())  # type: ignore[arg-type]
    assert ran and "limactl start" in ran[0]


def test_proxmox_start_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    """The empty context refuses any `ctx.secret` read, so a passing
    guard proves the API client was never built."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    platform = ProxmoxPlatform(
        "px",
        {"api_url": "https://pve:8006", "node": "n", "token_id": "t", "template_vmid": 1},
    )
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, vm, ctx: VMStatus.RUNNING)
    platform.start(_vm(), RunContext())  # type: ignore[arg-type]


def test_proxmox_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    platform = ProxmoxPlatform(
        "px",
        {"api_url": "https://pve:8006", "node": "n", "token_id": "t", "template_vmid": 1},
    )
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED)
    platform.stop(_vm(), RunContext())  # type: ignore[arg-type]


def test_proxmox_status_uses_bounded_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    platform = ProxmoxPlatform(
        "px",
        {"api_url": "https://pve:8006", "node": "n", "token_id": "t", "template_vmid": 1},
    )
    calls: list[tuple[str, int, float | None]] = []

    class _API:
        def vm_status(self, node: str, vmid: int, *, timeout: float | None = None) -> dict[str, str]:
            calls.append((node, vmid, timeout))
            return {"status": "running"}

    monkeypatch.setattr(platform, "_api", lambda _ctx: _API())
    vm = SimpleNamespace(name="v1", platform_metadata={"node": "pve", "vmid": 100})

    assert platform.status(vm, RunContext()) is VMStatus.RUNNING  # type: ignore[arg-type]
    assert calls == [("pve", 100, 10)]


def test_wsl2_status_uses_bounded_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform import wsl2 as wsl2_mod
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    calls: list[dict[str, object]] = []

    def run(_args: list[str], **kwargs: object) -> str:
        calls.append(kwargs)
        return "v1 Running 2\n"

    monkeypatch.setattr(wsl2_mod, "_wsl", run)

    assert WSL2Platform("wsl2", {}).status(_vm(), RunContext()) is VMStatus.RUNNING  # type: ignore[arg-type]
    assert calls == [{"check": False, "timeout": 10}]


def test_wsl2_status_launch_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.vm_platform import wsl2 as wsl2_mod
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    monkeypatch.setattr(
        wsl2_mod,
        "_wsl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("wsl")),
    )

    assert WSL2Platform("wsl2", {}).status(_vm(), RunContext()) is VMStatus.UNKNOWN  # type: ignore[arg-type]


def test_wsl2_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform import wsl2 as wsl2_mod
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    platform = WSL2Platform("wsl2", {})
    monkeypatch.setattr(WSL2Platform, "status", lambda self, vm, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(
        wsl2_mod,
        "_wsl",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run wsl")),
    )
    platform.stop(_vm(), RunContext())  # type: ignore[arg-type]
