"""Startup uncertainty keeps a recovery owner until native facts resolve it."""

from __future__ import annotations

import os
import shlex
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.capabilities.harness_integration import ShellIntegration
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _lifecycle, _pids

from .test_create_start_restart_orchestrated import BOOT_ID, _patch_transports, _requiring_template, _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import (
    _NATIVE_CAPTURE,
    _NATIVE_FINALIZE,
    NativeTarget,
    launch,
)
from .test_native_startup import (
    native as native,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="uses Linux guest runtime commands")


def bind_native_runtime(db, monkeypatch, native: NativeTarget) -> None:
    native.socket = str(native.root / "agt-a1" / "s1.sock")
    Path(native.socket).parent.mkdir()
    monkeypatch.setattr(_lifecycle, "AGENT_SOCKET_ROOT", str(native.root))
    monkeypatch.setattr(_lifecycle, "_teardown_session", manager._teardown_session)
    _patch_transports(monkeypatch, native, native)
    _requiring_template(monkeypatch, "sh")
    monkeypatch.setattr(manager, "_ensure_pid", _pids._ensure_pid)
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    monkeypatch.setattr(manager, "check_session_status", manager._status.check_session_status)
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _NATIVE_CAPTURE)
    monkeypatch.setattr(tmux, "finalize_session_start", _NATIVE_FINALIZE)


@pytest.mark.parametrize("operation", ["start_session", "restart_session"])
@pytest.mark.parametrize("recovery", ["single", "batch"])
def test_unacknowledged_native_spawn_is_recovered_and_finishes_startup(
    tmp_path, monkeypatch, native, operation, recovery
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    # The seed fixture models a stopped session with an old positive identity.
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    secret = uuid4().hex
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {"token": secret})
    passed_secrets = []
    artifact_secrets = []
    original_prepare = ShellIntegration.prepare_artifacts

    def prepare(self, context):
        artifact_secrets.append(context.redactions)
        return original_prepare(self, context)

    monkeypatch.setattr(ShellIntegration, "prepare_artifacts", prepare)

    def fail_after_native_spawn(*args, **kwargs):
        launch(native, "sleep 30", is_admin=False)
        raise ExternalError("lost launch acknowledgement")

    monkeypatch.setattr(tmux, "create_session", fail_after_native_spawn)
    try:
        with pytest.raises(ExternalError):
            getattr(manager, operation)(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        row = db.get_session("s1")
        assert row is not None and row.pid is None
        assert row.boot_id == tmux.read_boot_id(native)
        assert manager._status.check_session_status(row, target=native) is SessionStatus.RUNNING
        observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
        assert observed is not None
        original_pid = observed.pid

        def finalize(*args, **kwargs):
            passed_secrets.append(kwargs["redactions"])
            return _NATIVE_FINALIZE(*args, **kwargs)

        monkeypatch.setattr(tmux, "finalize_session_start", finalize)
        config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
        if recovery == "single":
            manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        else:
            monkeypatch.setattr(manager, "_batch_vm_boundary", lambda *a, **k: nullcontext())
            monkeypatch.setattr(
                manager,
                "observe_session_statuses",
                lambda rows, **k: {row.name: manager._status.check_session_status(row, target=native) for row in rows},
            )
            manager.start_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)
        recovered = db.get_session("s1")
        assert recovered is not None and recovered.pid == original_pid
        assert recovered.tmux_server_start_ticks is not None
        assert Path(native.socket).stat().st_mode & 0o070 == 0o070
        assert native.native("show-option -wAqv -t %0 remain-on-exit").stdout.strip() == "off"
        assert passed_secrets == [(secret,)]
        assert artifact_secrets == [(secret,)]
        assert sum("new-session" in command for command, _ in native.calls) == 1
    finally:
        db.close()


