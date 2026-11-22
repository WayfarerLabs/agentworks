"""Agent session startup on a real tmux server: recovery converges, and nobody but the owner gains access."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import AgentworksError, ExternalError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux

from .test_create_start_restart_orchestrated import _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, NativeGuest, launch
from .test_native_startup import native as native
from .test_round8_startup import bind_native_runtime

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a Linux tmux server")


def _owner() -> str:
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def _access_list(native: NativeGuest) -> list[str]:
    """The accounts in the server's access list, as ``server-access -l`` names them."""
    return [line.split()[0] for line in native.native("server-access -l").stdout.splitlines()]


def test_interrupted_native_start_recovers_without_replacing_runtime_or_sharing_the_server(
    tmp_path, monkeypatch, native
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    # Seed valid stopped state before launching; no launch evidence is edited.
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    monkeypatch.setattr(
        tmux,
        "create_session",
        lambda *args, **kwargs: launch(native, "sleep 120", is_admin=False, launch_options=kwargs),
    )
    native_run = native.run
    interrupted = KeyboardInterrupt()

    def interrupt_release(command, **kwargs):
        if "remain-on-exit off" in command:
            raise interrupted
        return native_run(command, **kwargs)

    monkeypatch.setattr(native, "run", interrupt_release)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        assert caught.value is interrupted
        before = db.get_session("s1")
        assert before is not None and before.pid is not None and before.pid > 0
        assert before.run_id is not None
        fingerprint = _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).fingerprint
        assert fingerprint is not None
        assert (before.pid, before.boot_id, before.tmux_server_start_ticks) == (
            fingerprint.pid,
            fingerprint.boot_id,
            fingerprint.start_ticks,
        )
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is True
        assert _access_list(native) == [_owner()]

        monkeypatch.setattr(native, "run", native_run)
        manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).fingerprint == fingerprint
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        assert sum("new-session" in command for command, _ in native.calls) == 1
        assert not any("kill-server" in command for command, _ in native.calls)
        assert _access_list(native) == [_owner()]
    finally:
        db.close()


def test_unacknowledged_native_launch_recovers_unknown_pid_and_ticks_without_sharing_the_server(
    tmp_path, monkeypatch, native
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )

    def lose_acknowledgement(*args, **kwargs):
        launch(native, "sleep 120", is_admin=False, launch_options=kwargs)
        raise ExternalError("controlled launch acknowledgement loss")

    monkeypatch.setattr(tmux, "create_session", lose_acknowledgement)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
    try:
        with pytest.raises(AgentworksError):
            manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        before = db.get_session("s1")
        assert before is not None and before.pid is None and before.tmux_server_start_ticks is None
        assert before.run_id is not None
        fingerprint = _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).fingerprint
        assert fingerprint is not None

        manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

        recovered = db.get_session("s1")
        assert recovered is not None
        assert (recovered.session_uuid, recovered.run_id, recovered.socket_path) == (
            before.session_uuid,
            before.run_id,
            before.socket_path,
        )
        assert (recovered.pid, recovered.boot_id, recovered.tmux_server_start_ticks) == (
            fingerprint.pid,
            fingerprint.boot_id,
            fingerprint.start_ticks,
        )
        assert _NATIVE_CAPTURE(run_command=native.run, socket_path=native.socket).fingerprint == fingerprint
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        assert sum("new-session" in command for command, _ in native.calls) == 1
        assert not any("kill-server" in command for command, _ in native.calls)
        assert _access_list(native) == [_owner()]
    finally:
        db.close()
