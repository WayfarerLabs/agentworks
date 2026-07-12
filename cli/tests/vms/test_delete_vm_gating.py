"""``delete_vm`` cleanup discipline: never gates, never lets a
best-effort step (bind, hold, logout) skip the backend delete, and
keeps the SIGINT contract at a site-secret prompt.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import VMStatus
from agentworks.errors import ConfigError, UserAbort
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow
    from tests.conftest import CapturedOutput


class _DeletePlatform:
    name = "stub"

    def __init__(self, *, hold_raises: bool = False) -> None:
        self.hold_raises = hold_raises
        self.status_calls = 0
        self.delete_calls = 0

    def status(self, vm: VMRow) -> VMStatus:
        self.status_calls += 1
        return VMStatus.STOPPED

    def vm_active(self, vm: VMRow, *, config: object | None = None) -> contextlib.AbstractContextManager[None]:
        if self.hold_raises:
            raise RuntimeError("keepalive exited immediately")
        return contextlib.nullcontext()

    def delete(self, vm: VMRow) -> None:
        self.delete_calls += 1


@pytest.fixture(autouse=True)
def _no_ssh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


def _seed(db: Database, *, tailscale: str | None = "100.64.0.3") -> None:
    db.insert_vm("dvm", site="lima", hostname="dvm")
    if tailscale:
        db.update_vm_tailscale("dvm", tailscale)
    db.set_operator_stopped("dvm", True)  # must not matter: delete never gates


def test_delete_never_gates(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """An operator-stopped VM deletes cleanly: no gate, no StateError,
    no status probe."""
    _seed(db)
    platform = _DeletePlatform()
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)

    vm_manager.delete_vm(db, object(), "dvm", yes=True)  # type: ignore[arg-type]

    assert platform.status_calls == 0
    assert platform.delete_calls == 1
    assert db.get_vm("dvm") is None


def test_hold_failure_does_not_skip_delete(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A broken hold (e.g. a manually unregistered WSL2 distro) is
    exactly what delete cleans up: warn and keep going."""
    _seed(db)
    platform = _DeletePlatform(hold_raises=True)
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )

    vm_manager.delete_vm(db, object(), "dvm", yes=True)  # type: ignore[arg-type]

    assert platform.delete_calls == 1
    assert db.get_vm("dvm") is None
    assert any("logout skipped" in w for w in captured_output.warnings)


def test_logout_failure_does_not_skip_delete(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    _seed(db)
    platform = _DeletePlatform()
    monkeypatch.setattr(
        vm_manager, "bind_platform", lambda config, vm, registry=None: platform
    )

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(vm_manager, "_tailscale_logout", _boom)

    vm_manager.delete_vm(db, object(), "dvm", yes=True)  # type: ignore[arg-type]

    assert platform.delete_calls == 1
    assert db.get_vm("dvm") is None


def test_bind_failure_warns_with_hint_and_still_deletes_row(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A stranded site (R3) degrades: backend cleanup is skipped with
    the manifest hint rendered, the DB row still goes."""
    _seed(db)

    def _stranded(config: object, vm: object, registry: object = None) -> object:
        raise ConfigError("site 'gone' is not declared", hint="kind: vm-site")

    monkeypatch.setattr(vm_manager, "bind_platform", _stranded)

    vm_manager.delete_vm(db, object(), "dvm", yes=True)  # type: ignore[arg-type]

    assert db.get_vm("dvm") is None
    joined = "\n".join(captured_output.warnings)
    assert "skipping backend cleanup" in joined
    assert "kind: vm-site" in joined


def test_user_abort_at_bind_aborts_the_delete(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Ctrl-C at a site-secret prompt aborts the whole delete rather
    than orphaning the backend VM behind a warn."""
    _seed(db)

    def _abort(config: object, vm: object, registry: object = None) -> object:
        raise UserAbort("cancelled at prompt")

    monkeypatch.setattr(vm_manager, "bind_platform", _abort)

    with pytest.raises(UserAbort):
        vm_manager.delete_vm(db, object(), "dvm", yes=True)  # type: ignore[arg-type]

    assert db.get_vm("dvm") is not None
