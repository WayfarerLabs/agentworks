"""Lima ``create`` rollback on failure and interrupt (#340).

``create`` runs ``limactl create`` + ``limactl start`` (a minutes-long
window where a Ctrl-C is likeliest) and then post-start steps (the
restart-sentinel probe, the Tailscale IP read). The caller
(``create_vm``) deletes only the DB row on failure or interrupt, so an
instance left behind would be orphaned with nothing left to target it.
``create`` therefore tears down the instance it made (``limactl delete
--force``, the delete op's exact command via the shared
``_delete_instance``) on plain failure AND on interrupt, re-raising
the original; a SECOND interrupt during the cleanup abandons it loudly,
naming the removal command. The azure precedent is
test_azure_create_interrupt.py (#338).

Backend seams (``_run_lima``, ``_create_local`` / ``_create_remote``)
are mocked as in test_lima_create_flow.py; no test runs limactl.
"""

from __future__ import annotations

import os
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
from agentworks.errors import ProvisioningError, SensitiveDataCleanupError, StateError
from agentworks.ssh import SSHError, SSHLogger

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


_REMOTE_TEMPLATE_DIR = "/tmp/agentworks-lima-template.A1b2C3d4E5"


def _remote_ssh_success(command: str) -> SimpleNamespace:
    stdout = f"{_REMOTE_TEMPLATE_DIR}\n" if "mktemp -d" in command else ""
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="", ok=True)


def _request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key="tskey-test",
        # The vm-template layer's resolved defaults, which is the only
        # shape a platform ever sees (the hardware fields are required).
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
    """Mock the backend seams; return the ``_run_lima`` commands issued.

    ``errors`` maps a command substring to the exception ``_run_lima``
    raises when it sees it (the command is still recorded first, so
    tests can pin exactly-once attempts).
    """
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


def test_local_create_streams_template_without_persistent_file(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "stdin-swordfish"
    calls: list[tuple[str, dict[str, object]]] = []
    _forbid_persistent_tempfiles(monkeypatch)

    def _fake_run(self: LimaPlatform, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    LimaPlatform("lima", {})._create_local("myvm", f"embedded: {secret}")

    assert calls == [
        ("limactl create --name myvm --tty=false -", {"input_text": f"embedded: {secret}"}),
        ("limactl start myvm", {}),
    ]
    assert secret not in calls[0][0]


@pytest.mark.parametrize(
    "failure",
    [
        BrokenPipeError("stdin write failed"),
        OSError("stdin flush failed"),
        OSError("stdin pre-close failed"),
    ],
    ids=("write", "flush", "pre-close"),
)
def test_local_stdin_io_failure_refuses_start_without_secret_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    secret = "stdin-swordfish"
    commands: list[list[str]] = []

    def _fail_stdin(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        assert kwargs["input"] == f"embedded: {secret}"
        raise failure

    monkeypatch.setattr("subprocess.run", _fail_stdin)

    with pytest.raises(OSError) as caught:
        LimaPlatform("lima", {})._create_local("myvm", f"embedded: {secret}")

    assert caught.value is failure
    assert commands == [["limactl", "create", "--name", "myvm", "--tty=false", "-"]]
    assert secret not in repr(caught.value)
    assert secret not in repr(commands)


def test_local_lima_failure_omits_secret_bearing_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "stdin-swordfish"

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"invalid template line: embedded: {secret}",
        ),
    )

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {})._create_local("myvm", f"embedded: {secret}")

    assert str(caught.value) == ("limactl stdin command failed (exit 1): limactl create --name myvm --tty=false -")
    assert secret not in repr(caught.value)


def test_failure_mid_create_cleans_up_and_reraises(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A plain backend failure (here: limactl start) tears down the
    instance and re-raises unwrapped, per lima's error convention; the
    interrupt messaging never appears."""
    ran = _wire(monkeypatch)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(SSHError("provision exploded")),
    )

    with pytest.raises(SSHError, match="provision exploded"):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert not any("Interrupted" in w for w in captured_output.warnings)
    assert not any("Cleanup abandoned" in w for w in captured_output.warnings)


def test_interrupt_during_start_cleans_up_and_reraises_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The likeliest scenario: Ctrl-C during the minutes-long limactl
    start. The rollback deletes the instance and the ORIGINAL interrupt
    propagates for the caller's row unwind (identity pin)."""
    ran = _wire(monkeypatch)
    interrupt = KeyboardInterrupt("first")
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)


def test_interrupt_during_post_start_steps_cleans_up_too(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback spans the FULL create, not just limactl create/start:
    a Ctrl-C in the post-start Tailscale IP read still tears down."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"tailscale ip": interrupt})

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]


