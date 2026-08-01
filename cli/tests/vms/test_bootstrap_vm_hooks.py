"""``bootstrap_vm``'s platform-hook wiring, both directions.

Success: a completed Phase A ends in the ``on_tailscale_ready`` callback
firing (``create_vm`` wires that callback to the platform's
``post_tailscale_ready``, the seam where Azure removes its scoped
bootstrap SSH allow on the happy path). Failure: the kept-FAILED path
calls the platform's ``secure_failed_vm`` hook best-effort (fail closed:
without it a failed Azure VM would keep its bootstrap ingress
indefinitely), and the original error keeps propagating even when the
hook itself fails.

``_phase_a_bootstrap`` is stubbed (its internals are exercised
elsewhere); the real ``bootstrap_vm`` body runs over a real Database row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.vms.initializer import driver

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database


class _SpyPlatform:
    """Platform stand-in recording the hook calls the driver makes."""

    name = "stub"

    def __init__(self) -> None:
        self.secured: list[str] = []
        self.secure_error: Exception | None = None

    def secure_failed_vm(self, vm: object) -> None:
        if self.secure_error is not None:
            raise self.secure_error
        self.secured.append(getattr(vm, "name", "?"))


def _stub_exec_target() -> Any:
    """The provisioning transport as the driver uses it directly: it
    assigns ``.logger`` and calls ``.describe()``."""
    return SimpleNamespace(describe=lambda: "stub-transport", logger=None)


@pytest.fixture
def _hermetic_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the SSH log dir at tmp and stub the function-local imports
    ``bootstrap_vm`` reaches after the hook (reconnect wait, SSH-config
    sync) so the tests never touch the network or the operator's home."""
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    monkeypatch.setattr("agentworks.transports.wait_for_reconnect", lambda *a, **k: True)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


def _call_bootstrap(db: Database, platform: _SpyPlatform, on_ready: Any) -> tuple[Any, Any, str]:
    return driver.bootstrap_vm(
        db,
        SimpleNamespace(),  # type: ignore[arg-type]  # config: unused past the stubbed seams
        SimpleNamespace(swap=0),  # type: ignore[arg-type]  # vm_template: only swap is read
        "hookvm",
        _stub_exec_target(),
        platform,  # type: ignore[arg-type]
        tailscale_auth_key="tskey-test",
        git_tokens={},
        on_tailscale_ready=on_ready,
    )


def test_success_path_fires_on_tailscale_ready(
    db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None
) -> None:
    """A successful Phase A ends in the on_tailscale_ready callback (the
    create_vm wiring through which Azure's post_tailscale_ready removes
    the bootstrap allow) and never touches the fail-closed hook."""
    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    monkeypatch.setattr(driver, "_phase_a_bootstrap", lambda *a, **k: SimpleNamespace())

    fired: list[bool] = []
    platform = _SpyPlatform()
    _call_bootstrap(db, platform, lambda: fired.append(True))

    assert fired == [True]
    assert platform.secured == []


def test_failed_path_secures_the_kept_vm(db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None) -> None:
    """The kept-FAILED path calls secure_failed_vm (fail closed) and never
    the success callback; the original failure propagates."""
    db.insert_vm("hookvm", site="stub", hostname="hookvm")

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("phase a exploded")

    monkeypatch.setattr(driver, "_phase_a_bootstrap", _boom)

    fired: list[bool] = []
    platform = _SpyPlatform()
    with pytest.raises(RuntimeError, match="phase a exploded"):
        _call_bootstrap(db, platform, lambda: fired.append(True))

    assert platform.secured == ["hookvm"]
    assert fired == []


def test_failed_path_hook_failure_does_not_mask(
    db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None, captured_output: Any
) -> None:
    """secure_failed_vm is best-effort: a hook failure warns and the
    ORIGINAL Phase A error still propagates (never the hook's)."""
    db.insert_vm("hookvm", site="stub", hostname="hookvm")

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("phase a exploded")

    monkeypatch.setattr(driver, "_phase_a_bootstrap", _boom)

    platform = _SpyPlatform()
    platform.secure_error = RuntimeError("hook exploded")
    with pytest.raises(RuntimeError, match="phase a exploded"):
        _call_bootstrap(db, platform, lambda: None)

    assert any("could not secure the failed VM" in w for w in captured_output.warnings)
