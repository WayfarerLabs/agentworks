"""Historical partial fingerprints cannot authorize a fresh native server."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import AgentworksError
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _pids

from .test_create_start_restart_orchestrated import _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry
from .test_native_startup import _NATIVE_CAPTURE, launch
from .test_native_startup import native as native
from .test_round8_startup import bind_native_runtime
from .test_round9_runtime import invoke

if TYPE_CHECKING:
    from agentworks.db import Database

    from .test_native_startup import NativeTarget


def partial_runtime(
    tmp_path, monkeypatch, native: NativeTarget, markers: str
) -> tuple[Database, tmux.TmuxServerFingerprint]:
    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.STOPPED)
    bind_native_runtime(db, monkeypatch, native)
    monkeypatch.setattr("agentworks.agents.grants.remove_from_workspace_group", lambda *a, **k: None)
    db.set_session_run_id("s1", str(uuid4()))
    row = db.get_session("s1")
    assert row is not None
    options = (
        {}
        if markers == "missing"
        else {
            "session_uuid": str(uuid4()) if markers == "foreign-session" else row.session_uuid,
            "run_id": str(uuid4()) if markers == "foreign-run" else row.run_id,
        }
    )
    launch(native, "sleep 30", is_admin=False, launch_options=options)
    observed = _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint
    assert observed is not None
    # This represents a historical row whose PID was reused by the actual
    # native server, without inventing a historical process start time.
    db._conn.execute(
        "UPDATE sessions SET socket_path=?, pid=?, boot_id=?, tmux_server_start_ticks=NULL WHERE name='s1'",
        (native.socket, observed.pid, observed.boot_id),
    )
    db._conn.commit()
    original_run = native.run

    def wait_for_native_exit(command, **kwargs):
        result = original_run(command, **kwargs)
        if "kill-server" in command:
            original_run(f"while test -d /proc/{observed.pid}; do sleep .01; done")
        return result

    monkeypatch.setattr(native, "run", wait_for_native_exit)
    monkeypatch.setattr(
        manager,
        "observe_session_statuses",
        lambda rows, **k: {row.name: manager.check_session_status(row, target=native) for row in rows},
    )
    monkeypatch.setattr(
        tmux, "create_session", lambda *a, **k: launch(native, "sleep 30", is_admin=False, launch_options=k)
    )
    return db, observed


def act(db: Database, native: NativeTarget, operation: str) -> None:
    row = db.get_session("s1")
    assert row is not None
    if operation == "repair":
        _pids._repair_session_pid(row, target=native, db=db)
    elif operation == "teardown":
        manager._teardown_session(
            row, target=native, target_owns_session=True, db=db, force=True, legacy_running_authorized=False
        )
    else:
        invoke(db, operation, force=True)


@pytest.mark.parametrize("operation", ["repair", "teardown", "stop_session", "restart_session", "delete_session"])
@pytest.mark.parametrize("markers", ["missing", "foreign-session", "foreign-run"])
def test_partial_fingerprint_never_relearns_or_kills_unproven_native_server(
    tmp_path, monkeypatch, native, markers, operation
):
    db, observed = partial_runtime(tmp_path, monkeypatch, native, markers)
    before = db.get_session("s1")
    assert before is not None
    first = len(native.calls)
    try:
        assert not _pids._needs_repair(before)
        assert _pids._ensure_pid(before, target=native, db=db) == before
        assert manager.check_session_status(before, target=native) is SessionStatus.RUNNING
        assert manager.batch_check_status([replace(before, mode="admin")], target=native) == {
            "s1": SessionStatus.RUNNING
        }
        assert not any("show-environment" in command for command, _ in native.calls[first:])
        with pytest.raises(AgentworksError) as caught:
            act(db, native, operation)
        if operation in {"repair", "teardown"}:
            assert caught.value.hint
        assert db.get_session("s1") == before
        assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == observed
        assert native.native("has-session -t '=s1'").ok
        assert not any("kill-server" in command or "new-session" in command for command, _ in native.calls[first:])
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["repair", "teardown", "stop_session", "restart_session", "delete_session"])
def test_partial_fingerprint_can_complete_with_matching_native_attempt(tmp_path, monkeypatch, native, operation):
    db, observed = partial_runtime(tmp_path, monkeypatch, native, "matching")
    try:
        act(db, native, operation)
        row = db.get_session("s1")
        if operation == "delete_session":
            assert row is None
        elif operation == "repair":
            assert row is not None and row.tmux_server_start_ticks == observed.start_ticks
            assert _NATIVE_CAPTURE(target=native, socket_path=native.socket).fingerprint == observed
        elif operation == "restart_session":
            assert row is not None and row.tmux_server_start_ticks is not None
            assert manager.check_session_status(row, target=native) is SessionStatus.RUNNING
        else:
            assert row is not None and row.pid == PID_STOPPED
    finally:
        db.close()


@pytest.mark.parametrize("old_boot", [False, True])
def test_partial_unmarked_runtime_can_recover_after_native_absence(tmp_path, monkeypatch, native, old_boot):
    db, observed = partial_runtime(tmp_path, monkeypatch, native, "missing")
    if old_boot:
        db._conn.execute("UPDATE sessions SET boot_id=? WHERE name='s1'", (str(uuid4()),))
        db._conn.commit()
    native.native("kill-server")
    native.run(f"while test -d /proc/{observed.pid}; do sleep .01; done")
    try:
        act(db, native, "repair")
        row = db.get_session("s1")
        assert row is not None and row.pid == PID_STOPPED and row.tmux_server_start_ticks is None
    finally:
        db.close()
