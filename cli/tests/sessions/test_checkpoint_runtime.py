"""A missing boot identity gets a bounded checkpoint, never an unprepared launch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, SessionMode
from agentworks.errors import StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from tests._runtime_probes import server_for
from tests.sessions.test_create_start_restart_orchestrated import _create_stubs
from tests.sessions.test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from tests.sessions.test_native_startup import _NATIVE_CAPTURE, launch
from tests.sessions.test_native_startup import native as native
from tests.sessions.test_round8_startup import bind_native_runtime
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize("operation", ["start_session", "restart_session", "stop_session"])
@pytest.mark.parametrize("null_address", [False, True])
def test_checkpoint_then_changed_boot_observation_enables_safe_ordinary_start(
    tmp_path, monkeypatch, native, null_address, operation
):
    db = _create_stubs(tmp_path, monkeypatch, [])
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(tmux, "ADMIN_SOCKET_ROOT", str(native.root))
    native.socket = str(native.root / "admin" / "s1.sock")
    Path(native.socket).parent.mkdir(exist_ok=True)
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN, socket_path=None if null_address else native.socket)
    monkeypatch.setattr(tmux, "create_session", lambda *a, **k: pytest.fail("checkpoint widened into spawn"))
    monkeypatch.setattr(
        "agentworks.harness_setup.readiness.require_setup_ready",
        lambda *a, **k: pytest.fail("checkpoint used launch readiness"),
    )
    config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
    try:
        with pytest.raises(StateError):
            getattr(manager, operation)(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        checkpoint = db.get_session("s1")
        assert checkpoint is not None and checkpoint.boot_id == tmux.read_boot_id(native.run) and checkpoint.pid is None
        with pytest.raises(StateError):
            getattr(manager, operation)(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        assert db.get_session("s1") == checkpoint
        monkeypatch.setattr(manager, "_get_boot_id", lambda *a, **k: str(uuid4()))
        monkeypatch.setattr("agentworks.harness_setup.readiness.require_setup_ready", lambda *a, **k: None)
        monkeypatch.setattr(tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", launch_options=k))
        getattr(manager, operation)(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        current = db.get_session("s1")
        assert (
            current is not None
            and current.session_uuid == checkpoint.session_uuid
            and (current.pid == PID_STOPPED) is (operation == "stop_session")
        )
        assert _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).status is (
            tmux.ProbeStatus.ABSENT if operation == "stop_session" else tmux.ProbeStatus.PRESENT
        )
    finally:
        db.close()


@pytest.mark.parametrize("obstruction", ["dedicated", "default", "unknown-dedicated", "unknown-default"])
def test_checkpoint_refuses_present_or_unknown_native_server_without_writing_identity(
    tmp_path, monkeypatch, native, obstruction
):
    db = _create_stubs(tmp_path, monkeypatch, [])
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr(tmux, "ADMIN_SOCKET_ROOT", str(native.root))
    native.socket = str(native.root / "admin" / "s1.sock")
    Path(native.socket).parent.mkdir(exist_ok=True)
    null_address = "default" in obstruction
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN, socket_path=None if null_address else native.socket)
    if obstruction == "dedicated":
        launch(native, "sleep 30")
    elif obstruction == "default":
        native.run("tmux new-session -d -s s1 'sleep 30'")
    elif obstruction == "unknown-dedicated":
        monkeypatch.setattr(
            tmux, "capture_tmux_server_fingerprint", lambda **k: tmux.FingerprintProbe(tmux.ProbeStatus.UNKNOWN)
        )
    else:
        original = native.run

        def unknown(command, **kwargs):
            if "AGW_RUNTIME_SESSION=" in command and "-S " not in command:
                return SimpleNamespace(returncode=255, stdout="", stderr="unavailable", ok=False)
            return original(command, **kwargs)

        monkeypatch.setattr(native, "run", unknown)
    before = db.get_session("s1")
    first = len(native.calls)
    try:
        with pytest.raises(StateError):
            manager.start_session(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        assert db.get_session("s1") == before
        assert not any(
            action in command
            for command, _ in native.calls[first:]
            for action in ("kill-server", "new-session", "chmod ")
        )
    finally:
        if obstruction == "default":
            native.run("tmux kill-server", check=False)
        db.close()


def test_runtime_observer_bounds_its_reads_and_reads_a_legacy_row_as_the_admin(tmp_path, monkeypatch):
    from dataclasses import replace

    from agentworks.sessions.manager import _status
    from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS

    db = _create_stubs(tmp_path, monkeypatch, [])
    vm = db.get_vm("vm1")
    assert vm is not None
    db.insert_session(
        "s1", "ws1", "claude", SessionMode.ADMIN, socket_path=f"{tmux.ADMIN_SOCKET_ROOT}/{vm.admin_username}/s1.sock"
    )
    row = db.get_session("s1")
    assert row is not None
    admin = RecordingVMAccess([], Admin())
    monkeypatch.setattr(manager, "check_session_status", _status.check_session_status)

    manager.observe_session_runtime(row, admin=admin, server=server_for(row))

    def legacy(name, *, run_command, socket_path):
        run_command("legacy", check=False)
        return tmux.ProbeStatus.PRESENT

    monkeypatch.setattr(tmux, "probe_tmux_session", legacy)
    before = len(admin.calls)
    manager.observe_session_runtime(replace(row, socket_path=None, pid=4242), admin=admin, server=None)
    requests = [call.request for call in admin.calls]
    legacy_reads = requests[before:]
    assert legacy_reads and not any(request.elevated for request in legacy_reads)
    assert all(request.deadline == GUEST_OBSERVATION_TIMEOUT_SECONDS for request in requests)
    db.close()
