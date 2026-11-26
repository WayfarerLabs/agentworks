"""A restart probes its sessions' lock sets before its consent, so the operator is never asked about a held one."""

from __future__ import annotations

import pytest

from agentworks.db import Database, SessionStatus
from agentworks.errors import StateError
from agentworks.native_locks import LockRequest, NativeLockBusyError, native_locks
from agentworks.native_locks.rows import session_entity_for, vm_exclusive
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from agentworks.sessions.manager import _lifecycle
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_composition import seed_session, seed_vm

type Runtime = tuple[dict, dict[str, SessionStatus], list[tuple[str, str]]]


def _session_lock(db: Database, name: str) -> LockRequest:
    session = db.get_session(name)
    assert session is not None
    return LockRequest(exclusive=(session_entity_for(db, session),))


def _never_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: pytest.fail("asked the operator"))
    monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("observed for consent"))


@pytest.mark.parametrize("spec", [None, '{"env":{"NEW":"after"}}'], ids=["plain", "spec"])
def test_a_held_session_refuses_its_restart_before_the_operator_is_asked(
    db: Database, make_config, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, spec: str | None
) -> None:
    seed_vm(db, "box")
    seed_session(db, "work", "box")
    runtime[1]["work"] = SessionStatus.RUNNING
    _never_asked(monkeypatch)
    with native_locks(db.path, _session_lock(db, "work")), pytest.raises(NativeLockBusyError) as refused:
        manager.restart_session(db, make_config(), name="work", spec=spec, interaction=TtyInteractionPolicy.ALLOW)
    assert refused.value.entity_kind == "session"
    assert refused.value.entity_name == "work"
    assert runtime[2] == []


def test_a_busy_vm_refuses_a_restart_before_the_operator_is_asked(
    db: Database, make_config, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vm(db, "box")
    seed_session(db, "work", "box")
    _never_asked(monkeypatch)
    with native_locks(db.path, vm_exclusive("box")), pytest.raises(NativeLockBusyError) as refused:
        manager.restart_session(db, make_config(), name="work", interaction=TtyInteractionPolicy.ALLOW)
    assert refused.value.entity_kind == "vm"


def test_a_batch_restart_asks_only_about_the_sessions_it_can_take(
    db: Database, make_config, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vm(db, "box")
    seed_session(db, "held", "box")
    seed_session(db, "free", "box")
    runtime[1].update({"held": SessionStatus.RUNNING, "free": SessionStatus.RUNNING})
    monkeypatch.setattr(
        "agentworks.sessions.manager._runtime.observe_session_runtime",
        lambda session, **kwargs: runtime[1].get(session.name, SessionStatus.STOPPED),
    )
    monkeypatch.setattr(
        manager, "_teardown_session", lambda session, *a, **k: runtime[1].update({session.name: SessionStatus.STOPPED})
    )
    asked: list[list[str]] = []
    monkeypatch.setattr(_lifecycle, "_confirm_running_restart", lambda names, **k: asked.append(list(names)))
    with native_locks(db.path, _session_lock(db, "held")), pytest.raises(StateError) as refused:
        manager.restart_all_sessions(db, make_config(), vm_name="box", interaction=TtyInteractionPolicy.ALLOW)
    assert asked == [["free"]]
    assert refused.value.entity_name == "held"
    assert ("session-start", "free") in runtime[2]
    assert ("session-start", "held") not in runtime[2]
