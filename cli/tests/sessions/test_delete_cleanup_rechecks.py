"""Session delete's follow-on cleanup keeps a workspace or agent that stopped being the candidate it chose.

The cleanup decides a workspace or agent is disposable after session
delete's lock set is released, and the delete it triggers reads its rows,
resolves its boundary and only then takes its own set. A grant committed
before that read is refused at the read; one committed after it is a new
entity of the delete's request, refused under its set. Either way the grant
survives. These run the real deleters against the shared workspace
lifecycle guest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.db import SessionRow
from agentworks.errors import AgentworksError, ConnectivityError, StateConflictError
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.output import Role
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource
from agentworks.sessions.manager._delete import _cleanup_now_empty_agent, _cleanup_now_empty_workspace
from tests.workspaces._lifecycle_support import Guest, argv, reachable, route, seed
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.conftest import CapturedOutput


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
    """Commit ``change`` as another command would while the delete resolves its boundary, before its lock set."""
    resolve = Resolver.resolve

    def resolve_then_change(self: Resolver) -> None:
        resolve(self)
        change()

    monkeypatch.setattr(Resolver, "resolve", resolve_then_change)


def test_an_auto_deleted_workspace_keeps_a_grant_committed_before_its_lock(
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


def test_an_auto_deleted_agent_keeps_a_grant_committed_before_its_lock(
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


def _commit_while_offering(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Commit ``change`` while the operator answers the offer: after it chose, before the delete reads its rows."""

    def confirm_then_change(*args: object, **kwargs: object) -> bool:
        change()
        return True

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "confirm", confirm_then_change)


def test_an_offered_workspace_keeps_a_grant_committed_while_the_offer_was_answered(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    reachable(monkeypatch, True)
    _commit_while_offering(monkeypatch, lambda: db.insert_agent_grant("a1", "ws1", "explicit"))

    _cleanup_now_empty_workspace(
        db, make_config(), _deleted_session(), yes=False, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_workspace("ws1") is not None
    assert db.list_workspace_explicit_granters("ws1") == ["a1"]
    assert guest.requests == []


def test_an_offered_agent_keeps_a_grant_committed_while_the_offer_was_answered(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    reachable(monkeypatch, True)
    _commit_while_offering(monkeypatch, lambda: db.insert_agent_grant("a1", "ws1", "explicit"))

    _cleanup_now_empty_agent(
        db, make_config(), _deleted_session(agent_name="a1"), yes=False, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_agent("a1") is not None
    assert db.list_workspace_explicit_granters("ws1") == ["a1"]
    assert guest.requests == []


class _AccountsStopGuest(Guest):
    """A healthy guest whose ``pkill`` ends every process of an account being removed."""

    def respond(self, request: object) -> Result | None:
        if isinstance(request, Command) and argv(request)[:2] == ("pgrep", "-u"):
            return Result(Completed(1))
        return super().respond(request)


def test_with_nothing_committed_meanwhile_cleanup_deletes_the_workspace_and_then_the_agent(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    reachable(monkeypatch, True)
    route(monkeypatch, _AccountsStopGuest())
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    config = make_config()
    session = _deleted_session(agent_name="a1")

    _cleanup_now_empty_workspace(db, config, session, yes=True, interaction=TtyInteractionPolicy.REFUSE)
    _cleanup_now_empty_agent(db, config, session, yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_workspace("ws1") is None
    assert db.get_agent("a1") is None


@pytest.mark.parametrize(
    ("refusal", "warned"),
    [(StateConflictError("changed"), False), (ConnectivityError("unreachable"), True)],
    ids=["changed-since-chosen", "failed"],
)
def test_a_delete_refused_as_changed_reports_the_resource_kept_and_any_other_failure_warns(
    captured_output: CapturedOutput, refusal: AgentworksError, warned: bool
) -> None:
    """A conflict kept the resource on purpose, so nothing warns or offers a command to delete it."""

    def delete() -> None:
        raise refusal

    cleanup_now_empty_resource(
        kind="workspace",
        name="ws1",
        created=True,
        delete=delete,
        manual_command="agw workspace delete ws1",
        yes=True,
        empty_clause="now has no sessions",
        report_clause="now has no sessions",
    )

    assert any(role is Role.WARNING for role, _level, _message in captured_output.lines) is warned
