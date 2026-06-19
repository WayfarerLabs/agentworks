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


def _seed_db(
    tmp_path: Path,
    *,
    platform: str = "lima",
    tailscale_host: str | None = "100.64.0.5",
    init_status: str | None = None,
    provisioning_status: str | None = None,
) -> Database:
    """VM row in a fresh sqlite db. Defaults paint a healthy Lima VM;
    override individual fields to exercise other-platform branches or
    the failed-init / failed-provisioning guard paths."""
    db = Database(tmp_path / "test.db")
    cols = ["name", "platform", "admin_username", "tailscale_host"]
    vals: list[object] = ["vm1", platform, "admin", tailscale_host]
    if init_status is not None:
        cols.append("init_status")
        vals.append(init_status)
    if provisioning_status is not None:
        cols.append("provisioning_status")
        vals.append(provisioning_status)
    placeholders = ", ".join(["?"] * len(cols))
    db._conn.execute(
        f"INSERT INTO vms ({', '.join(cols)}) VALUES ({placeholders})", vals,
    )
    db._conn.commit()
    return db


def _stub_target() -> object:
    """Minimal ExecTarget-shaped object that interactive() and the
    reachability probe in _provisioner_shell_target both accept.

    ``run`` returns an SSHResult-shaped object so ``target.run('echo ok',
    timeout=10)`` succeeds without invoking a real subprocess.
    """
    return SimpleNamespace(
        ssh=SimpleNamespace(host="ssh-host"),
        lima=None,
        wsl2=None,
        run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="ok", stderr="", ok=True),
    )


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
    """A NotImplementedError from the provisioner (e.g. Proxmox today) is
    wrapped into a typed StateError so the CLI renders it as a one-liner
    rather than leaking a Python traceback.

    Uses a non-proxmox platform so the generic hint path is exercised; the
    Proxmox-specific hint shape is tested separately below."""
    import contextlib

    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, platform="lima")

    class _UnsupportedProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            raise NotImplementedError("provisioning transport not yet implemented.")

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _UnsupportedProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack, pytest.raises(StateError) as exc_info:
        vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    # Generic platforms get the NotImplementedError's text as the hint.
    assert "not yet implemented" in (err.hint or "")
    db.close()


def test_provisioner_shell_target_proxmox_hint_points_at_web_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Proxmox specifically, the NotImplementedError wrapper substitutes
    a Proxmox-specific hint pointing the operator at the web UI's serial
    console. The guest agent's exec interface can't carry an interactive
    session, so the web console is the realistic escape hatch and the
    operator should hear that directly rather than the generic NIE text."""
    import contextlib

    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, platform="proxmox")

    class _ProxmoxProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            raise NotImplementedError("Proxmox provisioning transport not yet implemented.")

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _ProxmoxProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack, pytest.raises(StateError) as exc_info:
        vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    hint = err.hint or ""
    # Proxmox-specific guidance: point at the web UI's serial console as
    # the equivalent of the per-platform escape hatch other provisioners
    # have (limactl shell, wsl.exe, Azure public IP attach).
    assert "serial console" in hint
    assert "Proxmox VE web UI" in hint
    db.close()


def test_provisioner_shell_target_attaches_and_registers_detach_for_azure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Azure, the provisioner shell must attach a temporary public IP
    AND register the detach as an ExitStack callback. The operator should
    never see 'public IP required' on a healthy Azure VM, and the IP must
    come down regardless of how shell_vm unwinds (success, SSH failure, ^C)."""
    import contextlib

    from agentworks.vms import manager as vm_manager
    from agentworks.vms.provisioners.azure import AzureProvisioner

    db = _seed_db(tmp_path)

    attach_calls: list[str] = []
    detach_calls: list[str] = []

    class _FakeAzureProvisioner(AzureProvisioner):
        # Override the constructor so we don't need a real Azure config.
        def __init__(self) -> None:  # noqa: D401, ANN001
            pass

        def attach_public_ip(self, vm: object) -> str:
            attach_calls.append(getattr(vm, "name", "?"))
            return "203.0.113.42"

        def detach_public_ip(self, vm: object) -> None:
            detach_calls.append(getattr(vm, "name", "?"))

        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            # Include a stub `run` so the reachability probe in
            # _provisioner_shell_target succeeds without invoking a
            # real ssh subprocess.
            return SimpleNamespace(
                ssh=SimpleNamespace(host="203.0.113.42"),
                lima=None,
                wsl2=None,
                run=lambda *_a, **_k: SimpleNamespace(
                    returncode=0, stdout="ok", stderr="", ok=True,
                ),
            )

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _FakeAzureProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack:
        target = vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]
        # Attach must have run inside the stack scope.
        assert attach_calls == ["vm1"]
        # Detach must NOT have run yet; it should fire on stack exit.
        assert detach_calls == []
        assert target.ssh.host == "203.0.113.42"

    # Stack exited: detach must have run exactly once.
    assert detach_calls == ["vm1"]
    db.close()


