"""Real tmux access grants converge across interrupted startup completion."""

from __future__ import annotations

import os
import shlex
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import AgentworksError, ExternalError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux

from .test_create_start_restart_orchestrated import _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, NativeTarget, launch
from .test_native_startup import native as native
from .test_round8_startup import bind_native_runtime

_GRANT_ACCESS = tmux._grant_server_access

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a Linux tmux server")


@pytest.fixture
def access_user():
    import pwd

    candidates = [user.pw_name for user in pwd.getpwall() if user.pw_uid not in {0, os.geteuid()}]
    if not candidates:
        pytest.skip("requires an existing non-owner Unix identity")
    return candidates[0]


def bind_access_group(monkeypatch, native: NativeTarget, members: str, *, lookup_exit: int = 0):
    """Control NSS enumeration without creating users or changing real groups."""
    native_run = native.run
    lookup = f"printf '%s\\n' {shlex.quote('fixture:x:1000:' + members)}; return {lookup_exit}"

    def run(command, **kwargs):
        return native_run(f"getent() {{ {lookup}; }}; {command}", **kwargs)

    monkeypatch.setattr(native, "run", run)
    monkeypatch.setattr(tmux, "_grant_server_access", _GRANT_ACCESS)


def test_interrupted_native_start_recovers_existing_access_without_replacing_runtime(
    tmp_path, monkeypatch, native, access_user
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    # Seed valid stopped state before launching; no launch evidence is edited.
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    bind_access_group(monkeypatch, native, access_user)
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
        fingerprint = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
        assert fingerprint is not None
        assert (before.pid, before.boot_id, before.tmux_server_start_ticks) == (
            fingerprint.pid,
            fingerprint.boot_id,
            fingerprint.start_ticks,
        )
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is True
        assert f"{access_user} (W)" in native.native("server-access -l").stdout.splitlines()

        monkeypatch.setattr(native, "run", native_run)
        manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        assert sum("new-session" in command for command, _ in native.calls) == 1
        assert not any("kill-server" in command for command, _ in native.calls)
        assert f"{access_user} (W)" in native.native("server-access -l").stdout.splitlines()
    finally:
        db.close()


def test_unacknowledged_native_launch_recovers_unknown_pid_and_ticks_with_access(
    tmp_path, monkeypatch, native, access_user
):
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    db.update_session_runtime(
        "s1", socket_path=native.socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    bind_access_group(monkeypatch, native, access_user)

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
        fingerprint = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
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
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint
        assert tmux.startup_protection_enabled("s1", run_command=native.run, socket_path=native.socket) is False
        assert sum("new-session" in command for command, _ in native.calls) == 1
        assert not any("kill-server" in command for command, _ in native.calls)
        assert f"{access_user} (W)" in native.native("server-access -l").stdout.splitlines()
    finally:
        db.close()


@pytest.mark.parametrize("initial_access", ["absent", "read-only", "read-write"])
def test_native_grants_ensure_read_write_access_and_can_repeat(monkeypatch, native, access_user, initial_access):
    bind_access_group(monkeypatch, native, access_user)
    launch(native, "sleep 120", is_admin=False)
    if initial_access != "absent":
        flag = "-r" if initial_access == "read-only" else "-w"
        assert native.native(f"server-access {flag} {shlex.quote(access_user)}").ok
    fingerprint = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint

    for _ in range(2):
        _GRANT_ACCESS(native.run, native.socket)
        assert f"{access_user} (W)" in native.native("server-access -l").stdout.splitlines()
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == fingerprint


def test_failed_group_lookup_preserves_access_failure(monkeypatch, native, access_user):
    bind_access_group(monkeypatch, native, access_user, lookup_exit=47)
    launch(native, "sleep 120", is_admin=False)
    before = native.native("server-access -l").stdout

    with pytest.raises(ExternalError):
        _GRANT_ACCESS(native.run, native.socket)

    assert native.native("server-access -l").stdout == before


def test_early_grant_failure_is_not_hidden_by_a_later_valid_member(monkeypatch, native, access_user):
    bind_access_group(monkeypatch, native, f"agw886-{uuid4().hex[:16]},{access_user}")
    launch(native, "sleep 120", is_admin=False)
    before = native.native("server-access -l").stdout

    with pytest.raises(ExternalError):
        _GRANT_ACCESS(native.run, native.socket)

    assert native.native("server-access -l").stdout == before


def test_present_empty_group_needs_no_access_mutation(monkeypatch, native):
    bind_access_group(monkeypatch, native, "")
    launch(native, "sleep 120", is_admin=False)
    before = native.native("server-access -l").stdout

    _GRANT_ACCESS(native.run, native.socket)

    assert native.native("server-access -l").stdout == before
