"""The flagged ops' idempotency guards: a platform whose backend verb
errors on an already-in-state resource must land in that state itself
(the ABC's ``@idempotent_op`` contract on ``start`` / ``stop``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.db import VMStatus


def _vm() -> object:
    return SimpleNamespace(
        name="v1",
        platform_metadata={"distro_name": "v1", "instance_name": "v1"},
    )


def test_lima_start_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm: VMStatus.RUNNING)
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, cmd, **k: (_ for _ in ()).throw(AssertionError(f"must not run: {cmd}")),
    )
    platform.start(_vm())  # type: ignore[arg-type]


def test_lima_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm: VMStatus.STOPPED)
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, cmd, **k: (_ for _ in ()).throw(AssertionError(f"must not run: {cmd}")),
    )
    platform.stop(_vm())  # type: ignore[arg-type]


def test_lima_start_proceeds_when_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    platform = LimaPlatform("lima", {})
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm: VMStatus.STOPPED)
    ran: list[str] = []
    monkeypatch.setattr(
        LimaPlatform, "_run_lima", lambda self, cmd, **k: ran.append(cmd) or ""
    )
    platform.start(_vm())  # type: ignore[arg-type]
    assert ran and "limactl start" in ran[0]


def test_proxmox_start_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    """Constructed without a resolver, any `_api` access raises -- so a
    passing guard proves the API was never touched."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    platform = ProxmoxPlatform(
        "px",
        {"api_url": "https://pve:8006", "node": "n", "token_id": "t", "template_vmid": 1},
    )
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, vm: VMStatus.RUNNING)
    platform.start(_vm())  # type: ignore[arg-type]


def test_proxmox_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    platform = ProxmoxPlatform(
        "px",
        {"api_url": "https://pve:8006", "node": "n", "token_id": "t", "template_vmid": 1},
    )
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, vm: VMStatus.STOPPED)
    platform.stop(_vm())  # type: ignore[arg-type]


def test_wsl2_stop_skips_when_already_stopped(monkeypatch: pytest.MonkeyPatch, captured_output: object) -> None:
    from agentworks.capabilities.vm_platform import wsl2 as wsl2_mod
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    platform = WSL2Platform("wsl2", {})
    monkeypatch.setattr(WSL2Platform, "status", lambda self, vm: VMStatus.STOPPED)
    monkeypatch.setattr(
        wsl2_mod,
        "_wsl",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run wsl")),
    )
    platform.stop(_vm())  # type: ignore[arg-type]