def test_provisioner_shell_target_detaches_on_exception_for_azure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detach callback must be registered BEFORE any post-attach call
    that could raise, so detach fires regardless of how the function
    unwinds. Without this contract a future refactor that moves
    stack.callback after admin_exec_target would silently leak a public
    IP on every error path.

    Test: make admin_exec_target raise after attach_public_ip ran;
    assert detach was still called via the registered callback when the
    surrounding ExitStack closes."""
    import contextlib

    from agentworks.vms import manager as vm_manager
    from agentworks.vms.provisioners.azure import AzureProvisioner

    db = _seed_db(tmp_path)
    detach_calls: list[str] = []

    class _AzureRaisesAfterAttach(AzureProvisioner):
        def __init__(self) -> None:  # noqa: D401
            pass

        def attach_public_ip(self, vm: object) -> str:
            return "203.0.113.42"

        def detach_public_ip(self, vm: object) -> None:
            detach_calls.append(getattr(vm, "name", "?"))

        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            raise RuntimeError("simulated post-attach failure")

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _AzureRaisesAfterAttach(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack:
        with pytest.raises(RuntimeError, match="simulated post-attach failure"):
            vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]
        # ExitStack still open; detach fires on stack exit, not before.
        assert detach_calls == []

    # After stack exit, detach fired despite the RuntimeError. This is
    # the load-bearing invariant.
    assert detach_calls == ["vm1"]
    db.close()


def test_provisioner_shell_target_retries_reachability_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After attach_public_ip on Azure, the new public IP can take a few
    seconds to propagate through the SDN. The function probes the target
    with target.run('echo ok', ...) and retries on SSHError, so the
    operator's interactive ssh sees a reachable target rather than
    Connection refused on the first attempt. Matches the rekey_vm pattern."""
    import contextlib

    from agentworks.ssh import SSHError
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)

    # Stub time.sleep so the retry loop doesn't add real wall-time.
    # The function imports `time` inside the function body (not at module
    # level), so patch the module's sleep directly.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a: None)
    # Counter for run() calls so we can simulate "fails first 3 attempts,
    # succeeds on the 4th."
    run_call_count = {"n": 0}

    def flaky_run(*_a: object, **_k: object) -> object:
        run_call_count["n"] += 1
        if run_call_count["n"] < 4:
            raise SSHError("simulated propagation delay")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="", ok=True)

    class _FlakyProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            return SimpleNamespace(
                ssh=SimpleNamespace(host="203.0.113.42"),
                lima=None, wsl2=None,
                run=flaky_run,
            )

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _FlakyProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack:
        target = vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]

    # The probe retried until the 4th attempt succeeded; the function
    # returned the now-reachable target rather than raising.
    assert run_call_count["n"] == 4
    assert target.ssh.host == "203.0.113.42"
    db.close()


def test_provisioner_shell_target_raises_defensively_on_empty_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive guard: if a provisioner returns an SSHTarget with host=""
    (e.g. Azure's attach_public_ip silently failed, or a future provisioner
    has a bug), surface a clear StateError rather than letting interactive()
    hang trying to ssh to an empty hostname.

    Uses a non-Azure stub provisioner so the attach-public-IP path doesn't
    fire; we want to test the empty-host guard in isolation."""
    import contextlib

    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)

    class _BrokenProvisioner:
        def admin_exec_target(self, vm: object, *, config: object | None = None) -> object:
            return SimpleNamespace(
                ssh=SimpleNamespace(host=""), lima=None, wsl2=None,
            )

    monkeypatch.setattr(
        vm_manager, "get_provisioner_for_vm",
        lambda *a, **k: _BrokenProvisioner(),
    )

    vm = vm_manager._require_vm(db, "vm1")
    with contextlib.ExitStack() as stack, pytest.raises(StateError) as exc_info:
        vm_manager._provisioner_shell_target(db, _make_config(), vm, stack)  # type: ignore[arg-type]

    err = exc_info.value
    assert err.entity_kind == "vm"
    assert "no host" in str(err) or "not be reachable" in str(err)
    db.close()


# -- Failed-init handling -----------------------------------------------------


def test_shell_vm_warns_but_continues_on_failed_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Shelling into a failed-init VM is the most common reason to open a
    shell on one (investigate, apply a manual fix, re-run reinit). The
    operation must warn rather than raise. The warning text must mention
    'vm reinit' so the operator knows the recovery flow."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, init_status="failed")
    interactive_log: list[bool] = []
    _patch_common(monkeypatch, vm_manager, interactive_log=interactive_log)
    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target", lambda *a, **k: _stub_target(),
    )

    with pytest.raises(SystemExit) as exc_info:
        vm_manager.shell_vm(db, _make_config(), "vm1")  # type: ignore[arg-type]
    assert exc_info.value.code == 0
    assert interactive_log == [True], "shell must still open on a failed-init VM"

    # The warning should land on stderr (via output.warn) with the reinit hint.
    err = capsys.readouterr().err
    assert "failed initialization" in err
    assert "vm reinit" in err
    db.close()


def test_shell_vm_still_raises_on_failed_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed provisioning is a different beast: the VM may not even be
    reachable, and the project's stance is 'delete and recreate.' That
    block stays hard, even for vm shell. Confirms the warn-on-init
    softening doesn't accidentally also soften provisioning failures."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path, provisioning_status="failed")

    with pytest.raises(StateError) as exc_info:
        vm_manager.shell_vm(db, _make_config(), "vm1")  # type: ignore[arg-type]

    err = exc_info.value
    assert "failed provisioning" in str(err)
    assert "vm delete" in (err.hint or "")
    db.close()


def test_exec_vm_warns_but_continues_on_failed_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
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
    # ends in target.call_streaming(remote_cmd, env=env), so the stub
    # target has to expose that method too.
    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    monkeypatch.setattr(vm_manager, "keep_vm_active", lambda *a, **k: _NullCM())

    def exec_target_stub(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            ssh=SimpleNamespace(host="ssh-host"),
            lima=None,
            wsl2=None,
            call_streaming=lambda *_aa, **_kk: 0,
        )

    monkeypatch.setattr("agentworks.ssh.admin_exec_target", exec_target_stub)

    exit_code = vm_manager.exec_vm(db, _make_config(), "vm1", ["echo", "hi"])  # type: ignore[arg-type]
    assert exit_code == 0  # the underlying exec returned 0

    err = capsys.readouterr().err
    assert "failed initialization" in err
    assert "vm reinit" in err
    db.close()
