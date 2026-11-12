"""Legacy observation and runtime recovery keep their separate authority."""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, Database, SessionStatus
from agentworks.errors import AgentworksError, ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager._lifecycle import _redacted_launch_errors

from ._runtime_assertions import assert_runtime_preserved
from .test_create_start_restart_orchestrated import _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, _NATIVE_FINALIZE, NativeTarget, launch
from .test_native_startup import native as native
from .test_round8_startup import bind_native_runtime


def record_native_runtime(db: Database, native: NativeTarget) -> tmux.TmuxServerFingerprint:
    socket, _ = launch(native, "sleep 30", is_admin=False)
    fingerprint = _NATIVE_CAPTURE(target=native, socket_path=socket).fingerprint
    assert fingerprint is not None
    db.update_session_runtime(
        "s1",
        socket_path=socket,
        pid=fingerprint.pid,
        boot_id=fingerprint.boot_id,
        tmux_server_start_ticks=fingerprint.start_ticks,
    )
    return fingerprint


def test_legacy_multiwindow_retains_running_noop_and_active_pane_logs(tmp_path, monkeypatch, native):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    fingerprint = record_native_runtime(db, native)
    _NATIVE_FINALIZE("s1", run_command=native.run, socket_path=native.socket, is_admin=False)
    marker = uuid4().hex
    native.native("set-option -gu @agentworks-launch-pane")
    native.native(f"new-window -n operator {shlex.quote(f'printf {marker}; sleep 30')}")
    try:
        deadline = time.monotonic() + 3
        captured = ""
        while marker not in captured and time.monotonic() < deadline:
            captured = tmux.capture_output("s1", run_command=native.run, socket_path=native.socket)
            time.sleep(0.01)
        assert marker in captured
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        # Read-only legacy log selection is not authority for startup mutations.
        assert tmux._probe_pane_liveness("s1", run_command=native.run, socket_path=native.socket) == ("unverified", "")
        first = len(native.calls)
        manager.start_session(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert not any("chmod " in command or "remain-on-exit off" in command for command, _ in native.calls[first:])
    finally:
        db.close()


@pytest.mark.parametrize("metadata", ["", "invalid", "%0\n%1", "%999999"])
def test_present_malformed_or_missing_pane_metadata_never_uses_active_fallback(native, metadata):
    socket, _ = launch(native, "sleep 30")
    native.native(f"set-option -g @agentworks-launch-pane {shlex.quote(metadata)}")
    assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=socket) is None
    with pytest.raises(ExternalError):
        tmux.capture_output("s1", run_command=native.run, socket_path=socket)
    assert native.native("list-sessions").ok


@pytest.mark.parametrize("pid", [PID_STOPPED, None, 99999999])
@pytest.mark.parametrize("known_boot", [False, True])
def test_redirected_managed_socket_preserves_other_runtime_and_row(tmp_path, monkeypatch, native, pid, known_boot):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    owned_socket = native.socket
    native.socket = str(native.root / "other.sock")
    launch(native, "sleep 30", is_admin=False)
    native.native("rename-session -t =s1 other")
    Path(owned_socket).symlink_to(native.socket)
    db._conn.execute(
        "UPDATE sessions SET socket_path=?, pid=?, boot_id=?, tmux_server_start_ticks=? WHERE name='s1'",
        (owned_socket, pid, tmux.read_boot_id(native) if known_boot else None, 77 if pid == 99999999 else None),
    )
    db._conn.commit()
    before = db.get_session("s1")
    first = len(native.calls)
    try:
        with pytest.raises(AgentworksError):
            manager.stop_session(db, SimpleNamespace(), name="s1", interaction=TtyInteractionPolicy.REFUSE)
        assert_runtime_preserved(db, before, operation="stop_session")
        assert Path(owned_socket).is_symlink()
        assert native.native("list-sessions").ok
        assert not any(
            "kill-server" in command or "chmod " in command or "server-access" in command
            for command, _ in native.calls[first:]
        )
    finally:
        db.close()


@pytest.mark.parametrize("changed", ["pid", "boot_id", "start_ticks"])
def test_recovery_refuses_mismatched_complete_process_fingerprint(tmp_path, monkeypatch, native, changed):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    fingerprint = record_native_runtime(db, native)
    db.update_session_runtime(
        "s1",
        socket_path=native.socket,
        pid=fingerprint.pid + 1000000 if changed == "pid" else fingerprint.pid,
        boot_id=str(uuid4()) if changed == "boot_id" else fingerprint.boot_id,
        tmux_server_start_ticks=fingerprint.start_ticks + 1 if changed == "start_ticks" else fingerprint.start_ticks,
    )
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
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is True
        assert not any("chmod " in command or "remain-on-exit off" in command for command, _ in native.calls[first:])
    finally:
        db.close()


@pytest.mark.parametrize("replace_runtime", [False, True])
def test_recovery_rechecks_identity_after_secret_resolution(tmp_path, monkeypatch, native, replace_runtime):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    fingerprint = record_native_runtime(db, native)
    mutation_boundary = []

    from agentworks.secrets.resolver import Resolver

    original_resolve = Resolver.resolve

    def resolve(self: Resolver) -> None:
        original_resolve(self)
        if replace_runtime:
            native.native("kill-server")
            deadline = time.monotonic() + 3
            while Path(f"/proc/{fingerprint.pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not Path(f"/proc/{fingerprint.pid}").exists()
            launch(native, "sleep 30", is_admin=False)
        mutation_boundary.append(len(native.calls))

    monkeypatch.setattr(Resolver, "resolve", resolve)
    try:
        if replace_runtime:
            with pytest.raises(StateError):
                manager.start_session(
                    db,
                    SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                    name="s1",
                    interaction=TtyInteractionPolicy.REFUSE,
                )
            observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
            assert observed is not None and observed != fingerprint
            assert not any(
                "chmod " in command or "remain-on-exit off" in command
                for command, _ in native.calls[mutation_boundary[0] :]
            )
        else:
            manager.start_session(
                db,
                SimpleNamespace(session=SimpleNamespace(history_limit=1)),
                name="s1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
            assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert (
            tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is replace_runtime
        )
    finally:
        db.close()


@pytest.mark.parametrize("offset", [480, 485, 490])
def test_multiline_ansi_secret_is_redacted_whole_before_caps_and_outer_handler(offset):
    marker = uuid4().hex
    secret = "FIRST-" + marker + "\nSECOND-" + marker
    rendered = secret[:15] + "\x1b[31m" + secret[15:]
    captured = "x" * offset + rendered + " tail\n"

    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, ok=True, stdout=captured if "capture-pane" in command else "", stderr="")

    with pytest.raises(StateError) as caught, _redacted_launch_errors((secret,)):
        tmux._raise_workload_died("s1", run_command=run, socket_path="/tmp/unneeded", redactions=(secret,))
    assert marker not in str(caught.value) and marker[:8] not in str(caught.value)
    assert "\x1b" not in str(caught.value)
    lines = tmux._trim_captured_output("header\r\n" + rendered + "\r\nfooter", redactions=(secret,)).splitlines()
    assert len(lines) == 3 and lines[0] == "header" and lines[-1] == "footer"
    assert marker not in "".join(lines)