def test_unknown_launch_can_recover_after_reboot_without_destroying_vm(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    socket = "/run/agentworks/agent-tmux-sockets/agt-a1/s1.sock"
    db.update_session_runtime("s1", socket_path=socket, pid=None, boot_id=None, tmux_server_start_ticks=None)
    target = SimpleNamespace(run=lambda *a, **k: None)
    monkeypatch.setattr(
        tmux, "capture_tmux_server_fingerprint", lambda **k: tmux.FingerprintProbe(tmux.ProbeStatus.ABSENT)
    )
    try:
        row = db.get_session("s1")
        with pytest.raises(StateError):
            _pids._repair_session_pid(row, target=target, db=db)
        checkpoint = db.get_session("s1")
        assert checkpoint is not None and checkpoint.pid is None and checkpoint.boot_id == BOOT_ID
        with pytest.raises(StateError):
            _pids._repair_session_pid(checkpoint, target=target, db=db)
        unchanged = db.get_session("s1")
        assert unchanged is not None and unchanged.pid is None
        monkeypatch.setattr(manager, "_get_boot_id", lambda *a, **k: str(uuid4()))
        assert _pids._repair_session_pid(checkpoint, target=target, db=db)
        stopped = db.get_session("s1")
        assert stopped is not None and stopped.pid == PID_STOPPED
        assert db.get_vm("vm1") is not None and db.get_agent("a1") is not None
    finally:
        db.close()


@pytest.mark.parametrize("offset", [480, 485, 490])
def test_dead_pane_secret_is_redacted_before_the_line_cap(offset):
    marker = uuid4().hex
    payload = "x" * offset + marker + "diagnostic tail\n"

    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, ok=True, stdout=payload if "capture-pane" in command else "", stderr="")

    with pytest.raises(StateError) as caught:
        tmux._raise_workload_died("s1", run_command=run, socket_path="/tmp/unneeded", redactions=(marker,))
    assert marker not in str(caught.value)
    assert marker[:8] not in str(caught.value)


def test_logs_discard_unused_screen_rows_and_refuse_negative_count():
    def target(*a, **k):
        return SimpleNamespace(returncode=0, ok=True, stdout="one\ntwo\n\n \n\n", stderr="")

    assert tmux.capture_output("s1", run_command=target, lines=2) == "one\ntwo\n"
    with pytest.raises(ValidationError):
        tmux.capture_output("s1", run_command=target, lines=-2)


def test_native_launch_pane_survives_nondefault_indexes_and_new_active_window(native):
    config = Path(tmux.RESTRICTED_CONFIG_PATH)
    config.write_text("set -g base-index 7\nset -g pane-base-index 4\nset -g remain-on-exit on\n")
    gate = native.root / "exit"
    script = native.root / "launch.sh"
    script.write_text(
        "tmux new-window -d -n other 'sleep 30'\ntmux select-window -t =s1:8\n"
        f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep .02; done\nprintf 'launch-marker\\n'\nexit 31\n"
    )
    socket, _ = launch(native, f"sh {shlex.quote(str(script))}")
    gate.touch()
    native.wait_dead()
    with pytest.raises(StateError) as caught:
        _NATIVE_FINALIZE("s1", run_command=native.run, socket_path=socket, is_admin=True)
    assert "launch-marker" in str(caught.value)
    assert native.native("list-sessions").returncode != 0


def test_native_config_precreated_pane_does_not_steal_launch_identity(native):
    config = Path(tmux.RESTRICTED_CONFIG_PATH)
    config.write_text("set -g remain-on-exit on\nnew-session -d -s existing 'sleep 30'\n")
    socket, _ = launch(native, "sleep 30")
    assert native.native("show-option -gqv @agentworks-launch-pane").stdout.strip() != "%0"
    _NATIVE_FINALIZE("s1", run_command=native.run, socket_path=socket, is_admin=True)
    assert tmux._probe_pane_liveness("s1", run_command=native.run, socket_path=socket) == ("alive", "")
    # Corrupt metadata must not turn the config-created pane into our workload.
    native.native("set-option -g @agentworks-launch-pane %0")
    assert tmux._probe_pane_liveness("s1", run_command=native.run, socket_path=socket) == ("unverified", "")


