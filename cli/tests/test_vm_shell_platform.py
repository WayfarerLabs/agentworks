"""Tests for ``agw vm shell``'s routing and guards, and ``--platform``'s native shell.

``--platform`` opens the platform's own shell (``native_shell``) instead of
SSH to the tailnet address, so an operator can reach a VM to fix Tailscale
itself. These tests pin its refusals before the boundary, its routing to the
platform, and the terminal guard around it. They mock the attach layer (a
recording ``VMAccess``) and the platform, so they stay hermetic.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from agentworks.execution.access import Admin
from agentworks.execution.errors import FeatureUnavailable
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.conftest import empty_secret_target, stub_build_registry, stub_vm_gates
from tests.vm_access_support import AccessRecorder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve Registry reads from the module's namespace configs."""
    stub_build_registry(monkeypatch)


def _seed_db(
    tmp_path: Path,
    *,
    platform: str = "lima",
    tailscale_host: str | None = "100.64.0.5",
    init_status: str | None = None,
    provisioning_status: str = "complete",
) -> Database:
    """VM row in a fresh sqlite db. Defaults paint a healthy Lima VM;
    override individual fields to exercise other-platform branches or
    the failed-init / failed-provisioning guard paths."""
    db = Database(tmp_path / "test.db")
    cols = ["name", "site", "hostname", "admin_username", "tailscale_host"]
    vals: list[object] = ["vm1", platform, f"{platform}--vm1", "admin", tailscale_host]
    if init_status is not None:
        cols.append("init_status")
        vals.append(init_status)
    cols.append("provisioning_status")
    vals.append(provisioning_status)
    placeholders = ", ".join(["?"] * len(cols))
    db._conn.execute(
        f"INSERT INTO vms ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    db._conn.commit()
    return db


def _make_config() -> object:
    return SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
    )


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    vm_manager: ModuleType,
    *,
    interactive_log: list[bool],
) -> None:
    """Monkeypatch the boilerplate every shell_vm test needs to stub:
    env resolution, secrets, the recording ``VMAccess``, and the VM gates."""
    monkeypatch.setattr(
        vm_manager,
        "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),  # type: ignore[attr-defined]
    )
    # A real, empty SecretTarget: the orchestrated root registers the
    # env target on the operation's REAL resolver, so a bare object()
    # sentinel no longer survives the seam.
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: empty_secret_target())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    # compose_env normally calls into the secret resolver; stub out the
    # whole thing for tests that aren't exercising env composition.
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    stub_vm_gates(monkeypatch)

    # The admin's attach is the shell the operator sees; count its opens.
    recorder = AccessRecorder()
    monkeypatch.setattr("agentworks.execution.vm_access", recorder.vm_access)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: recorder.client)
    recorder.access(Admin()).attach = lambda request: interactive_log.append(True) or 0  # type: ignore[method-assign]


# -- Tailscale-host gate ------------------------------------------------------


