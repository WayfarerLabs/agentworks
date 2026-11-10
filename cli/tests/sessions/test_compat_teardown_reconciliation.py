"""Compatibility teardown preserves concurrent replacement rows."""

from __future__ import annotations

import contextlib
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, Database, SessionMode, SessionRuntimeSnapshot, SessionStatus
from agentworks.errors import ExternalError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.sessions import tmux as tmux_mod
from agentworks.sessions.manager import _lifecycle as lifecycle_mod
from agentworks.sessions.manager import _teardown as teardown_mod

from .test_session_batch_stop_parallelism import BOOT_ID, _seed_session, _Target

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import SessionRow
    from agentworks.transports import Transport


def _compat_session(db: Database, branch: str) -> SessionRow:
    session = _seed_session(
        db,
        "session",
        socket_path=None if branch == "legacy" else "/run/agentworks/admin-tmux-sockets/agentworks/session.sock",
        pid=PID_STOPPED if branch == "stopped" else 101,
        start_ticks=201 if branch == "complete" else None,
    )
    if branch == "legacy":
        db._conn.execute("UPDATE sessions SET pid = NULL, boot_id = NULL WHERE name = ?", (session.name,))
        db._conn.commit()
        refreshed = db.get_session(session.name)
        assert refreshed is not None
        session = refreshed
    return session


def _native_absence(monkeypatch: pytest.MonkeyPatch, at_cleanup: Callable[[], None]) -> None:
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: tmux_mod.FingerprintProbe(tmux_mod.ProbeStatus.ABSENT),
    )
    monkeypatch.setattr(session_manager, "_prove_stored_runtime_absent", lambda *args, **kwargs: True)
    monkeypatch.setattr(teardown_mod, "_remove_stale_socket", lambda **kwargs: at_cleanup())

    def legacy_probe(*args: object, **kwargs: object) -> tmux_mod.ProbeStatus:
        at_cleanup()
        return tmux_mod.ProbeStatus.ABSENT

    monkeypatch.setattr(tmux_mod, "probe_tmux_session", legacy_probe)


def _stop(db: Database, session: SessionRow) -> None:
    lifecycle_mod._teardown_session(
        session,
        target=cast("Transport", _Target("compat")),
        target_owns_session=True,
        db=db,
        force=False,
        legacy_running_authorized=True,
    )


@pytest.mark.parametrize("branch", ["partial", "stopped", "legacy"])
def test_compatibility_completion_persists_verified_stopped_state(
    db: Database, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    session = _compat_session(db, branch)
    cleanups: list[bool] = []
    _native_absence(monkeypatch, lambda: cleanups.append(True))

    _stop(db, session)

    stopped = db.get_session(session.name)
    assert stopped is not None
    assert (stopped.pid, stopped.boot_id, stopped.tmux_server_start_ticks) == (PID_STOPPED, None, None)
    assert (stopped.session_uuid, stopped.run_id, stopped.socket_path) == (
        session.session_uuid,
        session.run_id,
        session.socket_path,
    )
    assert cleanups == [True]


@pytest.mark.parametrize("branch", ["partial", "stopped", "legacy"])
@pytest.mark.parametrize("change", ["run", "row", "runtime"])
def test_compatibility_completion_preserves_replacement_from_another_connection(
    db: Database, monkeypatch: pytest.MonkeyPatch, branch: str, change: str
) -> None:
    session = _compat_session(db, branch)
    changed_rows: list[SessionRow] = []
    with contextlib.closing(Database(db.path)) as other:

        def replace() -> None:
            if change == "run":
                other.set_session_run_id(session.name, str(uuid4()))
            elif change == "row":
                other.delete_session(session.name)
                other.insert_session(
                    session.name, "work", "default", SessionMode.ADMIN, socket_path=session.socket_path
                )
                other._conn.execute(
                    "UPDATE sessions SET run_id = ?, pid = ?, boot_id = ?, tmux_server_start_ticks = ? WHERE name = ?",
                    (session.run_id, session.pid, session.boot_id, session.tmux_server_start_ticks, session.name),
                )
                other._conn.commit()
            else:
                other.update_session_runtime(
                    session.name, socket_path=session.socket_path, pid=909, boot_id=BOOT_ID, tmux_server_start_ticks=919
                )
            changed = other.get_session(session.name)
            assert changed is not None
            changed_rows.append(changed)

        _native_absence(monkeypatch, replace)
        with pytest.raises(StateError):
            _stop(db, session)

        assert db.get_session(session.name) == changed_rows[0]
        assert other.get_session(session.name) == changed_rows[0]


@pytest.mark.parametrize("branch", ["complete", "partial", "stopped", "legacy"])
def test_nullable_stopped_reconciliation_retries_after_interrupted_commit(db: Database, branch: str) -> None:
    expected = SessionRuntimeSnapshot.from_session(_compat_session(db, branch))
    commit = db._commit_unless_in_tx

    def interrupt_after_commit() -> None:
        commit()
        raise KeyboardInterrupt

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "_commit_unless_in_tx", interrupt_after_commit)
        with pytest.raises(KeyboardInterrupt):
            db.compare_and_set_session_stopped(expected)
    db.compare_and_set_session_stopped(expected)
    stopped = db.get_session(expected.name)
    assert stopped is not None and stopped.pid == PID_STOPPED and stopped.boot_id is None


