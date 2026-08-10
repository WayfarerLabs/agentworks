"""Remote Lima create staging, cleanup, and rollback failure contracts."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform import lima as lima_mod
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.errors import ProvisioningError, StateError
from agentworks.secrets.policy import InteractionPolicy
from agentworks.ssh import SSHError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput


_REMOTE_TEMPLATE_DIR = "/tmp/agentworks-lima-template.A1b2C3d4E5"
_PROVIDER_YAML = "arch: default\nmounts: []\n"


def _remote_ssh_success(command: str) -> SimpleNamespace:
    stdout = f"{_REMOTE_TEMPLATE_DIR}\n" if "mktemp -d" in command else ""
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="", ok=True)


def _request(*, tailscale_auth_key: str | None = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key=tailscale_auth_key,
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    errors: dict[str, BaseException] | None = None,
) -> list[str]:
    """Mock the backend seams and return the issued Lima commands."""
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        for needle, exc in (errors or {}).items():
            if needle in cmd:
                raise exc
        if REBOOT_SENTINEL_PATH in cmd:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        if "tailscale ip" in cmd:
            return "100.64.0.1"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)
    return ran


def _deletes(ran: list[str]) -> list[str]:
    return [cmd for cmd in ran if cmd.startswith("limactl delete")]


def _forbid_persistent_tempfiles(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("persistent local template file was attempted")

    monkeypatch.setattr("tempfile.NamedTemporaryFile", _forbidden)


class _FakeHostTransport:
    """Stands in for the vm_host SSHTransport (run_detached's target and
    the rollback's kill target). Records every call into the shared
    event log; every ``run`` reports rc 1 (so run_detached sees no
    prior pid/status files and starts fresh; its launch calls are all
    ``check=False``)."""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = events

    def write_file(self, path: str, content: str) -> None:
        self._events.append(("host-write", path))

    def run(self, cmd: str, **_kw: object) -> SimpleNamespace:
        self._events.append(("host", cmd))
        return SimpleNamespace(returncode=1, stdout="", stderr="", ok=False)


def _wire_remote_host(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, str]]) -> None:
    """Point the vm_host transport seam at the recording fake (the
    rollback's kill must never open a real SSH connection in tests)."""
    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FakeHostTransport(events),
    )


class _SilentProvisionLogger:
    path = "/dev/null"

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def log_error(self, message: str) -> None:
        del message

    def close(self) -> None:
        pass


def _wire_remote_operation_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import remote_exec

    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _SilentProvisionLogger)
    monkeypatch.setattr(
        remote_exec,
        "run_detached",
        lambda *args, **kwargs: SimpleNamespace(exit_code=0, output=""),
    )


def test_remote_logger_close_interrupt_preserves_active_failure_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec

    events: list[str] = []
    primary = SSHError("run failed")
    close_interrupt = KeyboardInterrupt("close interrupted")

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        if "mktemp -d" in command:
            events.append("allocate")
        elif "cat >" in command:
            events.append("stage")
        else:
            events.append("cleanup")
        return _remote_ssh_success(command)

    class _InterruptingLogger:
        display_path = "/tmp/provision.log"

        def __init__(self, vm_name: str, command_stem: str) -> None:
            assert (vm_name, command_stem) == ("myvm", "vm-provision")
            events.append("logger")

        def close(self) -> None:
            events.append("close")
            raise close_interrupt

    def _run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("run")
        raise primary

    _wire_remote_host(monkeypatch, events=[])  # type: ignore[arg-type]
    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _InterruptingLogger)
    monkeypatch.setattr(remote_exec, "run_detached", _run)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "team-myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert caught.value is primary
    assert events == ["allocate", "stage", "logger", "run", "close", "cleanup"]


def test_remote_standalone_logger_close_interrupt_propagates_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec

    events: list[str] = []
    close_interrupt = KeyboardInterrupt("close interrupted")

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        if "mktemp -d" in command:
            events.append("allocate")
        elif "cat >" in command:
            events.append("stage")
        else:
            events.append("cleanup")
        return _remote_ssh_success(command)

    class _InterruptingLogger:
        display_path = "/tmp/provision.log"

        def __init__(self, vm_name: str, command_stem: str) -> None:
            del vm_name, command_stem
            events.append("logger")

        def close(self) -> None:
            events.append("close")
            raise close_interrupt

    def _run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        events.append("run")
        return SimpleNamespace(exit_code=0, output="")

    _wire_remote_host(monkeypatch, events=[])
    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _InterruptingLogger)
    monkeypatch.setattr(remote_exec, "run_detached", _run)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "team-myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert caught.value is close_interrupt
    assert events == ["allocate", "stage", "logger", "run", "close", "cleanup"]


