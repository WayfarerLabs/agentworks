"""PID repair persists only against its original runtime snapshot."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, Database, SessionMode, SessionRuntimeSnapshot
from agentworks.errors import StateConflictError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _lifecycle, _pids, _teardown

from .test_session_batch_stop_parallelism import BOOT_ID, _seed_session, _Target

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import SessionRow
    from agentworks.transports import Transport
    from tests.conftest import CapturedOutput


BRANCHES = ("legacy-boot", "recovered", "dedicated-boot", "stopped", "legacy-stopped")
SOCKET_PATH = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"


def _session(db: Database, branch: str) -> SessionRow:
    _seed_session(
        db, "session", socket_path=None if branch.startswith("legacy") else SOCKET_PATH, pid=101, start_ticks=None
    )
    db.update_session_runtime(
        "session",
        socket_path=None if branch.startswith("legacy") else SOCKET_PATH,
        pid=None,
        boot_id=None if branch.endswith("boot") else BOOT_ID,
        tmux_server_start_ticks=None,
    )
    db.set_session_run_id("session", str(uuid4()))
    session = db.get_session("session")
    assert session is not None
    return session


def _observations(monkeypatch: pytest.MonkeyPatch, branch: str, before_write: Callable[[], None]) -> None:
    monkeypatch.setattr(tmux, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(tmux, "probe_tmux_session", lambda *args, **kwargs: tmux.ProbeStatus.ABSENT)
    monkeypatch.setattr(
        tmux,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: (
            tmux.FingerprintProbe(tmux.ProbeStatus.PRESENT, tmux.TmuxServerFingerprint(101, BOOT_ID, 201))
            if branch == "recovered"
            else tmux.FingerprintProbe(tmux.ProbeStatus.ABSENT)
        ),
    )

    def observed_boot(*args: object, **kwargs: object) -> str:
        before_write()
        return BOOT_ID

    def proof(*args: object, **kwargs: object) -> bool:
        before_write()
        return True

    monkeypatch.setattr(manager, "_get_boot_id", observed_boot)
    monkeypatch.setattr(tmux, "_matches_launch_attempt", proof)
    monkeypatch.setattr(_pids, "_prove_stored_runtime_absent", proof)


def _replace(other: Database, session: SessionRow, change: str) -> SessionRow | None:
    if change == "missing":
        other.delete_session(session.name)
    elif change == "row":
        other.delete_session(session.name)
        other.insert_session(session.name, "work", "default", SessionMode.ADMIN, socket_path=session.socket_path)
        other._conn.execute(
            "UPDATE sessions SET run_id = ?, pid = ?, boot_id = ?, tmux_server_start_ticks = ? WHERE name = ?",
            (session.run_id, session.pid, session.boot_id, session.tmux_server_start_ticks, session.name),
        )
        other._conn.commit()
    else:
        values: dict[str, str | int] = {
            "run_id": str(uuid4()),
            "socket_path": "/replacement.sock",
            "pid": 909,
            "boot_id": str(uuid4()),
            "tmux_server_start_ticks": 919,
        }
        # Only the fixed parameterized runtime columns below select this SQL column.
        other._conn.execute(f"UPDATE sessions SET {change} = ? WHERE name = ?", (values[change], session.name))
        other._conn.commit()
    return other.get_session(session.name)


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize(
    "change", ["run_id", "row", "socket_path", "pid", "boot_id", "tmux_server_start_ticks", "missing"]
)
def test_each_repair_write_preserves_replacement_from_another_connection(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, branch: str, change: str
) -> None:
    session = _session(db, branch)
    changed: list[SessionRow | None] = []
    with contextlib.closing(Database(db.path)) as other:
        _observations(monkeypatch, branch, lambda: changed.append(_replace(other, session, change)))
        with pytest.raises(StateConflictError):
            _pids._repair_session_pid(session, target=cast("Transport", _Target("repair")), db=db)
        assert len(changed) == 1
        assert db.get_session(session.name) == changed[0]
        assert other.get_session(session.name) == changed[0]
    assert captured_output.lines == []


@pytest.mark.parametrize("branch", BRANCHES)
def test_repair_retains_positive_recovery_and_learned_boot_refusal(
    db: Database, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    session = _session(db, branch)
    _observations(monkeypatch, branch, lambda: None)
    expected_runtime: tuple[int | None, str | None, int | None]
    if branch.endswith("boot"):
        with pytest.raises(StateError) as caught:
            _pids._repair_session_pid(session, target=cast("Transport", _Target("repair")), db=db)
        assert not isinstance(caught.value, StateConflictError)
        expected_runtime = (None, BOOT_ID, None)
    else:
        assert _pids._repair_session_pid(session, target=cast("Transport", _Target("repair")), db=db)
        expected_runtime = (101, BOOT_ID, 201) if branch == "recovered" else (PID_STOPPED, None, None)
    current = db.get_session(session.name)
    assert current is not None
    assert (current.pid, current.boot_id, current.tmux_server_start_ticks) == expected_runtime
    assert current.socket_path == (None if branch == "legacy-boot" else SOCKET_PATH)
    assert (current.session_uuid, current.run_id) == (session.session_uuid, session.run_id)
    assert not db._conn.in_transaction
    with contextlib.closing(Database(db.path)) as other:
        assert other.get_session(session.name) == current


@pytest.mark.parametrize("branch", BRANCHES)
def test_repair_writes_respect_enclosing_transaction_rollback(
    db: Database, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    session = _session(db, branch)
    _observations(monkeypatch, branch, lambda: None)
    with pytest.raises(StateError), db.transaction():
        _pids._repair_session_pid(session, target=cast("Transport", _Target("repair")), db=db, announce=False)
        raise StateError("roll back the completed repair")
    assert db.get_session(session.name) == session
    with contextlib.closing(Database(db.path)) as other:
        assert other.get_session(session.name) == session


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("caller", ["named", "batch"])
def test_repair_conflict_refuses_callers_before_status_or_destructive_dispatch(
    db: Database, monkeypatch: pytest.MonkeyPatch, branch: str, caller: str
) -> None:
    session = _session(db, branch)
    with contextlib.closing(Database(db.path)) as other:
        changed: list[SessionRow | None] = []
        _observations(monkeypatch, branch, lambda: changed.append(_replace(other, session, "run_id")))
        monkeypatch.setattr(manager, "_batch_vm_boundary", lambda *args, **kwargs: contextlib.nullcontext())
        monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *args, **kwargs: None)
        monkeypatch.setattr(manager, "transport", lambda *args, **kwargs: _Target("repair"))
        workspace = db.get_workspace(session.workspace_name)
        assert workspace is not None
        vm = db.get_vm(workspace.vm_name)
        assert vm is not None
        target = _Target("repair")
        monkeypatch.setattr(
            manager,
            "_prepare_vm",
            lambda *args, **kwargs: contextlib.nullcontext((workspace, vm, target.run, target.run, target)),
        )
        monkeypatch.setattr(manager, "check_session_status", lambda *args, **kwargs: pytest.fail("stale named status"))
        monkeypatch.setattr(
            manager, "observe_session_statuses", lambda *args, **kwargs: pytest.fail("stale status dispatch")
        )
        monkeypatch.setattr(
            _teardown, "execute_dedicated_teardown", lambda *args: pytest.fail("stale destructive dispatch")
        )
        with pytest.raises(StateConflictError):
            if caller == "named":
                _lifecycle.stop_session(
                    db, cast("Config", SimpleNamespace()), name=session.name, interaction=TtyInteractionPolicy.REFUSE
                )
            else:
                _lifecycle.stop_all_sessions(
                    db, cast("Config", SimpleNamespace()), interaction=TtyInteractionPolicy.REFUSE
                )
        assert len(changed) == 1
        assert db.get_session(session.name) == changed[0]
        assert other.get_session(session.name) == changed[0]


@pytest.mark.parametrize(
    "pid,boot,ticks",
    [
        (-2, None, None),
        (101, None, 201),
        (101, BOOT_ID, None),
        (101, BOOT_ID, 0),
        (PID_STOPPED, BOOT_ID, None),
        (PID_STOPPED, None, 201),
        (None, BOOT_ID, 201),
    ],
)
@pytest.mark.parametrize("conditional", [False, True])
def test_runtime_repair_preserves_existing_desired_shape_validation(
    db: Database, conditional: bool, pid: int | None, boot: str | None, ticks: int | None
) -> None:
    session = _session(db, "recovered")
    with pytest.raises(ValueError):
        if conditional:
            db.compare_and_set_session_runtime_repair(
                SessionRuntimeSnapshot.from_session(session),
                socket_path=SOCKET_PATH,
                pid=pid,
                boot_id=boot,
                tmux_server_start_ticks=ticks,
            )
        else:
            db.update_session_runtime(
                session.name, socket_path=SOCKET_PATH, pid=pid, boot_id=boot, tmux_server_start_ticks=ticks
            )
    assert db.get_session(session.name) == session


def test_batch_repair_retains_best_effort_for_ordinary_ownership_refusal(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db, "recovered")
    _observations(monkeypatch, "recovered", lambda: None)
    monkeypatch.setattr(tmux, "_matches_launch_attempt", lambda **kwargs: False)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "transport", lambda *args, **kwargs: _Target("repair"))
    result = _pids.ensure_pids_batch([session], db=db, config=cast("Config", SimpleNamespace()))
    assert result == [session]
    assert db.get_session(session.name) == session