@pytest.mark.parametrize("branch", ["partial", "stopped", "legacy"])
@pytest.mark.parametrize(
    "field,value",
    [("socket_path", "/replacement.sock"), ("pid", 909), ("boot_id", "other-boot"), ("tmux_server_start_ticks", 919)],
)
def test_each_nullable_runtime_field_fences_stopped_persistence(
    db: Database, branch: str, field: str, value: object
) -> None:
    session = _compat_session(db, branch)
    expected = SessionRuntimeSnapshot.from_session(session)
    with contextlib.closing(Database(db.path)) as other:
        # The column is selected exclusively from the fixed parameter list above.
        other._conn.execute(f"UPDATE sessions SET {field} = ? WHERE name = ?", (value, session.name))
        other._conn.commit()
        changed = other.get_session(session.name)
        assert changed is not None
        with pytest.raises(StateError):
            db.compare_and_set_session_stopped(expected)
        assert db.get_session(session.name) == changed


def test_runtime_snapshot_copies_nullable_fields_before_mutable_row_changes(db: Database) -> None:
    session = _compat_session(db, "legacy")
    snapshot = SessionRuntimeSnapshot.from_session(session)
    session.pid = 909
    session.run_id = str(uuid4())
    assert snapshot.pid is None and snapshot.run_id is None and snapshot.socket_path is None
    with pytest.raises(FrozenInstanceError):
        snapshot.pid = 910  # type: ignore[misc]


@pytest.mark.parametrize("mode", ["serial", "batch"])
@pytest.mark.parametrize("field,value", [("pid", 0), ("pid", -2), ("boot_id", "bad"), ("tmux_server_start_ticks", 0)])
def test_malformed_complete_fields_never_authorize_native_work(
    db: Database, monkeypatch: pytest.MonkeyPatch, mode: str, field: str, value: object
) -> None:
    session = _seed_session(
        db,
        "session",
        socket_path="/run/agentworks/admin-tmux-sockets/agentworks/session.sock",
        pid=101,
        start_ticks=201,
    )
    setattr(session, field, value)
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: pytest.fail("native work authorized"))
    if mode == "serial":
        with pytest.raises(StateError):
            _stop(db, session)
    else:
        monkeypatch.setattr(session_manager, "_batch_vm_boundary", lambda *args, **kwargs: contextlib.nullcontext())
        monkeypatch.setattr(session_manager, "ensure_pids_batch", lambda sessions, **kwargs: [session])
        monkeypatch.setattr(
            session_manager,
            "observe_session_statuses",
            lambda sessions, **kwargs: {session.name: SessionStatus.RUNNING},
        )
        monkeypatch.setattr(session_manager, "transport", lambda *args, **kwargs: _Target("invalid"))
        with pytest.raises(ExternalError):
            lifecycle_mod.stop_all_sessions(
                db, cast("Config", SimpleNamespace()), interaction=TtyInteractionPolicy.REFUSE
            )
    current = db.get_session(session.name)
    assert current is not None and (current.pid, current.boot_id, current.tmux_server_start_ticks) == (
        101,
        BOOT_ID,
        201,
    )
