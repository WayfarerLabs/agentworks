"""Session delete's follow-on cleanup keeps a workspace or agent that stopped being a candidate before its guard.

The cleanup decides a workspace or agent is disposable after session
delete's guard is released, and the delete it triggers resolves its boundary
before taking its own guard. A grant committed in between is not one the
cleanup saw, so the delete refuses under its guard and the grant survives.
These run the real deleters against the shared workspace lifecycle guest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.db import SessionRow
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions.manager._queries import _cleanup_now_empty_agent, _cleanup_now_empty_workspace
from tests.workspaces._lifecycle_support import Guest, reachable, seed
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database


def _deleted_session(*, agent_name: str | None = None) -> SessionRow:
    """The deleted session as session delete snapshotted it, having created its workspace and agent."""
    return SessionRow(
        name="s1",
        workspace_name="ws1",
        template="default",
        mode="agent" if agent_name else "admin",
        created_at="t",
        updated_at="t",
        agent_name=agent_name,
        created_workspace=True,
        created_agent=True,
    )


def _commit_while_resolving(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Commit ``change`` as another command would while the delete resolves its boundary, before its guard."""
    resolve = Resolver.resolve

    def resolve_then_change(self: Resolver) -> None:
        resolve(self)
        change()

    monkeypatch.setattr(Resolver, "resolve", resolve_then_change)


def test_an_auto_deleted_workspace_keeps_a_grant_committed_before_its_guard(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    reachable(monkeypatch, True)
    _commit_while_resolving(monkeypatch, lambda: db.insert_agent_grant("a1", "ws1", "explicit"))

    _cleanup_now_empty_workspace(
        db, make_config(), _deleted_session(), yes=True, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_workspace("ws1") is not None
    assert db.list_workspace_explicit_granters("ws1") == ["a1"]
    assert guest.requests == []


def test_an_offered_workspace_keeps_a_grant_its_offer_did_not_name(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator accepted revoking a1's grant; a2's, committed after the offer, was never shown."""
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    db.insert_agent("a2", "box", "agt-a2", template="default")
    db.insert_agent_grant("a1", "ws1", "explicit")
    reachable(monkeypatch, True)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", lambda *a, **k: True)
    _commit_while_resolving(monkeypatch, lambda: db.insert_agent_grant("a2", "ws1", "explicit"))

    _cleanup_now_empty_workspace(
        db, make_config(), _deleted_session(), yes=False, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_workspace("ws1") is not None
    assert db.list_workspace_explicit_granters("ws1") == ["a1", "a2"]
    assert guest.requests == []


def test_an_auto_deleted_agent_keeps_a_grant_committed_before_its_guard(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    reachable(monkeypatch, True)
    _commit_while_resolving(monkeypatch, lambda: db.insert_agent_grant("a1", "ws1", "explicit"))

    _cleanup_now_empty_agent(
        db, make_config(), _deleted_session(agent_name="a1"), yes=True, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_agent("a1") is not None
    assert db.list_workspace_explicit_granters("ws1") == ["a1"]
    assert guest.requests == []
