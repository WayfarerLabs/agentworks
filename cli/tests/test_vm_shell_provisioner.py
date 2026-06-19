"""Tests for ``agw vm shell --provisioner``.

The provisioner-shell path uses the platform-native transport (limactl
shell, wsl.exe, Azure SSH via public IP) instead of Tailscale SSH. It
exists primarily so an operator can reach a VM to fix Tailscale itself
(e.g. the issue #117 latched DNS state, whose heal involves stopping
tailscaled and would terminate a Tailscale-SSH session mid-sequence).

These tests pin the branching behavior in ``shell_vm`` and the typed-
error wrapping in ``_provisioner_shell_target``. They mock the
interactive SSH layer so the tests stay hermetic.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError

if TYPE_CHECKING:
    pass


def _seed_db(tmp_path: Path, *, tailscale_host: str | None = "100.64.0.5") -> Database:
    """VM row in a fresh sqlite db. tailscale_host is parameterizable so we
    can exercise both the tailscale-present and tailscale-absent branches."""
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES (?, ?, ?, ?)",
        ("vm1", "lima", "admin", tailscale_host),
    )
    db._conn.commit()
    return db


def _stub_target() -> object:
    """Minimal ExecTarget-shaped object that interactive() will accept."""
    return SimpleNamespace(ssh=SimpleNamespace(host="ssh-host"), lima=None, wsl2=None)


class _NullCM:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> None:
        return None


def _make_config() -> object:
    return SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
        secret_resolver=None,
    )


def _patch_common(
    monkeypatch: pytest.MonkeyPatch, vm_manager: object, *, interactive_log: list[bool],
) -> None:
    """Monkeypatch the boilerplate every shell_vm test needs to stub:
    env resolution, secrets, interactive, keep_vm_active."""
    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    # compose_env normally calls into the secret resolver; stub out the
    # whole thing for tests that aren't exercising env composition.
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    monkeypatch.setattr(vm_manager, "keep_vm_active", lambda *a, **k: _NullCM())

    def _track_interactive(*_a: object, **_k: object) -> int:
        interactive_log.append(True)
        return 0

    monkeypatch.setattr("agentworks.ssh.interactive", _track_interactive)


# -- Tailscale-host gate ------------------------------------------------------


def test_shell_vm_raises_when_no_tailscale_host_and_no_provisioner_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing behavior: refuse to open a Tailscale-SSH shell when the VM
    has no Tailscale IP. The hint must now mention --provisioner as the
    escape hatch (it's the whole reason we added the flag)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, tailscale_host=None)

    with pytest.raises(StateError) as exc_info:
        vm_manager.shell_vm(db, _make_config(), "vm1")  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    assert "Tailscale" in str(err)
    assert "--provisioner" in (err.hint or "")
    db.close()


def test_shell_vm_provisioner_flag_bypasses_tailscale_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--provisioner is the path operators use when Tailscale isn't
    available (or is the thing they're trying to fix). The function must
    not raise the no-Tailscale-IP error when the flag is set."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, tailscale_host=None)
    interactive_log: list[bool] = []
    _patch_common(monkeypatch, vm_manager, interactive_log=interactive_log)
    monkeypatch.setattr(
        vm_manager, "_provisioner_shell_target",
        lambda *a, **k: _stub_target(),
    )

    # SystemExit is fine; interactive() is stubbed to return 0 and
    # shell_vm wraps it in sys.exit().
    with pytest.raises(SystemExit) as exc_info:
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, _make_config(), "vm1", provisioner=True,
        )
    assert exc_info.value.code == 0
    assert interactive_log == [True], "the interactive shell should have been opened"
    db.close()


# -- Provisioner-target routing ----------------------------------------------


def test_shell_vm_provisioner_uses_provisioner_admin_exec_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --provisioner is set, shell_vm must route through
    get_provisioner_for_vm().admin_exec_target(), NOT the
    ssh.admin_exec_target (which would go via Tailscale)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)  # tailscale_host populated; should still route via provisioner
    interactive_log: list[bool] = []
    _patch_common(monkeypatch, vm_manager, interactive_log=interactive_log)

    provisioner_target_calls: list[tuple[str, object]] = []

    class _StubProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            provisioner_target_calls.append((getattr(vm, "name", "?"), config))
            return _stub_target()

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _StubProvisioner(),
    )

    # Also pin the SSH path so it would explode if accidentally taken.
    def _ssh_admin_exec_target_must_not_be_called(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "ssh.admin_exec_target must not be called when --provisioner is set",
        )

    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target",
        _ssh_admin_exec_target_must_not_be_called,
    )

    with pytest.raises(SystemExit):
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, _make_config(), "vm1", provisioner=True,
        )

    assert len(provisioner_target_calls) == 1
    assert provisioner_target_calls[0][0] == "vm1"
    db.close()


# -- Typed-error wrapping for unavailable provisioner shells ------------------


def test_provisioner_shell_target_wraps_notimplementederror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxmox's provisioner shell raises NotImplementedError. The wrapper
    must convert that into a typed StateError so the CLI renders it as
    a one-liner with the provisioner's message as the hint, rather than
    leaking a Python traceback to the operator."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)

    class _UnsupportedProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            raise NotImplementedError("Proxmox provisioning transport not yet implemented.")

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _UnsupportedProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with pytest.raises(StateError) as exc_info:
        vm_manager._provisioner_shell_target(db, _make_config(), vm)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    assert "not yet implemented" in (err.hint or "")
    db.close()


def test_provisioner_shell_target_raises_when_no_public_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure's admin_exec_target returns an SSHTarget with host="" when
    no public IP is attached. The wrapper must catch that empty host and
    raise StateError pointing the operator at the Azure serial console
    rather than letting the empty-host SSH attempt fail cryptically."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)

    class _AzureNoPublicIP:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            # Shape matches what azure.py returns when _get_vm_public_ip()
            # returns "" because no public IP is attached.
            return SimpleNamespace(
                ssh=SimpleNamespace(host=""), lima=None, wsl2=None,
            )

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _AzureNoPublicIP(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with pytest.raises(StateError) as exc_info:
        vm_manager._provisioner_shell_target(db, _make_config(), vm)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    assert "public IP" in str(err)
    assert "serial console" in (err.hint or "")
    db.close()
