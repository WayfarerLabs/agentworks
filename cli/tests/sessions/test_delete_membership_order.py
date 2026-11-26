"""Session delete removes the agent's last workspace membership before it deletes the session and grant rows."""

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
    from tests.conftest import _ScriptedGuest


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
