"""Gate semantics: ``ensure_active`` / ``keep_active`` and the
operator_stopped flag writes in ``start_vm`` / ``stop_vm``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow


class _GatePlatform:
    """Recording platform double for the gate tests."""

    name = "stub"

    def __init__(self, status: VMStatus = VMStatus.RUNNING) -> None:
        self._status = status
        self.status_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.holds = 0

    def status(self, vm: VMRow) -> VMStatus:
        self.status_calls += 1
        return self._status

    def start(self, vm: VMRow) -> None:
        self.start_calls += 1

    def stop(self, vm: VMRow) -> None:
        self.stop_calls += 1

    def vm_active(self, vm: VMRow, *, config: object | None = None) -> contextlib.AbstractContextManager[None]:
        self.holds += 1
        return contextlib.nullcontext()


def _seed(db: Database, *, tailscale: str | None = "100.64.0.9") -> VMRow:
    db.insert_vm("gvm", site="lima", hostname="gvm")
    if tailscale:
        db.update_vm_tailscale("gvm", tailscale)
    vm = db.get_vm("gvm")
    assert vm is not None
    return vm


def test_fast_path_skips_status(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable Tailscale host short-circuits before any backend
    round trip (no status(), no start())."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    platform = _GatePlatform()

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.status_calls == 0
    assert platform.start_calls == 0


def test_auto_resume_starts_and_holds_through_tailscale(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """STOPPED without operator intent: start, then verify Tailscale
    inside the platform hold (a fresh WSL2 boot must not idle out
    during the handshake wait)."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    order: list[str] = []
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale",
        lambda *a, **k: order.append("tailscale"),
    )
    platform = _GatePlatform(status=VMStatus.STOPPED)

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.start_calls == 1
    assert platform.holds == 1
    assert order == ["tailscale"]


def test_operator_stopped_raises_instead_of_resuming(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)

    with pytest.raises(StateError, match="stopped") as exc:
        vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]
    assert "agw vm start gvm" in (exc.value.hint or "")
    assert platform.start_calls == 0


def test_unknown_status_proceeds_without_start(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient status failure must not trigger a spurious start;
    the operation surfaces the real error downstream."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.UNKNOWN)

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.start_calls == 0


def test_keep_active_gates_then_holds(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    platform = _GatePlatform()

    with vm_manager.keep_active(db, object(), vm, platform):  # type: ignore[arg-type]
        pass

    assert platform.holds == 1


def test_stop_sets_flag_before_already_stopped_shortcut(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """Stopping an already-stopped VM still records the intent: the
    operator means 'keep it stopped' even when it idled out first."""
    _seed(db)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )

    vm_manager.stop_vm(db, object(), "gvm")  # type: ignore[arg-type]

    vm = db.get_vm("gvm")
    assert vm is not None
    assert vm.operator_stopped is True
    assert platform.stop_calls == 0  # short-circuited, flag still set


def test_start_clears_flag(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    _seed(db)
    db.set_operator_stopped("gvm", True)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)

    vm_manager.start_vm(db, object(), "gvm")  # type: ignore[arg-type]

    vm = db.get_vm("gvm")
    assert vm is not None
    assert vm.operator_stopped is False
    assert platform.start_calls == 1
