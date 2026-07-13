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


def test_manually_stopped_raises_instead_of_resuming(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the operator's own action and skips the
    Tailscale probe entirely: pinging a stopped VM would burn the
    probe's full timeout just to reach this error."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None

    def _no_ping(host: str) -> bool:
        raise AssertionError("reachability probed for a manually stopped VM")

    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", _no_ping)
    platform = _GatePlatform(status=VMStatus.STOPPED)

    with pytest.raises(StateError, match="manually stopped") as exc:
        vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]
    assert "not be auto-started" in str(exc.value)
    assert "agw vm start gvm" in (exc.value.hint or "")
    assert platform.start_calls == 0


def test_manually_stopped_but_running_out_of_band_proceeds(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is intent, not observed state: a VM started outside
    agw (limactl/wsl directly) is usable, so a RUNNING status proceeds
    without a start and without raising."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None
    monkeypatch.setattr(
        vm_manager, "_is_tailscale_reachable", lambda host: False
    )
    platform = _GatePlatform(status=VMStatus.RUNNING)

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.start_calls == 0


def test_concurrent_start_clears_the_flag_and_resumes(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """The mirror of the concurrent-stop re-read: a `vm start` in
    another terminal cleared the flag after the caller's row load, so
    the gate auto-resumes instead of refusing on stale intent."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")  # loaded with operator_stopped=True
    assert vm is not None
    db.set_operator_stopped("gvm", False)  # another terminal starts it
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.STOPPED)

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.start_calls == 1


def test_deallocated_auto_resumes_like_stopped(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """DEALLOCATED (the Azure-relevant observed value) takes the same
    resume branch as STOPPED."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.DEALLOCATED)

    vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]

    assert platform.start_calls == 1


def test_flag_is_reread_on_the_slow_path(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent `vm stop` between the caller's row load and the
    gate must not be auto-undone: the slow path re-reads the flag."""
    vm = _seed(db)  # loaded with operator_stopped=False
    db.set_operator_stopped("gvm", True)  # another terminal stops it
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)

    with pytest.raises(StateError, match="stopped"):
        vm_manager.ensure_active(db, object(), vm, platform)  # type: ignore[arg-type]
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
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output
) -> None:
    """Stopping an idle-stopped VM still records the intent -- and the
    message says so instead of the misleading bare 'already stopped'
    (the command DID change something: auto-start is now off)."""
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
    (message,) = captured_output.info
    assert "stopped on its own" in message
    assert "will not be auto-started" in message


def test_stop_of_a_manually_stopped_vm_is_a_true_noop(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output
) -> None:
    """Only when the intent was ALREADY recorded does 'already' apply,
    and it names the manual state."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )

    vm_manager.stop_vm(db, object(), "gvm")  # type: ignore[arg-type]

    (message,) = captured_output.info
    assert message == "VM 'gvm' is already manually stopped"


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