def test_second_interrupt_abandons_cleanup_loudly(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A second Ctrl-C during the cleanup abandons it instead of
    wedging: the delete is attempted exactly once, the warning names the
    exact removal command, and the ORIGINAL interrupt still propagates."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"limactl delete": KeyboardInterrupt("second")})
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert len(_deletes(ran)) == 1
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "'limactl delete --force myvm'" in abandoned


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
        lambda self, name, yaml, *, redactions: (_ for _ in ()).throw(interrupt),
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

    captured_redactions: list[tuple[str, ...]] = []

    class _StubLogger:
        path = "/dev/null"

        def __init__(self, *a: object, **kw: object) -> None:
            captured_redactions.append(kw.get("redactions", ()))  # type: ignore[arg-type]

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
    assert captured_redactions == [("tskey-test",)]
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed stdin stream may have created a partial remote YAML.

    Verified cleanup succeeds before the original staging error is restored.
    """
    secret = "tskey-stdin-only"
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
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert caught.value is write_failure
    assert "mktemp -d" in calls[0][0]
    assert calls[1][1]["input_text"] == f"embedded: {secret}"
    assert calls[1][0] == f"umask 077 && cat > {_REMOTE_TEMPLATE_DIR}/template.yaml"
    assert calls[2][0].startswith(f"rm -rf -- {_REMOTE_TEMPLATE_DIR}")
    assert secret not in repr([command for command, _kwargs in calls])
    assert secret not in caplog.text
    assert secret not in repr(caught.value)


def test_remote_template_staging_is_mode_0600_stdin_and_verified_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "remote-stdin-swordfish"
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
        f"embedded: {secret}",
        redactions=(secret,),
    )

    assert len(calls) == 3
    allocate_command, allocate_kwargs = calls[0]
    assert allocate_command == "umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX"
    assert allocate_kwargs == {}
    stage_command, stage_kwargs = calls[1]
    assert stage_command == f"umask 077 && cat > {_REMOTE_TEMPLATE_DIR}/template.yaml"
    assert stage_kwargs == {"input_text": f"embedded: {secret}"}
    cleanup_command, cleanup_kwargs = calls[2]
    assert cleanup_command == (f"rm -rf -- {_REMOTE_TEMPLATE_DIR} && test ! -e {_REMOTE_TEMPLATE_DIR}")
    assert cleanup_kwargs == {}
    assert secret not in stage_command
    assert secret not in cleanup_command


def test_remote_template_uses_private_random_directory_not_predictable_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precreated legacy-path FIFO neither receives the secret nor blocks."""
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    predictable_fifo = remote_root / "agentworks-myvm.yaml"
    os.mkfifo(predictable_fifo)
    secret = "fifo-adversary-swordfish"
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
        f"embedded: {secret}",
        redactions=(secret,),
    )

    assert staged_snapshots == [(0o700, 0o600, f"embedded: {secret}")]
    assert stat.S_ISFIFO(predictable_fifo.stat().st_mode)
    fifo_fd = os.open(predictable_fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert os.read(fifo_fd, 4096) == b""
    finally:
        os.close(fifo_fd)
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
            "embedded: secret",
            redactions=("secret",),
        )

    assert calls == ["umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX"]
    assert "/tmp/other" not in repr(caught.value)


@pytest.mark.parametrize(
    "cleanup_failure",
    [SSHError("unlink refused swordfish"), KeyboardInterrupt("cleanup interrupted swordfish")],
    ids=("unlink", "keyboard-interrupt"),
)
def test_remote_template_cleanup_retries_then_succeeds_without_surface(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
    captured_output: CapturedOutput,
) -> None:
    attempts = 0

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        del target, command, kwargs
        attempts += 1
        if attempts == 1:
            raise cleanup_failure
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._remove_remote_template_dir(
        SimpleNamespace(),  # type: ignore[arg-type]
        _REMOTE_TEMPLATE_DIR,
    )

    assert attempts == 2
    assert "swordfish" not in "\n".join([*captured_output.detail, *captured_output.warnings])


