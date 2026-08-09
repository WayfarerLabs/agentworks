"""``bootstrap_vm``'s platform-hook wiring, both directions.

Success: a completed Phase A ends in the ``on_tailscale_ready`` callback
firing (``create_vm`` wires that callback to the platform's
``post_tailscale_ready``, the seam where Azure removes its scoped
bootstrap SSH allow on the happy path). Failure: the kept-FAILED path
calls the platform's ``secure_failed_vm`` hook best-effort (fail closed:
without it a failed Azure VM would keep its bootstrap ingress
indefinitely), and the original error keeps propagating even when the
hook itself fails. The same fail-closed contract holds for an operator
interrupt (KeyboardInterrupt escapes the Exception arm but still
secures, without touching the row's status).

``_phase_a_bootstrap`` is stubbed (its internals are exercised
elsewhere); the real ``bootstrap_vm`` body runs over a real Database row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.ssh import SSHLogger
from agentworks.vms.initializer import driver

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database


class _SpyPlatform:
    """Platform stand-in recording the hook calls the driver makes."""

    name = "stub"

    def __init__(self) -> None:
        self.secured: list[str] = []
        self.secure_error: BaseException | None = None

    def secure_failed_vm(self, vm: object, ctx: object) -> None:
        if self.secure_error is not None:
            raise self.secure_error
        self.secured.append(getattr(vm, "name", "?"))


def _stub_exec_target() -> Any:
    """The provisioning transport as the driver uses it directly: it
    assigns ``.logger`` and calls ``.describe()``."""
    return SimpleNamespace(describe=lambda: "stub-transport", logger=None)


def _tailscale_ctx(secret: str) -> RunContext:
    return RunContext(secrets=ScopedSecrets({"tailscale-auth-key": secret}, ("tailscale-auth-key",)))


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
        SimpleNamespace(swap=0, tailscale_auth_key="tailscale-auth-key"),  # type: ignore[arg-type]
        "hookvm",
        _stub_exec_target(),
        platform,  # type: ignore[arg-type]
        RunContext(),
        admin_username="agentworks",
        tailscale_ctx=_tailscale_ctx("tskey-test"),
        git_tokens={},
        on_tailscale_ready=on_ready,
    )


def test_bootstrap_logger_receives_every_resolved_secret(
    db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None
) -> None:
    """The VM-create boundary supplies both the Tailscale key and every
    credential token before the incremental logger writes its header."""
    captured: list[tuple[str, ...]] = []

    class _LoggerSpy:
        path = "/dev/null"

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            captured.append(redactions)

        def close(self) -> None:
            pass

    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)
    monkeypatch.setattr(driver, "_phase_a_bootstrap", lambda *a, **k: SimpleNamespace())

    driver.bootstrap_vm(
        db,
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(swap=0, tailscale_auth_key="tailscale-auth-key"),  # type: ignore[arg-type]
        "hookvm",
        _stub_exec_target(),
        _SpyPlatform(),  # type: ignore[arg-type]
        RunContext(),
        admin_username="admin",
        tailscale_ctx=_tailscale_ctx("tailscale-secret"),
        git_tokens={"gh": "github-secret", "gl": "gitlab-secret"},
        on_tailscale_ready=lambda: None,
    )

    assert captured == [("tailscale-secret", "github-secret", "gitlab-secret")]


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


def test_interrupt_during_bootstrap_secures_and_reraises(
    db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None
) -> None:
    """Ctrl-C during the minutes-long bootstrap escapes the Exception arm
    (KeyboardInterrupt is a BaseException), but the kept VM must still be
    secured best-effort before the interrupt propagates: without it the
    Azure bootstrap allow would stand indefinitely. The row's status is
    left as the abort found it (nothing marks FAILED on this path), and
    the success callback never fires."""
    from agentworks.db import ProvisioningStatus

    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    row_before = db.get_vm("hookvm")
    assert row_before is not None

    def _interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(driver, "_phase_a_bootstrap", _interrupt)

    fired: list[bool] = []
    platform = _SpyPlatform()
    with pytest.raises(KeyboardInterrupt):
        _call_bootstrap(db, platform, lambda: fired.append(True))

    assert platform.secured == ["hookvm"]
    assert fired == []
    row_after = db.get_vm("hookvm")
    assert row_after is not None
    # Status untouched by the interrupt path: in particular, never FAILED.
    assert row_after.provisioning_status == row_before.provisioning_status
    assert row_after.provisioning_status != ProvisioningStatus.FAILED.value


def test_interrupt_hook_failure_does_not_mask_the_interrupt(
    db: Database, monkeypatch: pytest.MonkeyPatch, _hermetic_driver: None, captured_output: Any
) -> None:
    """A secure_failed_vm failure on the interrupt path warns and the
    KeyboardInterrupt still propagates (never the hook's error)."""
    db.insert_vm("hookvm", site="stub", hostname="hookvm")

    def _interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(driver, "_phase_a_bootstrap", _interrupt)

    platform = _SpyPlatform()
    platform.secure_error = RuntimeError("hook exploded")
    with pytest.raises(KeyboardInterrupt):
        _call_bootstrap(db, platform, lambda: None)

    assert any("could not secure the interrupted VM" in w for w in captured_output.warnings)


@pytest.mark.parametrize(
    ("primary", "hook_failure", "warning_failure"),
    [
        (RuntimeError("primary"), RuntimeError("hook"), RuntimeError("warn")),
        (KeyboardInterrupt("primary"), KeyboardInterrupt("hook"), KeyboardInterrupt("warn")),
        (SystemExit(41), SystemExit(42), SystemExit(43)),
        (GeneratorExit(), GeneratorExit(), GeneratorExit()),
    ],
    ids=("exception", "keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_secure_hook_and_every_failure_warning_preserve_exact_primary(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_driver: None,
    primary: BaseException,
    hook_failure: BaseException,
    warning_failure: BaseException,
) -> None:
    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    warning_calls: list[str] = []

    def fail_phase(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise primary

    def fail_warning(message: str) -> None:
        warning_calls.append(message)
        raise warning_failure

    platform = _SpyPlatform()
    platform.secure_error = hook_failure
    monkeypatch.setattr(driver, "_phase_a_bootstrap", fail_phase)
    monkeypatch.setattr("agentworks.vms.initializer.failure_cleanup.output.warn", fail_warning)

    with pytest.raises(type(primary)) as caught:
        _call_bootstrap(db, platform, lambda: None)

    assert caught.value is primary
    state = "failed" if isinstance(primary, Exception) else "interrupted"
    assert warning_calls[0] == f"could not secure the {state} VM"
    if isinstance(primary, Exception):
        assert warning_calls[1].startswith("Log: ")


def test_log_path_rendering_failure_cannot_replace_bootstrap_primary(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_driver: None,
) -> None:
    primary = RuntimeError("bootstrap primary")
    rendering_failure = SystemExit(44)
    db.insert_vm("hookvm", site="stub", hostname="hookvm")

    def fail_phase(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise primary

    def fail_display_path(self: SSHLogger) -> str:
        del self
        raise rendering_failure

    monkeypatch.setattr(driver, "_phase_a_bootstrap", fail_phase)
    monkeypatch.setattr(SSHLogger, "display_path", property(fail_display_path))

    with pytest.raises(RuntimeError) as caught:
        _call_bootstrap(db, _SpyPlatform(), lambda: None)

    assert caught.value is primary


@pytest.mark.parametrize(
    "primary",
    [RuntimeError("bootstrap failed"), KeyboardInterrupt("bootstrap interrupted")],
    ids=("exception", "base-exception"),
)
def test_bootstrap_logger_close_failure_never_masks_primary(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_driver: None,
    captured_output: Any,
    primary: BaseException,
) -> None:
    db.insert_vm("hookvm", site="stub", hostname="hookvm")

    def fail_phase(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise primary

    def fail_close(self: object) -> None:
        del self
        raise SystemExit(91)

    monkeypatch.setattr(driver, "_phase_a_bootstrap", fail_phase)
    monkeypatch.setattr("agentworks.ssh.SSHLogger.close", fail_close)

    with pytest.raises(type(primary)) as caught:
        _call_bootstrap(db, _SpyPlatform(), lambda: None)

    assert caught.value is primary
    assert captured_output.warnings.count("could not close the VM operation log after failure") == 1


@pytest.mark.parametrize(
    "primary",
    [RuntimeError("initialization failed"), GeneratorExit()],
    ids=("exception", "base-exception"),
)
def test_initialization_logger_close_failure_never_masks_primary(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: Any,
    primary: BaseException,
) -> None:
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    logger = SSHLogger("hookvm", "vm-create")

    def fail_phase(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise primary

    def fail_close() -> None:
        raise SystemExit(92)

    monkeypatch.setattr(driver, "_phase_b_setup", fail_phase)
    monkeypatch.setattr(logger, "close", fail_close)

    with pytest.raises(type(primary)) as caught:
        driver.run_initialization(
            db,
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            "hookvm",
            SimpleNamespace(),  # type: ignore[arg-type]
            {},
            "/home/agentworks",
            "agentworks",
            logger,
            git_tokens={},
        )

    assert caught.value is primary
    assert captured_output.warnings.count("could not close the VM operation log after failure") == 1


def test_close_and_warning_sink_failures_cannot_replace_initialization_primary(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = GeneratorExit()
    close_failure = SystemExit(92)
    warning_failure = KeyboardInterrupt("warning sink interrupted")
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    db.insert_vm("hookvm", site="stub", hostname="hookvm")
    logger = SSHLogger("hookvm", "vm-create")

    def fail_phase(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise primary

    def fail_close() -> None:
        raise close_failure

    def fail_warning(message: str) -> None:
        del message
        raise warning_failure

    monkeypatch.setattr(driver, "_phase_b_setup", fail_phase)
    monkeypatch.setattr(logger, "close", fail_close)
    monkeypatch.setattr("agentworks.vms.initializer.driver.output.warn", fail_warning)

    with pytest.raises(GeneratorExit) as caught:
        driver.run_initialization(
            db,
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            "hookvm",
            SimpleNamespace(),  # type: ignore[arg-type]
            {},
            "/home/agentworks",
            "agentworks",
            logger,
            git_tokens={},
        )

    assert caught.value is primary