def test_shell_vm_raises_when_no_tailscale_host_and_no_platform_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing behavior: refuse to open a Tailscale-SSH shell when the VM
    has no Tailscale IP. The hint must now mention --platform as the
    escape hatch (it's the whole reason we added the flag)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, tailscale_host=None)

    with pytest.raises(StateError) as exc_info:
        vm_manager.shell_vm(db, _make_config(), "vm1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    assert "Tailscale" in str(err)
    assert "--platform" in (err.hint or "")
    db.close()


# -- --platform: the native shell ---------------------------------------------


def _forbidden(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("reached work the native shell must not do")


def _native_shell_platform(monkeypatch: pytest.MonkeyPatch, status: int, calls: list[str]) -> None:
    """Give the gates' stub platform a native shell that records each VM it opens and exits ``status``."""
    platform = stub_vm_gates(monkeypatch)

    def native_shell(_platform: object, vm: object, ctx: object) -> int:
        calls.append(getattr(vm, "name", "?"))
        return status

    monkeypatch.setattr(type(platform), "native_shell", native_shell, raising=False)


def test_platform_shell_opens_the_native_shell_with_no_tailnet_address_and_no_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM with no tailnet address still gets its native shell, and no environment is composed (R7)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, tailscale_host=None)
    attach_log: list[bool] = []
    _patch_common(monkeypatch, vm_manager, interactive_log=attach_log)
    calls: list[str] = []
    _native_shell_platform(monkeypatch, 0, calls)
    monkeypatch.setattr(vm_manager, "_resolve_vm_admin_env_scopes", _forbidden)
    monkeypatch.setattr(vm_manager, "_vm_secret_target", _forbidden)

    status = vm_manager.shell_vm(
        db,
        _make_config(),  # type: ignore[arg-type]
        "vm1",
        native_shell=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert status == 0
    assert calls == ["vm1"]
    assert attach_log == []  # never the SSH attach
    db.close()


def test_platform_shell_with_a_workspace_is_refused_before_anything_else(tmp_path: Path) -> None:
    """Refused before the VM is even looked up: the database holds no VM, so any later check would fail differently."""
    from agentworks.vms import manager as vm_manager

    db = Database(tmp_path / "test.db")

    with pytest.raises(ValidationError) as caught:
        vm_manager.shell_vm(
            db,
            _make_config(),  # type: ignore[arg-type]
            "vm1",
            native_shell=True,
            workspace_name="ws1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.hint
    db.close()


def test_a_platform_without_a_native_shell_is_refused_before_the_boundary_resolves_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.vms import manager as vm_manager
    from tests._proxmox_fakes import PROXMOX_CONFIG

    db = _seed_db(tmp_path, platform="proxmox")
    platform = ProxmoxPlatform("proxmox", dict(PROXMOX_CONFIG))
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda *_a, **_k: platform)
    monkeypatch.setattr("agentworks.vms.manager.exec.gated_vm_platform_recovery_boundary", _forbidden)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _forbidden)

    with pytest.raises(FeatureUnavailable) as caught:
        vm_manager.shell_vm(
            db,
            _make_config(),  # type: ignore[arg-type]
            "vm1",
            native_shell=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.entity_name == "vm1"
    assert caught.value.hint == ProxmoxPlatform.native_shell_unavailable_hint
    db.close()


@pytest.mark.parametrize("status", [0, 1, 255])
def test_the_native_shell_runs_under_the_terminal_guard_and_255_is_noted_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """The guard's exit pass leaves the alternate screen, so the 255 notice must come after it (I6)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_common(monkeypatch, vm_manager, interactive_log=[])
    events: list[str] = []
    calls: list[str] = []
    _native_shell_platform(monkeypatch, status, calls)
    clean_exits: list[bool] = []

    @contextlib.contextmanager
    def guard() -> Iterator[object]:
        events.append("guard-enter")
        handle = type("Guard", (), {"clean_exit": False})()
        try:
            yield handle
        finally:
            clean_exits.append(handle.clean_exit)
            events.append("guard-exit")

    monkeypatch.setattr("agentworks.terminal.guarded_terminal", guard)
    monkeypatch.setattr("agentworks.output.warn", lambda message: events.append("warn"))

    assert (
        vm_manager.shell_vm(
            db,
            _make_config(),  # type: ignore[arg-type]
            "vm1",
            native_shell=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        == status
    )

    assert len(calls) == 1
    assert events == ["guard-enter", "guard-exit", *(["warn"] if status == 255 else [])]
    assert clean_exits == [status == 0]
    db.close()


# -- Failed-init handling -----------------------------------------------------


def test_shell_vm_warns_but_continues_on_failed_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Shelling into a failed-init VM is the most common reason to open a
    shell on one (investigate, apply a manual fix, re-run reinit). The
    operation must warn rather than raise. The warning text must mention
    'vm reinit' so the operator knows the recovery flow."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, init_status="failed")
    interactive_log: list[bool] = []
    _patch_common(monkeypatch, vm_manager, interactive_log=interactive_log)

    rc = vm_manager.shell_vm(db, _make_config(), "vm1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
    assert rc == 0
    assert interactive_log == [True], "shell must still open on a failed-init VM"

    # The warning should land on stderr (via output.warn) with the reinit hint.
    err = capsys.readouterr().err
    assert "failed initialization" in err
    assert "vm reinit" in err
    db.close()


def test_shell_vm_still_raises_on_failed_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed provisioning is a different beast: the VM may not even be
    reachable, and the project's stance is 'delete and recreate.' That
    block stays hard, even for vm shell. Confirms the warn-on-init
    softening doesn't accidentally also soften provisioning failures."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, provisioning_status="failed")

    with pytest.raises(StateError) as exc_info:
        vm_manager.shell_vm(db, _make_config(), "vm1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    err = exc_info.value
    assert "failed provisioning" in str(err)
    assert "vm delete" in (err.hint or "")
    db.close()


@pytest.mark.parametrize("status", ["pending", "in_progress"])
@pytest.mark.parametrize("allow_failed_init", [False, True])
def test_a_vm_whose_provisioning_never_completed_is_refused(
    tmp_path: Path, status: str, allow_failed_init: bool
) -> None:
    """A killed create can leave a row pending or in progress with no arm run: never attested, so gated."""
    from agentworks.vms.manager import _guard_incomplete_vm

    db = _seed_db(tmp_path, provisioning_status=status)
    vm = db.get_vm("vm1")
    assert vm is not None

    with pytest.raises(StateError) as caught:
        _guard_incomplete_vm(vm, allow_failed_init=allow_failed_init)

    assert caught.value.entity_name == "vm1"
    db.close()


def test_a_completed_vm_passes_the_gate(tmp_path: Path) -> None:
    from agentworks.vms.manager import _guard_incomplete_vm

    db = _seed_db(tmp_path)
    vm = db.get_vm("vm1")
    assert vm is not None

    _guard_incomplete_vm(vm)
    db.close()


def test_exec_vm_warns_but_continues_on_failed_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """vm exec is the non-interactive twin of vm shell: same diagnostic
    primitive in script-friendly form. Running 'agw vm exec failed-vm
    cat /var/log/cloud-init.log' is exactly the workflow we softened
    vm shell for. Confirm exec also warns rather than blocks on
    failed initialization."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, init_status="failed")

    # Stub out everything below the guard so we can confirm the guard
    # warns rather than raises without actually invoking SSH. exec_vm
    # streams through the VMAccess, so a recording one stands in for it.
    monkeypatch.setattr(
        vm_manager,
        "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: empty_secret_target())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    stub_vm_gates(monkeypatch)

    recorder = AccessRecorder()
    monkeypatch.setattr("agentworks.execution.vm_access", recorder.vm_access)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: recorder.client)

    exit_code = vm_manager.exec_vm(db, _make_config(), "vm1", ["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
    assert exit_code == 0  # the underlying exec returned 0

    err = capsys.readouterr().err
    assert "failed initialization" in err
    assert "vm reinit" in err
    db.close()
