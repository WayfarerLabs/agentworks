"""Exercise startup protection, native diagnostics, and log limits with tmux."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.db import SessionStatus
from agentworks.errors import AgentworksError, ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager._pids import _repair_session_pid
from agentworks.sessions.manager._status import check_session_status
from agentworks.ssh import SSHError, SSHResult
from tests.vm_access_support import Admin, RecordingVMAccess

from .test_create_start_restart_orchestrated import (
    _create_stubs,
    _patch_transports,
    _requiring_template,
    _restart_fixture,
)
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_startup_recovery import _fake_owner_realizers

_NATIVE_CREATE = tmux.create_session
_NATIVE_CAPTURE = tmux.capture_tmux_server_fingerprint
_NATIVE_FINALIZE = tmux.finalize_session_start

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
    reason="requires Linux with local tmux",
)


class NativeTarget:
    logger = None

    def __init__(self, root: Path) -> None:
        self.root = root
        self.socket = str(root / "s")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.banner = ""
        self.stderr_banner = ""
        self.environment = {
            "HOME": str(root),
            "PATH": os.defpath,
            "SHELL": "/bin/sh",
            "TERM": "xterm",
            "TMUX_TMPDIR": str(root),
        }

    def run(self, command: str, *, check: bool = True, **kwargs: Any) -> SSHResult:
        self.calls.append((command, kwargs))
        input_data = kwargs.get("input_data")
        if isinstance(input_data, bytes):
            input_data = input_data.decode()
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            env=self.environment,
            input=input_data,
        )
        if check and result.returncode:
            raise ExternalError(result.stderr or result.stdout)
        return SSHResult(result.returncode, self.banner + result.stdout, self.stderr_banner + result.stderr)

    def native(self, command: str) -> SSHResult:
        return self.run(tmux.tmux_cmd(command, self.socket), check=False)

    def wait_dead(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state, _status = tmux._probe_pane_liveness("s1", run_command=self.run, socket_path=self.socket)
            if state == "dead":
                return
            time.sleep(0.02)
        raise AssertionError("workload did not exit")


@pytest.fixture
def native(monkeypatch):
    if os.name != "posix" or not Path("/proc/self/stat").exists() or shutil.which("tmux") is None:
        pytest.skip("requires Linux guest process identity observations")
    # pytest's nested tmp_path is longer than sun_path on some hosts.
    with tempfile.TemporaryDirectory(prefix="agw-r9-runtime-native-", dir="/tmp") as directory:
        root = Path(directory)
        target = NativeTarget(root)
        config = root / "tmux.conf"
        config.write_text("set-option -g @agentworks-native-config loaded\n")
        monkeypatch.setattr(tmux, "RESTRICTED_CONFIG_PATH", str(config))
        monkeypatch.setattr(tmux, "admin_socket_path", lambda *a: target.socket)
        monkeypatch.setattr(tmux, "agent_socket_path", lambda *a: target.socket)
        monkeypatch.setattr(tmux, "ensure_admin_socket_root", lambda *a: None)
        monkeypatch.setattr(tmux, "ensure_agent_socket_root", lambda *a: None)
        monkeypatch.setattr(tmux, "ensure_agent_socket_dir", lambda *a: None)
        # Cross-user access is independently tested; no system users are created.
        monkeypatch.setattr(tmux, "_grant_server_access", lambda *a: None)
        try:
            yield target
        finally:
            target.native("kill-server")
            assert target.native("list-sessions").returncode != 0
    assert not root.exists()


def launch(native: NativeTarget, command: str, *, is_admin: bool = True, launch_options=None):
    options = launch_options or {}
    return _NATIVE_CREATE(
        "s1",
        str(native.root),
        command,
        linux_user="owned",
        run_command=native.run,
        admin_files=RecordingVMAccess([], Admin()).files(),
        admin_username="owned",
        is_admin=is_admin,
        env=options.get("env"),
        before_spawn=options.get("before_spawn"),
        session_uuid=options.get("session_uuid"),
        run_id=options.get("run_id"),
    )


def test_native_global_config_and_last_n_log_lines(native):
    gate = native.root / "emit"
    script = native.root / "work.sh"
    script.write_text(
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep .02; done\n"
        "i=1; while [ $i -le 35 ]; do printf 'line-%s\\n' $i; i=$((i+1)); done\n"
        "exec sleep 30\n"
    )
    socket, _pid = launch(native, f"/bin/sh {shlex.quote(str(script))}")
    _NATIVE_FINALIZE("s1", run_command=native.run, socket_path=socket, is_admin=True)
    assert native.native("show-option -wAqv -t =s1: remain-on-exit").stdout.strip() == "off"
    assert native.native("show-option -gqv @agentworks-native-config").stdout.strip() == "loaded"
    gate.touch()
    deadline = time.monotonic() + 5
    captured = ""
    while "line-35" not in captured and time.monotonic() < deadline:
        captured = tmux.capture_output("s1", run_command=native.run, socket_path=socket, lines=3)
        time.sleep(0.02)
    assert len(captured.splitlines()) == 3
    assert "line-35" in captured
    assert "line-1\n" not in captured


def test_native_dead_pane_captures_saved_screen_and_active_overlay(native):
    gate = native.root / "exit"
    script = native.root / "work.sh"
    script.write_text(
        "printf 'saved-marker\\n'; printf '\\033[?1049h'; printf 'active-marker\\n'\n"
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep .02; done\nexit 23\n"
    )
    socket, _pid = launch(native, f"/bin/sh {shlex.quote(str(script))}")
    gate.touch()
    native.wait_dead()
    observations = []

    def observed_run(command, **kwargs):
        result = native.run(command, **kwargs)
        if "#{pane_dead}" in command:
            observations.append(result.stdout.removeprefix("\nAGW_RUNTIME_PANE=").strip())
        return result

    with pytest.raises(StateError) as caught:
        _NATIVE_FINALIZE("s1", run_command=observed_run, socket_path=socket, is_admin=False)
    assert "saved-marker" in str(caught.value)
    assert "active-marker" in str(caught.value)
    # tmux can observe death before a wait status is available. Preserve the
    # status actually returned by the native probe without inferring one.
    assert len(observations) == 1 and observations[0] in ("1", "1 23")
    if observations[0] == "1 23":
        assert "23" in str(caught.value)
    chmod = next(index for index, (cmd, _) in enumerate(native.calls) if "chmod g+rwx" in cmd)
    finished = chmod + 1
    check = next(index for index, (cmd, _) in enumerate(native.calls[finished:], finished) if "#{pane_dead}" in cmd)
    assert chmod < check
    assert Path(socket).stat().st_mode & 0o070 == 0o070
    assert native.native("list-sessions").returncode != 0


@pytest.mark.parametrize("operation", ["start_session", "restart_session"])
def test_native_death_during_identity_capture_keeps_recoverable_fingerprint(tmp_path, monkeypatch, native, operation):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    _patch_transports(monkeypatch, native, native)
    _requiring_template(monkeypatch, "sh")
    monkeypatch.setattr(manager, "check_session_status", check_session_status)
    gate = native.root / "exit"
    script = native.root / "work.sh"
    script.write_text(f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep .02; done\nprintf late-marker; exit 29\n")

    def create(*args, **kwargs):
        return launch(native, f"/bin/sh {shlex.quote(str(script))}", is_admin=False, launch_options=kwargs)

    def capture(**kwargs):
        gate.touch()
        # This is the old 1.5-3 second observation window, while protection is held.
        time.sleep(1.6)
        native.wait_dead()
        return _NATIVE_CAPTURE(target=native, socket_path=native.socket)

    monkeypatch.setattr(tmux, "create_session", create)
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", capture)
    monkeypatch.setattr(tmux, "finalize_session_start", _NATIVE_FINALIZE)
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    try:
        with pytest.raises(StateError) as caught:
            getattr(manager, operation)(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        assert "late-marker" in str(caught.value)
        row = db.get_session("s1")
        assert row is not None and row.pid is not None and row.boot_id is not None
        assert row.tmux_server_start_ticks is not None
        # kill-server acknowledgement can precede process teardown. Keep the
        # stored-identity guard in force until that process is actually absent.
        deadline = time.monotonic() + 5
        status = check_session_status(row, target=native)
        while status is not SessionStatus.STOPPED and time.monotonic() < deadline:
            assert status is SessionStatus.BROKEN
            time.sleep(0.02)
            status = check_session_status(row, target=native)
        assert status is SessionStatus.STOPPED
        monkeypatch.setattr(
            tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
        )
        monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
        # A subsequent managed launch removes the observed stale socket and recovers.
        getattr(manager, operation)(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )
        recovered = db.get_session("s1")
        assert recovered is not None and recovered.pid is not None
        assert check_session_status(recovered, target=native) is SessionStatus.RUNNING
    finally:
        db.close()


def test_native_unverified_checkpoint_recovers_still_private_socket(tmp_path, monkeypatch, native):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    _patch_transports(monkeypatch, native, native)
    _requiring_template(monkeypatch, "sh")
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    monkeypatch.setattr(
        tmux, "capture_tmux_server_fingerprint", lambda **k: tmux.FingerprintProbe(tmux.ProbeStatus.UNKNOWN)
    )
    try:
        with pytest.raises(AgentworksError):
            manager.start_session(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        row = db.get_session("s1")
        assert row is not None and row.pid is None
        assert Path(native.socket).stat().st_mode & 0o077 == 0
        assert check_session_status(row, target=native) is SessionStatus.UNKNOWN
        assert any(options.get("sudo") is True for command, options in native.calls if "has-session" in command)
        monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
        assert _repair_session_pid(row, target=native, db=db)
        recovered = db.get_session("s1")
        assert recovered is not None and recovered.pid is not None and recovered.tmux_server_start_ticks is not None
    finally:
        db.close()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_fingerprint_and_presence_ignore_pre_frame_login_stdout(native, stream):
    if stream == "stdout":
        native.banner = "harmless login stdout without a newline"
    else:
        native.stderr_banner = "harmless login stderr without a newline"
    socket, pid = launch(native, "sleep 30")
    assert (pid is None) is (stream == "stdout")
    fingerprint = _NATIVE_CAPTURE(target=native, socket_path=socket)
    assert fingerprint.status is tmux.ProbeStatus.PRESENT
    assert fingerprint.fingerprint is not None and fingerprint.fingerprint.pid > 0
    assert tmux.probe_tmux_session("s1", run_command=native.run, socket_path=socket) is tmux.ProbeStatus.PRESENT
    _NATIVE_FINALIZE("s1", run_command=native.run, socket_path=socket, is_admin=True)


@pytest.mark.parametrize("fault", ["fingerprint", "grant", "interrupt"])
def test_native_create_failure_keeps_live_runtime_and_new_dependencies(tmp_path, monkeypatch, native, fault):
    db = _create_stubs(tmp_path, monkeypatch, [])
    _fake_owner_realizers(monkeypatch)
    _patch_transports(monkeypatch, native, native)
    _requiring_template(monkeypatch, "sh")
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    failure = KeyboardInterrupt() if fault == "interrupt" else SSHError("controlled sharing failure")

    def grant(*args, **kwargs):
        raise failure

    monkeypatch.setattr(tmux, "finalize_session_start", _NATIVE_FINALIZE)
    if fault == "fingerprint":
        monkeypatch.setattr(
            tmux, "capture_tmux_server_fingerprint", lambda **k: tmux.FingerprintProbe(tmux.ProbeStatus.UNKNOWN)
        )
    else:
        monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
        monkeypatch.setattr(tmux, "_grant_server_access", grant)
    try:
        with pytest.raises(ExternalError if fault == "fingerprint" else type(failure)) as caught:
            manager.create_session(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1), paths=SimpleNamespace(vm_workspaces="/srv")),
                name="s1",
                new_workspace=True,
                workspace_spec='{"tmuxinator":false}',
                new_agent=True,
                agent_spec='{"shell":"zsh"}',
                vm_name="vm1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        if fault != "fingerprint":
            assert caught.value is failure
        row = db.get_session("s1")
        assert row is not None and row.socket_path == native.socket
        assert db.get_agent("s1") is not None and db.get_workspace("s1") is not None
        expected = SessionStatus.UNKNOWN if fault == "fingerprint" else SessionStatus.RUNNING
        assert check_session_status(row, target=native) is expected
        if fault == "fingerprint":
            assert row.pid is None
            monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
            assert _repair_session_pid(row, target=native, db=db)
        else:
            assert row.pid is not None and row.boot_id is not None and row.tmux_server_start_ticks is not None
        assert native.native("show-option -wqv -t =s1: remain-on-exit").stdout.strip() == "on"
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["create_session", "start_session", "restart_session"])
def test_failed_native_startup_release_retains_checkpointed_runtime(tmp_path, monkeypatch, native, operation):
    if operation == "create_session":
        db = _create_stubs(tmp_path, monkeypatch, [])
        _fake_owner_realizers(monkeypatch)
        options = {
            "new_workspace": True,
            "workspace_spec": '{"tmuxinator":false}',
            "new_agent": True,
            "agent_spec": '{"shell":"zsh"}',
            "vm_name": "vm1",
        }
    else:
        db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
        options = {}
    _patch_transports(monkeypatch, native, native)
    _requiring_template(monkeypatch, "sh")
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
    monkeypatch.setattr(tmux, "finalize_session_start", _NATIVE_FINALIZE)
    real_run = native.run

    def reject_release(command, *, check=True, **kwargs):
        if "remain-on-exit off" in command:
            # Make the actual native command fail. NativeTarget.run enforces
            # check=True exactly as the production Transport does.
            command = command.replace("remain-on-exit", "agentworks-invalid-release-option")
        return real_run(command, check=check, **kwargs)

    monkeypatch.setattr(native, "run", reject_release)
    try:
        with pytest.raises(AgentworksError):
            getattr(manager, operation)(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1), paths=SimpleNamespace(vm_workspaces="/srv")),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
                **options,
            )
        row = db.get_session("s1")
        assert row is not None and row.socket_path == native.socket
        observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
        assert observed is not None
        assert (row.pid, row.boot_id, row.tmux_server_start_ticks) == (
            observed.pid,
            observed.boot_id,
            observed.start_ticks,
        )
        assert check_session_status(row, target=native) is SessionStatus.RUNNING
        assert native.native("show-option -wqv -t =s1: remain-on-exit").stdout.strip() == "on"
        if operation == "create_session":
            assert db.get_agent("s1") is not None and db.get_workspace("s1") is not None
    finally:
        db.close()
