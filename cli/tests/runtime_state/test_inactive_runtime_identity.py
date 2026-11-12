"""Inactive VM observation retains evidence needed after memory or boot recovery."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agentworks.db import PID_STOPPED, DesiredRuntimeState, SessionRuntimeSnapshot, SessionStatus, VMStatus
from agentworks.errors import StateError
from agentworks.runtime_state import reconcile_session
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _status
from tests.runtime_state.test_composition import REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_public_stop import _running

if TYPE_CHECKING:
    from agentworks.db import SessionRow

NEW_BOOT = "11111111-2222-4333-8444-555555555555"


@dataclass
class _Resident:
    name: str
    vm: str
    socket: str
    fingerprint: tmux.TmuxServerFingerprint
    present: bool = True
    socket_exists: bool = True


class _Target:
    def __init__(self, model: _Memory, vm: str) -> None:
        self.model = model
        self.vm = vm

    def run(self, command: str, **kwargs: object) -> SimpleNamespace:
        if "kill-server" in command:
            words = shlex.split(command)
            resident = self.model.by_socket(words[words.index("-S") + 1])
            assert resident.present
            resident.present = False
            self.model.killed.append(resident.name)
        elif "rm -f --" in command:
            resident = next(state for state in self.model.residents.values() if state.socket in command)
            resident.socket_exists = False
        elif command.startswith("test -e "):
            resident = self.model.by_socket(shlex.split(command)[2])
            code = 0 if resident.socket_exists else 1
            return SimpleNamespace(returncode=code, ok=code == 0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            ok=True,
            stdout="AGW_RUNTIME_STARTUP=off\n" if "AGW_RUNTIME_STARTUP=" in command else "",
            stderr="",
        )

    def write_file(self, *args: object, **kwargs: object) -> None:
        pass


class _Memory:
    def __init__(self) -> None:
        self.residents: dict[str, _Resident] = {}
        self.boots: dict[str, str] = {}
        self.reads: list[str] = []
        self.transports: list[str] = []
        self.killed: list[str] = []
        self.spawned: list[str] = []

    def add(self, row: SessionRow, vm: str) -> None:
        assert row.socket_path is not None and row.pid is not None
        assert row.boot_id is not None and row.tmux_server_start_ticks is not None
        self.residents[row.name] = _Resident(
            row.name,
            vm,
            row.socket_path,
            tmux.TmuxServerFingerprint(row.pid, row.boot_id, row.tmux_server_start_ticks),
        )
        self.boots[vm] = row.boot_id

    def by_socket(self, socket: str) -> _Resident:
        return next(resident for resident in self.residents.values() if resident.socket == socket)

    def transport(self, vm, config, **kwargs) -> _Target:
        self.transports.append(vm.name)
        return _Target(self, vm.name)

    def session_presence(self, name, **kwargs) -> tmux.ProbeStatus:
        self.reads.append(f"session:{name}")
        return tmux.ProbeStatus.PRESENT if self.residents[name].present else tmux.ProbeStatus.ABSENT

    def server_presence(self, *, socket_path, **kwargs) -> tmux.ProbeStatus:
        self.reads.append(f"server:{socket_path}")
        return tmux.ProbeStatus.PRESENT if self.by_socket(socket_path).present else tmux.ProbeStatus.ABSENT

    def capture(self, *, socket_path, **kwargs) -> tmux.FingerprintProbe:
        self.reads.append(f"capture:{socket_path}")
        resident = self.by_socket(socket_path)
        if not resident.present:
            return tmux.FingerprintProbe(tmux.ProbeStatus.ABSENT)
        return tmux.FingerprintProbe(tmux.ProbeStatus.PRESENT, resident.fingerprint)

    def boot(self, target) -> str:
        while hasattr(target, "_target"):
            target = target._target
        self.reads.append(f"boot:{target.vm}")
        return self.boots[target.vm]

    def pid_presence(self, pid, **kwargs) -> tmux.ProbeStatus:
        self.reads.append(f"pid:{pid}")
        return (
            tmux.ProbeStatus.PRESENT
            if any(resident.present and resident.fingerprint.pid == pid for resident in self.residents.values())
            else tmux.ProbeStatus.ABSENT
        )

    def spawn(self, name, *args, **kwargs) -> tuple[str, int]:
        kwargs["before_spawn"]()
        resident = self.residents[name]
        resident.fingerprint = tmux.TmuxServerFingerprint(9001, self.boots[resident.vm], 9002)
        resident.present = resident.socket_exists = True
        self.spawned.append(name)
        return resident.socket, resident.fingerprint.pid


def _model(db, runtime, monkeypatch) -> _Memory:
    _, statuses, _ = runtime
    model = _Memory()
    for name, vm in [("work", "box"), ("peer", "other")]:
        seed_vm(db, vm)
        _running(db, statuses, name, vm)
        db.set_session_run_id(name, str(uuid4()))
        db.record_session_started(name)
        model.add(db.get_session(name), vm)
    monkeypatch.setattr(manager, "transport", model.transport)
    monkeypatch.setattr("agentworks.transports.transport", model.transport)
    monkeypatch.setattr(_status, "probe_tmux_session", model.session_presence)
    monkeypatch.setattr(_status, "probe_tmux_server", model.server_presence)
    monkeypatch.setattr(tmux, "probe_tmux_session", model.session_presence)
    monkeypatch.setattr(tmux, "probe_tmux_server", model.server_presence)
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", model.capture)
    monkeypatch.setattr(tmux, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_get_boot_id", model.boot)
    monkeypatch.setattr(manager, "_pid_presence", model.pid_presence)
    monkeypatch.setattr(manager, "check_session_status", _status.check_session_status)

    def observe_rows(rows, **kwargs):
        return {
            row.name: manager.check_session_status(row, target=_Target(model, model.residents[row.name].vm))
            for row in rows
        }

    monkeypatch.setattr(manager, "observe_session_statuses", observe_rows)
    monkeypatch.setattr(tmux, "create_session", model.spawn)
    return model


@pytest.mark.parametrize("operation", ["named", "bulk", "reconcile"])
def test_inactive_stop_retains_same_boot_memory_identity_for_later_safe_stop(
    db, make_config, runtime, monkeypatch, operation
):
    powers, _, events = runtime
    model = _model(db, runtime, monkeypatch)
    if operation == "reconcile":
        manager.set_session_desired_runtime_state(db, name="work", state=DesiredRuntimeState.STOPPED)
    before = db.get_session("work")
    peer = db.get_session("peer")
    peer_native = replace(model.residents["peer"])
    powers["box"] = VMStatus.STOPPED
    config = make_config()
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows: rendered.extend(rows))
    if operation == "named":
        manager.stop_session(db, config, name="work", interaction=REFUSE)
    elif operation == "bulk":
        manager.stop_all_sessions(db, config, workspace_name="ws-box", interaction=REFUSE)
    else:
        reconcile_session(db, config, name="work", interaction=REFUSE)

    result = next(row for row in rendered if row.kind == "session")
    assert result.observation is SessionStatus.STOPPED and result.observation_from_parent
    inactive = db.get_session("work")
    assert inactive == replace(
        before, desired_runtime_state=DesiredRuntimeState.STOPPED, updated_at=inactive.updated_at
    )
    assert SessionRuntimeSnapshot.from_session(inactive) == SessionRuntimeSnapshot.from_session(before)
    assert model.residents["work"].present
    assert model.reads == model.transports == model.killed == []
    assert not events
    reconcile_session(db, config, name="work", interaction=REFUSE)
    assert db.get_session("work") == inactive
    assert model.reads == model.transports == model.killed == []

    powers["box"] = VMStatus.RUNNING
    assert manager.check_session_status(inactive, target=_Target(model, "box")) is SessionStatus.RUNNING
    rendered.clear()
    reconcile_session(db, config, name="work", interaction=REFUSE)
    result = next(row for row in rendered if row.kind == "session")
    assert result.observation is SessionStatus.STOPPED and not result.observation_from_parent

    assert model.killed == ["work"]
    assert not model.residents["work"].present
    assert db.get_session("work").pid == PID_STOPPED
    assert db.get_session("work").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert model.spawned == []
    assert db.get_session("peer") == peer
    assert model.residents["peer"] == peer_native


def test_real_shutdown_new_boot_can_restore_running_intent_without_adopting_old_identity(
    db, make_config, runtime, monkeypatch
):
    powers, _, _ = runtime
    model = _model(db, runtime, monkeypatch)
    before = db.get_session("work")
    peer = db.get_session("peer")
    powers["box"] = VMStatus.STOPPED
    config = make_config()
    manager.stop_session(db, config, name="work", interaction=REFUSE)
    assert SessionRuntimeSnapshot.from_session(db.get_session("work")) == SessionRuntimeSnapshot.from_session(before)
    model.residents["work"].present = model.residents["work"].socket_exists = False
    model.boots["box"] = NEW_BOOT
    powers["box"] = VMStatus.RUNNING
    manager.set_session_desired_runtime_state(db, name="work", state=DesiredRuntimeState.RUNNING)
    reconcile_session(db, config, name="work", interaction=REFUSE)
    current = db.get_session("work")
    assert current.session_uuid == before.session_uuid and current.run_id != before.run_id
    assert (current.pid, current.boot_id, current.tmux_server_start_ticks) == (9001, NEW_BOOT, 9002)
    assert model.spawned == ["work"] and model.killed == []
    assert db.get_session("peer") == peer
    reconcile_session(db, config, name="work", interaction=REFUSE)
    assert db.get_session("work") == current
    assert model.spawned == ["work"]


def test_resumed_memory_with_changed_native_incarnation_is_not_adopted_or_killed(db, make_config, runtime, monkeypatch):
    powers, _, _ = runtime
    model = _model(db, runtime, monkeypatch)
    peer = db.get_session("peer")
    powers["box"] = VMStatus.STOPPED
    config = make_config()
    manager.stop_session(db, config, name="work", interaction=REFUSE)
    retained = db.get_session("work")
    resident = model.residents["work"]
    resident.fingerprint = replace(resident.fingerprint, start_ticks=resident.fingerprint.start_ticks + 1)
    powers["box"] = VMStatus.RUNNING
    with pytest.raises(StateError):
        reconcile_session(db, config, name="work", interaction=REFUSE)
    assert resident.present and model.killed == model.spawned == []
    assert db.get_session("work") == retained
    assert db.get_session("peer") == peer