def test_remote_template_cleanup_interrupt_preserves_active_failure_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec

    events: list[str] = []
    primary = SSHError("run failed")
    cleanup_interrupt = KeyboardInterrupt("cleanup interrupted")

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        if "mktemp -d" in command:
            events.append("allocate")
        elif "cat >" in command:
            events.append("stage")
        else:
            events.append("cleanup")
            raise cleanup_interrupt
        return _remote_ssh_success(command)

    class _Logger:
        display_path = "/tmp/provision.log"

        def __init__(self, vm_name: str, command_stem: str) -> None:
            del vm_name, command_stem
            events.append("logger")

        def close(self) -> None:
            events.append("close")

    def _run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("run")
        raise primary

    _wire_remote_host(monkeypatch, events=[])
    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _Logger)
    monkeypatch.setattr(remote_exec, "run_detached", _run)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "team-myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert caught.value is primary
    assert events == ["allocate", "stage", "logger", "run", "close", "cleanup"]


def test_remote_standalone_template_cleanup_interrupt_remains_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec

    events: list[str] = []
    cleanup_interrupt = KeyboardInterrupt("cleanup interrupted")

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        if "mktemp -d" in command:
            events.append("allocate")
        elif "cat >" in command:
            events.append("stage")
        else:
            events.append("cleanup")
            raise cleanup_interrupt
        return _remote_ssh_success(command)

    class _Logger:
        display_path = "/tmp/provision.log"

        def __init__(self, vm_name: str, command_stem: str) -> None:
            del vm_name, command_stem
            events.append("logger")

        def close(self) -> None:
            events.append("close")

    def _run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        events.append("run")
        return SimpleNamespace(exit_code=0, output="")

    _wire_remote_host(monkeypatch, events=[])
    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _Logger)
    monkeypatch.setattr(remote_exec, "run_detached", _run)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "team-myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert caught.value is cleanup_interrupt
    assert events == ["allocate", "stage", "logger", "run", "close", "cleanup"]


def test_remote_provision_log_is_removed_by_normal_vm_delete(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec, ssh

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(ssh, "LOG_DIR", log_dir)
    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr(lima_mod, "ssh_run", lambda target, command, **kwargs: _remote_ssh_success(command))
    monkeypatch.setattr(
        remote_exec,
        "run_detached",
        lambda *args, **kwargs: SimpleNamespace(exit_code=0, output=""),
    )

    platform = LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})
    platform._create_remote(
        "team-myvm",
        _PROVIDER_YAML,
        log_vm_name="myvm",
    )
    (created_log,) = list(log_dir.glob("myvm-*-vm-provision.log"))
    assert list(log_dir.glob("team-myvm-*-vm-provision.log")) == []

    db.insert_vm("myvm", site="lima", hostname="team-myvm")
    db.update_vm_platform_metadata("myvm", {"instance_name": "team-myvm"})
    vm_node = SimpleNamespace(site=SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        "agentworks.vms.manager.power._live_vm_boundary",
        lambda *args, **kwargs: (vm_node, RunContext()),
    )
    monkeypatch.setattr(LimaPlatform, "_run_lima", lambda self, command, **kwargs: "")
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *args, **kwargs: None)

    vm_manager.delete_vm(
        db,
        SimpleNamespace(),  # type: ignore[arg-type]
        "myvm",
        yes=True,
        interaction=InteractionPolicy.REFUSE,
    )

    assert not created_log.exists()
    assert db.get_vm("myvm") is None