@pytest.mark.parametrize(
    "cleanup_failure",
    [SSHError("unlink refused swordfish"), KeyboardInterrupt("cleanup interrupted swordfish")],
    ids=("unlink", "keyboard-interrupt"),
)
def test_repeated_remote_template_cleanup_failure_reports_typed_residue(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
) -> None:
    attempts = 0

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        del target, command, kwargs
        attempts += 1
        raise cleanup_failure

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(SensitiveDataCleanupError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._remove_remote_template_dir(
            SimpleNamespace(),  # type: ignore[arg-type]
            _REMOTE_TEMPLATE_DIR,
        )

    assert attempts == 3
    assert str(caught.value) == "removal of sensitive Lima provisioning input could not be confirmed"
    assert caught.value.hint is not None
    assert f"recursively remove directory '{_REMOTE_TEMPLATE_DIR}'" in caught.value.hint
    assert "swordfish" not in repr(caught.value)
    assert "swordfish" not in caught.value.hint


def test_remote_template_cleanup_propagates_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programming_error = RuntimeError("bug in SSH adapter")
    attempts = 0

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        del target, command, kwargs
        attempts += 1
        raise programming_error

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(RuntimeError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._remove_remote_template_dir(
            SimpleNamespace(),  # type: ignore[arg-type]
            _REMOTE_TEMPLATE_DIR,
        )

    assert caught.value is programming_error
    assert attempts == 1


def test_successful_remote_operation_with_cleanup_residue_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "success-cleanup-swordfish"
    calls: list[str] = []
    _wire_remote_operation_success(monkeypatch)

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        calls.append(command)
        if "mktemp -d" in command:
            return _remote_ssh_success(command)
        if "cat >" in command:
            return SimpleNamespace(stdout="")
        raise SSHError(f"unlink exposed {secret}")

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(SensitiveDataCleanupError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert len(calls) == 5
    assert str(caught.value) == "removal of sensitive Lima provisioning input could not be confirmed"
    assert caught.value.hint is not None
    assert secret not in repr(caught.value)
    assert secret not in caught.value.hint
    assert caught.value.__context__ is None


def test_staging_and_repeated_cleanup_failure_reports_safe_combined_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "combined-swordfish"
    calls: list[str] = []

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        calls.append(command)
        if "mktemp -d" in command:
            return _remote_ssh_success(command)
        if "cat >" in command:
            raise SSHError(f"stdin write exposed {secret}")
        raise SSHError(f"unlink exposed {secret}")

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(SensitiveDataCleanupError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert len(calls) == 5
    assert all(secret not in command for command in calls)
    assert str(caught.value) == (
        "Lima provisioning failed and removal of sensitive Lima provisioning input could not be confirmed"
    )
    assert caught.value.hint is not None
    assert secret not in repr(caught.value)
    assert secret not in caught.value.hint
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_staging_interrupt_and_cleanup_interrupt_preserve_original_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "interrupt-swordfish"
    original = KeyboardInterrupt("staging interrupted")
    attempts = 0

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        del target, kwargs
        attempts += 1
        if "mktemp -d" in command:
            return _remote_ssh_success(command)
        if "cat >" in command:
            raise original
        if attempts == 3:
            raise KeyboardInterrupt(f"cleanup interrupted {secret}")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert caught.value is original
    assert attempts == 4
    assert secret not in repr(caught.value)


def test_logger_close_interrupt_preserves_active_interrupt_and_cleans_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import remote_exec

    original = KeyboardInterrupt("provisioning interrupted")
    close_interrupt = KeyboardInterrupt("logger close interrupted")
    calls: list[str] = []
    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target, kwargs
        calls.append(command)
        return _remote_ssh_success(command)

    def _raise_original(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise original

    def _interrupt_close(self: SSHLogger) -> None:
        del self
        raise close_interrupt

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr(remote_exec, "run_detached", _raise_original)
    monkeypatch.setattr(SSHLogger, "close", _interrupt_close)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            "embedded: secret",
            redactions=("secret",),
        )

    assert caught.value is original
    assert calls[-1] == f"rm -rf -- {_REMOTE_TEMPLATE_DIR} && test ! -e {_REMOTE_TEMPLATE_DIR}"


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
                raise SSHError("artifact cleanup exposed swordfish")
            return super().run(cmd, **kwargs)

    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FailingCleanupHost(events),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, redactions: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(_request(), RunContext())

    assert caught.value is interrupt
    assert any(kind == "host" and cmd.startswith("rm -f") and ".sh" in cmd for kind, cmd in events)
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert "swordfish" not in "\n".join(captured_output.warnings)


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
                raise SSHError("artifact cleanup exposed swordfish")
            return super().run(cmd, **kwargs)

    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FailingCleanupHost(events),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, redactions: (_ for _ in ()).throw(original),
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
    assert "swordfish" not in surfaced
    assert "swordfish" not in repr(caught.value)


def test_remote_provision_failure_redacts_log_and_raised_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap output can echo the embedded key, so neither sink may."""
    from agentworks import remote_exec

    secret = "tskey-test"
    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    monkeypatch.setattr(lima_mod, "ssh_run", lambda target, command, **kwargs: _remote_ssh_success(command))
    monkeypatch.setattr(
        remote_exec,
        "run_detached",
        lambda *a, **k: SimpleNamespace(exit_code=1, output=f"bootstrap rejected {secret}"),
    )

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert secret not in str(caught.value)
    (log_path,) = tmp_path.glob("*.log")
    log_text = log_path.read_text()
    assert secret not in log_text
    assert "bootstrap rejected [REDACTED]" in log_text


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
