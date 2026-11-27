"""Session delete: its questions come before the guard, its re-checks under it, and the last membership goes first."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionStatus
from agentworks.execution.errors import CommandFailed
from agentworks.execution.results import Completed, Result
from agentworks.secrets.policy import TtyInteractionPolicy
from tests._consoles_support import _stub_build_registry, _StubConfig  # noqa: F401
from tests.sessions.test_delete_resource_cleanup import _prep_delete_session

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput, _ScriptedGuest


def _agent_session(db: Database) -> None:
    db.insert_agent("bot", "vm1", "agt-bot")
    db._conn.execute(
        "UPDATE sessions SET mode = 'agent', agent_name = 'bot', socket_path = ? WHERE name = 's'",
        ("/run/agentworks/agent-tmux-sockets/agt-bot/s.sock",),
    )
    db._conn.commit()
    db.insert_agent_grant("bot", "ws-vm1", "implicit", session_name="s")


@pytest.mark.parametrize("other_grant", [False, True], ids=["last-grant", "explicit-grant-remains"])
def test_a_failed_membership_removal_keeps_the_session_and_grant_rows_for_a_rerun(
    db: Database,
    scripted_guest: _ScriptedGuest,
    monkeypatch: pytest.MonkeyPatch,
    other_grant: bool,
) -> None:
    from agentworks.agents import grants as agent_grants
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch)
    _agent_session(db)
    if other_grant:
        db.insert_agent_grant("bot", "ws-vm1", "explicit")
    monkeypatch.setattr(manager_mod, "_verified_session_access", lambda session, **kwargs: kwargs["admin"])
    monkeypatch.setattr(manager_mod, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)
    failures = [CommandFailed("vm1", 10, Result(Completed(10)), streams_logged=True)]
    removed: list[str] = []

    def remove(vm: object, config: object, db_: object, linux_user: str, workspace: str, **kwargs: object) -> None:
        if failures:
            raise failures.pop()
        removed.append(workspace)

    monkeypatch.setattr(agent_grants, "remove_from_workspace_group", remove)

    def delete() -> None:
        manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    if other_grant:
        delete()
        assert removed == []
        assert db.get_session("s") is None
        assert db.has_any_grant("bot", "ws-vm1")
        return

    with pytest.raises(CommandFailed):
        delete()

    assert db.get_session("s") is not None
    assert db.has_any_grant("bot", "ws-vm1")

    delete()

    assert removed == ["ws-vm1"]
    assert db.get_session("s") is None
    assert not db.has_any_grant("bot", "ws-vm1")


def _guard_free(db: Database) -> bool:
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    try:
        with native_mutation_guard(db.path, "vm1"):
            return True
    except NativeSetupBusyError:
        return False


@pytest.mark.parametrize("answer", [True, False], ids=["accepted", "declined"])
def test_delete_asks_both_questions_with_no_guard_held(
    db: Database,
    scripted_guest: _ScriptedGuest,
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
    captured_output: CapturedOutput,
) -> None:
    """The confirmation is asked before the guard and the offer for the console
    the delete left empty after it; an accepted console is deleted and a
    declined one is kept without a report."""
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch)
    db.insert_console("only", "vm1")
    db.add_console_session("only", "s", [])
    asked: list[tuple[str, bool]] = []

    def confirm(message: str, default: bool = False) -> bool:
        asked.append(("console" if "only" in message else "session", _guard_free(db)))
        return answer if "only" in message else True

    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", confirm)
    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", lambda *a, **k: None)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=TtyInteractionPolicy.REFUSE)

    assert asked[:2] == [("session", True), ("console", True)]
    assert db.get_session("s") is None
    assert (db.get_console("only") is None) is answer
    if not answer:
        # Declined, so kept without a report.
        assert not any("only" in warning for warning in captured_output.warnings)


def test_a_status_that_changed_before_the_guard_refuses_the_delete(
    db: Database,
    scripted_guest: _ScriptedGuest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import StateConflictError
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch)
    statuses = iter([SessionStatus.STOPPED, SessionStatus.RUNNING])
    monkeypatch.setattr(manager_mod, "check_session_status", lambda *a, **k: next(statuses))
    torn_down: list[str] = []
    monkeypatch.setattr(manager_mod, "_teardown_session", lambda session, **k: torn_down.append(session.name))

    with pytest.raises(StateConflictError):
        manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert torn_down == []
    assert db.get_session("s") is not None