def test_remote_abandon_warning_names_the_vm_host(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """On a remote-Lima site the manual removal runs on the vm_host, so
    the abandon warning says where."""
    interrupt = KeyboardInterrupt("first")
    _wire(monkeypatch, errors={"limactl delete": KeyboardInterrupt("second")})
    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, log_vm_name: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "on 'user@host'" in abandoned


def test_remote_interrupt_kills_the_detached_limactl_before_deleting(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The real remote path (#340 review): _create_remote drives limactl
    via run_detached, which nohups the remote command precisely so it
    SURVIVES this process; a Ctrl-C during the poll sleep stops nothing
    on the vm_host. The rollback must therefore kill the detached
    process (via its PID file, run_detached's own mechanism) BEFORE
    running `limactl delete`, or the delete races a create/start still
    mutating the same instance. Exercises the real _create_remote /
    run_detached / _poll_until_done code, faking only the transport,
    logger, and sleep seams as the remote_exec tests do."""
    import time as real_time

    from agentworks import remote_exec

    events: list[tuple[str, str]] = []
    _wire_remote_host(monkeypatch, events)

    # Seams around _create_remote's edges: stdin staging, verified remote
    # removal, and the SSH log file.
    monkeypatch.setattr(
        lima_mod,
        "ssh_run",
        lambda target, cmd, **kw: events.append(("ssh", cmd)) or _remote_ssh_success(cmd),
    )

    class _StubLogger:
        path = "/dev/null"

        def __init__(self, *a: object, **kw: object) -> None:
            del a, kw

        def log_error(self, msg: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _StubLogger)

    # The interrupt lands in _poll_until_done's sleep (run_detached's
    # 0.5s launch pause passes; the poll-interval sleep raises).
    interrupt = KeyboardInterrupt("first")

    def _sleep(secs: float) -> None:
        if secs >= 1:
            raise interrupt

    monkeypatch.setattr(remote_exec, "time", SimpleNamespace(sleep=_sleep, monotonic=real_time.monotonic))

    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)

    def _fake_run_lima(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        events.append(("lima", cmd))
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run_lima)

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    # The kill goes through run_detached's PID-file mechanism, and it
    # PRECEDES the delete.
    kill_cmd = "kill $(cat /tmp/agentworks-lima-myvm.pid)"
    (kill_index,) = [i for i, (kind, cmd) in enumerate(events) if kind == "host" and kill_cmd in cmd]
    artifact_cleanup = (
        "rm -f /tmp/agentworks-lima-myvm.out /tmp/agentworks-lima-myvm.sh "
        "/tmp/agentworks-lima-myvm.pid /tmp/agentworks-lima-myvm.status"
    )
    (artifact_cleanup_index,) = [
        i for i, (kind, cmd) in enumerate(events) if kind == "host" and cmd == artifact_cleanup
    ]
    (delete_index,) = [
        i for i, (kind, cmd) in enumerate(events) if kind == "lima" and cmd == "limactl delete --force myvm"
    ]
    assert kill_index < artifact_cleanup_index < delete_index
    # The interrupt no longer skips verified remote template cleanup.
    assert any(kind == "ssh" and cmd.startswith(f"rm -rf -- {_REMOTE_TEMPLATE_DIR}") for kind, cmd in events)
    assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)


def test_remote_stdin_write_failure_removes_partial_template_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed provider-YAML stream may have created a partial remote file.

    Verified cleanup succeeds before the original staging error is restored.
    """
    write_failure = SSHError("stdin write failed")
    calls: list[tuple[str, dict[str, object]]] = []

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target
        calls.append((command, kwargs))
        if "cat >" in command:
            raise write_failure
        return _remote_ssh_success(command)

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert caught.value is write_failure
    assert "mktemp -d" in calls[0][0]
    assert calls[1][1]["input_text"] == _PROVIDER_YAML
    assert calls[1][0] == f"umask 077 && cat > {_REMOTE_TEMPLATE_DIR}/template.yaml"
    assert calls[2][0].startswith(f"rm -rf -- {_REMOTE_TEMPLATE_DIR}")


def test_remote_template_staging_is_mode_0600_stdin_and_verified_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    _wire_remote_operation_success(monkeypatch)
    _forbid_persistent_tempfiles(monkeypatch)

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target
        calls.append((command, kwargs))
        return _remote_ssh_success(command)

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
        "myvm",
        _PROVIDER_YAML,
        log_vm_name="myvm",
    )

    assert len(calls) == 3
    allocate_command, allocate_kwargs = calls[0]
    assert allocate_command == "umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX"
    assert allocate_kwargs == {}
    stage_command, stage_kwargs = calls[1]
    assert stage_command == f"umask 077 && cat > {_REMOTE_TEMPLATE_DIR}/template.yaml"
    assert stage_kwargs == {"input_text": _PROVIDER_YAML}
    cleanup_command, cleanup_kwargs = calls[2]
    assert cleanup_command == (f"rm -rf -- {_REMOTE_TEMPLATE_DIR} && test ! -e {_REMOTE_TEMPLATE_DIR}")
    assert cleanup_kwargs == {}


def test_remote_template_uses_private_random_directory_not_predictable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precreated legacy path is untouched by random private staging."""
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    predictable_path = remote_root / "agentworks-myvm.yaml"
    predictable_path.write_text("legacy provider data\n")
    staged_snapshots: list[tuple[int, int, str]] = []
    _wire_remote_operation_success(monkeypatch)
    monkeypatch.setattr(lima_mod, "_REMOTE_TEMPLATE_ROOT", str(remote_root))

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target
        input_text = kwargs.get("input_text")
        assert input_text is None or isinstance(input_text, str)
        if command.startswith("rm -rf"):
            (private_dir,) = remote_root.glob("agentworks-lima-template.*")
            template = private_dir / "template.yaml"
            staged_snapshots.append(
                (
                    stat.S_IMODE(private_dir.stat().st_mode),
                    stat.S_IMODE(template.stat().st_mode),
                    template.read_text(),
                )
            )
        completed = subprocess.run(
            ["sh", "-c", command],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            raise SSHError(f"semantic remote shell failed with {completed.returncode}")
        return SimpleNamespace(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            ok=True,
        )

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
        "myvm",
        _PROVIDER_YAML,
        log_vm_name="myvm",
    )

    assert staged_snapshots == [(0o700, 0o600, _PROVIDER_YAML)]
    assert predictable_path.read_text() == "legacy provider data\n"
    assert list(remote_root.glob("agentworks-lima-template.*")) == []


def test_remote_template_rejects_untrusted_allocation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        calls.append(command)
        return SimpleNamespace(stdout="/tmp/agentworks-lima-template.not-safe\n/tmp/other\n")

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(ProvisioningError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            _PROVIDER_YAML,
            log_vm_name="myvm",
        )

    assert calls == ["umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX"]
    assert "/tmp/other" not in repr(caught.value)


def test_remote_interrupt_preserves_original_when_artifact_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Detached artifact cleanup is best-effort on the interrupt unwind."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch)
    events: list[tuple[str, str]] = []

    class _FailingCleanupHost(_FakeHostTransport):
        def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
            if cmd.startswith("rm -f") and ".sh" in cmd:
                self._events.append(("host", cmd))
                raise SSHError("artifact cleanup failed")
            return super().run(cmd, **kwargs)

    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FailingCleanupHost(events),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, log_vm_name: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(_request(), RunContext())

    assert caught.value is interrupt
    assert any(kind == "host" and cmd.startswith("rm -f") and ".sh" in cmd for kind, cmd in events)
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert not any("artifact cleanup failed" in warning for warning in captured_output.warnings)


def test_remote_exception_kills_cleans_artifacts_then_deletes_without_masking(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """An ordinary provisioning failure gets the full remote rollback."""
    original = SSHError("ordinary provisioning failure")
    events: list[tuple[str, str]] = []
    _wire(monkeypatch)

    class _FailingCleanupHost(_FakeHostTransport):
        def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
            if cmd.startswith("rm -f") and ".sh" in cmd:
                self._events.append(("host", cmd))
                raise SSHError("artifact cleanup failed")
            return super().run(cmd, **kwargs)

    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FailingCleanupHost(events),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, log_vm_name: (_ for _ in ()).throw(original),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, cmd, **kwargs: events.append(("lima", cmd)) or "",
    )

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(_request(), RunContext())

    assert caught.value is original
    kill_cmd = "kill $(cat /tmp/agentworks-lima-myvm.pid)"
    (kill_index,) = [i for i, (kind, cmd) in enumerate(events) if kind == "host" and kill_cmd in cmd]
    (cleanup_index,) = [
        i
        for i, (kind, cmd) in enumerate(events)
        if kind == "host"
        and cmd.startswith("rm -f")
        and all(f".{suffix}" in cmd for suffix in ("out", "sh", "pid", "status"))
    ]
    (delete_index,) = [
        i for i, (kind, cmd) in enumerate(events) if kind == "lima" and cmd == "limactl delete --force myvm"
    ]
    assert kill_index < cleanup_index < delete_index
    surfaced = "\n".join([*captured_output.detail, *captured_output.warnings])
    assert "artifact cleanup failed" not in surfaced
    assert caught.value is original


def test_cleanup_failure_warns_and_does_not_mask_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback is best-effort: a broken teardown warns with the
    manual removal command and the ORIGINAL failure still propagates."""
    ran = _wire(monkeypatch, errors={"limactl delete": SSHError("cleanup broke")})
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(SSHError("original failure")),
    )

    with pytest.raises(SSHError, match="original failure"):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert len(_deletes(ran)) == 1
    warned = "\n".join(captured_output.warnings)
    assert "could not clean up the partial Lima instance 'myvm'" in warned
    assert "'limactl delete --force myvm'" in warned


def test_pre_mutation_failure_makes_no_cleanup_calls(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A failure before anything is created (the name-collision
    pre-flight) must not fire any teardown: there is nothing of ours to
    delete, and the colliding instance is NOT ours to touch."""
    ran = _wire(monkeypatch)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: True)

    with pytest.raises(StateError, match="already exists"):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert _deletes(ran) == []
    # The cleanup announcement goes to the detail stream; its absence
    # there pins that neither rollback arm even started.
    assert not any("Cleaning up" in d for d in captured_output.detail)


def test_delete_op_issues_the_shared_teardown_command(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """delete() is unchanged by the dedup: exactly the forced delete the
    rollback also uses."""
    ran = _wire(monkeypatch)
    vm = SimpleNamespace(name="myvm", platform_metadata={"instance_name": "myvm"})

    LimaPlatform("lima", {"placement": {"mode": "local"}}).delete(vm, RunContext())  # type: ignore[arg-type]

    assert ran == ["limactl delete --force myvm"]