def test_native_legacy_multi_pane_has_no_guessed_launch_target(native):
    socket, _ = launch(native, "sleep 30")
    native.native("set-option -gu @agentworks-launch-pane")
    assert tmux._probe_pane_liveness("s1", run_command=native.run, socket_path=socket) == ("alive", "")
    native.native("new-window -d 'sleep 30'")
    assert tmux._probe_pane_liveness("s1", run_command=native.run, socket_path=socket) == ("unverified", "")
    assert native.native("list-sessions").ok


@pytest.mark.parametrize("same_boot", [True, False])
def test_boot_only_evidence_has_matching_readonly_status_projections(tmp_path, monkeypatch, native, same_boot):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    row = db.get_session("s1")
    assert row is not None
    checkpoint = replace(
        row,
        pid=None,
        boot_id=tmux.read_boot_id(native) if same_boot else str(uuid4()),
        socket_path=native.socket,
        mode="admin",
        tmux_server_start_ticks=None,
    )
    monkeypatch.setattr(manager, "_get_boot_id", tmux.read_boot_id)
    expected = SessionStatus.UNKNOWN if same_boot else SessionStatus.STOPPED
    from agentworks.sessions.manager._status import batch_check_status, check_session_status

    try:
        assert check_session_status(checkpoint, target=native) is expected
        assert batch_check_status([checkpoint], target=native) == {"s1": expected}
        assert db.get_session("s1") == row
    finally:
        db.close()


def test_native_retry_completes_protected_runtime_with_known_fingerprint(tmp_path, monkeypatch, native):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    monkeypatch.setattr(tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False))
    error = ExternalError("native release refused")

    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(tmux, "finalize_session_start", interrupted)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
    try:
        with pytest.raises(ExternalError) as caught:
            manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        assert caught.value is error
        row = db.get_session("s1")
        assert row is not None and row.pid is not None and row.tmux_server_start_ticks is not None
        monkeypatch.setattr(tmux, "finalize_session_start", _NATIVE_FINALIZE)
        manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        recovered = db.get_session("s1")
        assert recovered is not None and recovered.pid == row.pid
        assert Path(native.socket).stat().st_mode & 0o070 == 0o070
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        assert sum("new-session" in command for command, _ in native.calls) == 1
    finally:
        db.close()


@pytest.mark.parametrize("target_owns_session", [False, True])
def test_parent_cleanup_accepts_stopped_child_with_proven_server_absence(
    tmp_path, monkeypatch, native, target_owns_session
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    row = db.get_session("s1")
    assert row is not None
    try:
        # Agent/workspace deletion calls this directly for every child, including
        # stopped children. It does not take singular delete's status shortcut.
        _lifecycle._teardown_session(
            row,
            target=native,
            target_owns_session=target_owns_session,
            db=db,
            force=True,
            legacy_running_authorized=True,
        )
        stopped = db.get_session("s1")
        assert stopped is not None and stopped.pid == PID_STOPPED
        assert not Path(native.socket).exists()
        assert all("kill-server" not in command for command, _ in native.calls)
    finally:
        db.close()


def test_parent_cleanup_does_not_treat_unknown_probe_as_stopped_absence(tmp_path, monkeypatch, native):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    row = db.get_session("s1")
    assert row is not None
    monkeypatch.setattr(
        tmux, "capture_tmux_server_fingerprint", lambda **k: tmux.FingerprintProbe(tmux.ProbeStatus.UNKNOWN)
    )
    try:
        with pytest.raises(StateError):
            _lifecycle._teardown_session(
                row,
                target=native,
                target_owns_session=False,
                db=db,
                force=True,
                legacy_running_authorized=True,
            )
        assert db.get_session("s1") == row
        assert not native.calls
    finally:
        db.close()
