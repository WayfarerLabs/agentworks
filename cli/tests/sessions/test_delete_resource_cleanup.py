"""Tests for the generalized workspace/agent cleanup on ``session delete``.

Issue #266 broadened the old provenance-only cleanup (offer to delete a
workspace / agent only when THIS session created it) into a general
"now-empty" cleanup: whenever deleting a session leaves its underlying
workspace or agent unused, offer to delete it interactively regardless of
provenance, and under --yes auto-delete only what this session created
(otherwise report-but-keep, mirroring the console cascade). A workspace is
unused when it has no sessions; an agent is unused only when it has no
sessions AND no standing workspace grant (no explicit grant row and
grant_all unset), since a standing grant is operator intent to use the
agent (the pre-#266 suppression, preserved).

The offer/report logic lives in ``_cleanup_now_empty_workspace`` /
``_cleanup_now_empty_agent``; those are exercised directly here (the full
``delete_session`` SSH machinery is orthogonal to the branch logic), with a
handful of admin-session integration tests through ``delete_session`` to
prove the wiring. The reusable ``workspace_has_sessions`` /
``agent_is_unused`` predicates (shared with the #268 prune command) are
unit-tested on their own.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from agentworks.agents.manager import agent_has_grants, agent_has_sessions, agent_is_unused
from agentworks.db import PID_STOPPED, Database, SessionRow
from agentworks.errors import ConnectivityError
from agentworks.secrets.policy import InteractionPolicy
from agentworks.sessions.manager._queries import (
    _cleanup_now_empty_agent,
    _cleanup_now_empty_workspace,
)
from agentworks.workspaces.manager import workspace_external_explicit_granters, workspace_has_sessions
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401

if TYPE_CHECKING:
    import pytest

    from tests.conftest import CapturedOutput, _FakeTarget


def _session_snapshot(
    name: str,
    workspace_name: str,
    *,
    agent_name: str | None = None,
    created_workspace: bool = False,
    created_agent: bool = False,
) -> SessionRow:
    """A pre-delete session snapshot, as ``delete_session`` captures before
    the row is removed. ``created_at`` / ``updated_at`` are irrelevant to the
    cleanup helpers."""
    return SessionRow(
        name=name,
        workspace_name=workspace_name,
        template="default",
        mode="agent" if agent_name else "admin",
        created_at="t",
        updated_at="t",
        agent_name=agent_name,
        created_workspace=created_workspace,
        created_agent=created_agent,
    )


def _seed_agent(db: Database, name: str = "bot") -> None:
    db.insert_agent(name, "vm1", f"{name}-user")


# ---------------------------------------------------------------------------
# Reusable "is unused" predicates (shared with the #268 prune command)
# ---------------------------------------------------------------------------


def test_workspace_has_sessions_predicate(db: Database) -> None:
    _seed_vm(db)
    assert workspace_has_sessions(db, "ws-vm1") is False
    _seed_sessions(db, ["a"])
    assert workspace_has_sessions(db, "ws-vm1") is True


def test_workspace_external_explicit_granters_predicate(db: Database) -> None:
    """Non-grant-all agents with an explicit grant on the workspace, sorted;
    implicit rows and grant-all agents' materialized rows are excluded."""
    _seed_vm(db)
    assert workspace_external_explicit_granters(db, "ws-vm1") == []
    # An implicit (session-tied) grant never counts.
    db.insert_agent("a", "vm1", "a-user")
    db.insert_agent_grant("a", "ws-vm1", "implicit", session_name="s")
    assert workspace_external_explicit_granters(db, "ws-vm1") == []
    # An explicit grant from a non-grant-all agent counts.
    db.insert_agent_grant("a", "ws-vm1", "explicit")
    assert workspace_external_explicit_granters(db, "ws-vm1") == ["a"]
    # A grant-all agent's materialized explicit row is excluded by its flag.
    db.insert_agent("z", "vm1", "z-user", grant_all=True)
    db.insert_agent_grant("z", "ws-vm1", "explicit")
    assert workspace_external_explicit_granters(db, "ws-vm1") == ["a"]
    # Multiple granters come back sorted by agent name.
    db.insert_agent("b", "vm1", "b-user")
    db.insert_agent_grant("b", "ws-vm1", "explicit")
    assert workspace_external_explicit_granters(db, "ws-vm1") == ["a", "b"]


def test_agent_has_sessions_predicate(db: Database) -> None:
    _seed_vm(db)
    _seed_agent(db)
    assert agent_has_sessions(db, "bot") is False
    # An admin session on the same workspace does not keep the agent alive.
    _seed_sessions(db, ["adm"])
    assert agent_has_sessions(db, "bot") is False
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')"
    )
    db._conn.commit()
    assert agent_has_sessions(db, "bot") is True


def test_agent_has_grants_predicate(db: Database) -> None:
    _seed_vm(db)
    _seed_agent(db)
    assert agent_has_grants(db, "bot") is False
    # Implicit (session-tied) grants do not count toward "in use".
    db.insert_agent_grant("bot", "ws-vm1", "implicit", session_name="s")
    assert agent_has_grants(db, "bot") is False
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    assert agent_has_grants(db, "bot") is True


def test_agent_grant_all_counts_as_standing_grant(db: Database) -> None:
    """The grant_all flag is standing intent even with ZERO explicit rows
    (grant_all rows cascade away as workspaces are deleted), so it must be
    read from the agent row, not inferred from the grant table."""
    _seed_vm(db)
    db.insert_agent("bot", "vm1", "bot-user", grant_all=True)
    # No explicit grant rows, no sessions, but grant_all holds.
    assert db.list_granted_workspaces_with_types("bot") == []
    assert agent_has_grants(db, "bot") is True
    assert agent_is_unused(db, "bot") is False
    # Clearing grant_all (still no rows, no sessions) makes it unused.
    db.update_agent_grant_all("bot", False)
    assert agent_has_grants(db, "bot") is False
    assert agent_is_unused(db, "bot") is True


def test_agent_is_unused_predicate(db: Database) -> None:
    """Agent unused = no sessions AND no standing grant; any one blocks."""
    _seed_vm(db)
    _seed_agent(db)
    assert agent_is_unused(db, "bot") is True
    # An explicit grant alone makes it "in use".
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    assert agent_is_unused(db, "bot") is False
    db.delete_agent_grant("bot", "ws-vm1", "explicit")
    assert agent_is_unused(db, "bot") is True
    # grant_all alone also makes it "in use".
    db.update_agent_grant_all("bot", True)
    assert agent_is_unused(db, "bot") is False
    db.update_agent_grant_all("bot", False)
    assert agent_is_unused(db, "bot") is True
    # A remaining session alone also makes it "in use".
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')"
    )
    db._conn.commit()
    assert agent_is_unused(db, "bot") is False


# ---------------------------------------------------------------------------
# Workspace cleanup helper
# ---------------------------------------------------------------------------


def _spy_delete_workspace(db: Database, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record delete_workspace calls and perform the DB-row removal so state
    assertions hold, without the real VM/SSH teardown."""
    calls: list[str] = []

    def spy(db_: Database, config: object, name: str, **kwargs: object) -> None:
        calls.append(name)
        db_.delete_workspace(name)

    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", spy)
    return calls


def _record_confirm(monkeypatch: pytest.MonkeyPatch, *, answer: bool) -> list[str]:
    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return answer

    # A stubbed confirm models an interactive operator; the helper's TTY gate
    # (report-but-keep without one) is pinned by its own dedicated test.
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)
    return prompts


def test_now_empty_workspace_offered_and_deleted_interactive(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Interactive, not created by the session: the now-empty workspace is
    offered regardless of provenance, and an accepted offer deletes it."""
    _seed_vm(db)
    calls = _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any("ws-vm1" in p and "now has no sessions" in p for p in prompts)
    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None


def test_now_empty_workspace_created_interactive_offer_notes_provenance(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A workspace this session created carries a provenance cue in the
    interactive offer, so the operator recognizes what they made."""
    _seed_vm(db)
    calls = _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=True)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any("Workspace 'ws-vm1' (created with this session) now has no sessions" in p for p in prompts)
    assert calls == ["ws-vm1"]


def test_now_empty_workspace_declined_interactive_keeps(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A declined offer leaves the empty workspace in place."""
    _seed_vm(db)
    calls = _spy_delete_workspace(db, monkeypatch)
    _record_confirm(monkeypatch, answer=False)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert calls == []
    assert db.get_workspace("ws-vm1") is not None


def test_now_empty_workspace_yes_not_created_reports_but_keeps(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Under --yes, a now-empty workspace this session did NOT create is
    reported (with the manual command) but not deleted."""
    _seed_vm(db)
    calls = _spy_delete_workspace(db, monkeypatch)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == []
    assert db.get_workspace("ws-vm1") is not None
    assert any(
        "ws-vm1" in w and "now has no sessions" in w and "agw workspace delete ws-vm1" in w
        for w in captured_output.warnings
    )


def test_now_empty_workspace_yes_created_auto_deletes(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Under --yes, a workspace this session created is auto-deleted with no
    prompt (the pre-#266 behavior, preserved)."""
    _seed_vm(db)
    calls = _spy_delete_workspace(db, monkeypatch)

    def _no_confirm(message: str, default: bool = False) -> bool:
        raise AssertionError(f"unexpected confirm under --yes: {message}")

    monkeypatch.setattr("agentworks.output.confirm", _no_confirm)

    session = _session_snapshot("s", "ws-vm1", created_workspace=True)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None


def test_now_empty_workspace_yes_created_kept_when_external_explicit_grant(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The grant guard: under --yes, a workspace this session CREATED is NOT
    auto-deleted when another agent holds an explicit grant on it (deleting
    would silently revoke that grant via the FK cascade). It is reported and
    kept, and the report names the granting agent."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    calls = _spy_delete_workspace(db, monkeypatch)

    def _no_confirm(message: str, default: bool = False) -> bool:
        raise AssertionError(f"unexpected confirm under --yes: {message}")

    monkeypatch.setattr("agentworks.output.confirm", _no_confirm)

    session = _session_snapshot("s", "ws-vm1", created_workspace=True)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == []
    assert db.get_workspace("ws-vm1") is not None
    assert any(
        "ws-vm1" in w and "agent(s) bot hold explicit grants" in w and "agw workspace delete ws-vm1" in w
        for w in captured_output.warnings
    )


def test_now_empty_workspace_yes_created_auto_deletes_with_only_grant_all_row(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The maintainer's scenario: a grant-all agent's materialized explicit row
    is blanket policy, not per-workspace intent, so it is excluded by the flag
    and does NOT block the --yes auto-delete of a session-created workspace."""
    _seed_vm(db)
    db.insert_agent("botall", "vm1", "botall-user", grant_all=True)
    db.insert_agent_grant("botall", "ws-vm1", "explicit")  # materialized by grant_all
    calls = _spy_delete_workspace(db, monkeypatch)

    def _no_confirm(message: str, default: bool = False) -> bool:
        raise AssertionError(f"unexpected confirm under --yes: {message}")

    monkeypatch.setattr("agentworks.output.confirm", _no_confirm)

    session = _session_snapshot("s", "ws-vm1", created_workspace=True)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None
    assert not captured_output.warnings


def test_now_empty_workspace_interactive_offer_discloses_grants(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Transparency: the interactive offer for a workspace carrying external
    explicit grants discloses whose grants a delete would revoke."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    calls = _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any(
        "Workspace 'ws-vm1' now has no sessions (deleting revokes explicit grant(s) held by: bot). Delete it?" in p
        for p in prompts
    )
    # An accepted offer still deletes the workspace (and its grants cascade).
    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None


def test_now_empty_workspace_interactive_offer_plain_with_only_grant_all_row(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A grant-all agent's materialized row is excluded, so the interactive
    offer stays plain: no grant-disclosure noise."""
    _seed_vm(db)
    db.insert_agent("botall", "vm1", "botall-user", grant_all=True)
    db.insert_agent_grant("botall", "ws-vm1", "explicit")  # materialized by grant_all
    _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any("Workspace 'ws-vm1' now has no sessions. Delete it?" in p for p in prompts)
    assert all("revokes explicit grant" not in p for p in prompts)


def test_workspace_with_remaining_sessions_not_touched(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A workspace that still has other sessions is neither offered nor
    deleted, under both interactive and --yes."""
    _seed_vm(db)
    _seed_sessions(db, ["other"])
    calls = _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=True)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert prompts == []
    assert calls == []
    assert not captured_output.warnings
    assert db.get_workspace("ws-vm1") is not None


def test_now_empty_workspace_delete_failure_warns(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A follow-on delete_workspace that raises AgentworksError is swallowed
    with a warning; it must not propagate (the session is already gone)."""
    _seed_vm(db)

    def _boom(*a: object, **k: object) -> None:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", _boom)
    _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", created_workspace=False)
    # No exception escapes.
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert db.get_workspace("ws-vm1") is not None
    assert any(
        "Could not delete empty workspace 'ws-vm1'" in w and "agw workspace delete ws-vm1" in w
        for w in captured_output.warnings
    )


# ---------------------------------------------------------------------------
# Agent cleanup helper
# ---------------------------------------------------------------------------


def _spy_delete_agent(db: Database, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def spy(db_: Database, config: object, *, name: str, **kwargs: object) -> None:
        calls.append(name)
        db_.delete_agent(name)

    monkeypatch.setattr("agentworks.agents.manager.delete_agent", spy)
    return calls


def test_now_empty_agent_offered_and_deleted_interactive(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Interactive, not created by the session: a now-empty agent is offered
    regardless of provenance, and an accepted offer deletes it."""
    _seed_vm(db)
    _seed_agent(db)
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=False)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any("bot" in p and "now has no sessions" in p for p in prompts)
    assert calls == ["bot"]
    assert db.get_agent("bot") is None


def test_now_empty_agent_created_interactive_offer_notes_provenance(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """An agent this session created carries a provenance cue in the
    interactive offer."""
    _seed_vm(db)
    _seed_agent(db)
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=True)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert any("Agent 'bot' (created with this session) now has no sessions" in p for p in prompts)
    assert calls == ["bot"]
    assert db.get_agent("bot") is None


def test_now_empty_agent_yes_not_created_reports_but_keeps(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_agent(db)
    calls = _spy_delete_agent(db, monkeypatch)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=False)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == []
    assert db.get_agent("bot") is not None
    assert any(
        "bot" in w and "now has no sessions" in w and "agw agent delete bot" in w for w in captured_output.warnings
    )


def test_now_empty_agent_yes_created_auto_deletes(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_agent(db)
    calls = _spy_delete_agent(db, monkeypatch)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=True)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == ["bot"]
    assert db.get_agent("bot") is None


def test_admin_session_skips_agent_cleanup(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """An admin session (agent_name is None) has no agent to clean up: no
    prompt, no report, no delete."""
    _seed_vm(db)
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name=None)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert prompts == []
    assert calls == []
    assert not captured_output.warnings


def test_agent_with_remaining_sessions_not_touched(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_agent(db)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('other', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/other.sock')"
    )
    db._conn.commit()
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=True)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert prompts == []
    assert calls == []
    assert not captured_output.warnings
    assert db.get_agent("bot") is not None


def test_now_empty_agent_delete_failure_warns(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_agent(db)

    def _boom(*a: object, **k: object) -> None:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    monkeypatch.setattr("agentworks.agents.manager.delete_agent", _boom)
    _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=False)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert db.get_agent("bot") is not None
    assert any(
        "Could not delete empty agent 'bot'" in w and "agw agent delete bot" in w for w in captured_output.warnings
    )


def test_granted_sessionless_agent_not_touched(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A sessionless agent that still holds an explicit workspace grant is not
    a cleanup candidate: not offered, not reported, not auto-deleted, under
    interactive and --yes, created and not-created (the pre-#266 suppression
    the grant guard preserves)."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    for created in (False, True):
        session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=created)
        _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
        _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert prompts == []
    assert calls == []
    assert not captured_output.warnings
    assert db.get_agent("bot") is not None


def test_agent_becomes_candidate_once_grant_removed(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Revoking the explicit grant (no sessions, no grants) makes the agent a
    candidate again: the offer path fires."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=False)
    # Still granted: suppressed.
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    assert prompts == []
    assert calls == []

    # Grant revoked: now a candidate, so the offer fires and, accepted, deletes.
    db.delete_agent_grant("bot", "ws-vm1", "explicit")
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    assert any("bot" in p and "now has no sessions" in p for p in prompts)
    assert calls == ["bot"]
    assert db.get_agent("bot") is None


def test_grant_all_sessionless_agent_not_touched(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A grant_all agent with no explicit rows and no sessions is standing
    intent and is not a cleanup candidate: not offered, not reported, not
    auto-deleted, across interactive and --yes, created and not-created."""
    _seed_vm(db)
    db.insert_agent("bot", "vm1", "bot-user", grant_all=True)
    calls = _spy_delete_agent(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    for created in (False, True):
        session = _session_snapshot("s", "ws-vm1", agent_name="bot", created_agent=created)
        _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
        _cleanup_now_empty_agent(db, _StubConfig(), session, yes=True, interaction=InteractionPolicy.REFUSE)

    assert prompts == []
    assert calls == []
    assert not captured_output.warnings
    assert db.get_agent("bot") is not None


# ---------------------------------------------------------------------------
# Cross-resource cascade: deleting the now-unused workspace can revoke the
# agent's last grant (FK cascade on agent_workspace_grants) and make the
# agent a candidate in the same run. delete_session runs the two cleanup
# helpers in this exact order, so exercising them in sequence is faithful.
# ---------------------------------------------------------------------------


def test_workspace_delete_cascade_makes_agent_candidate(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """When the deleted session's workspace is the agent's ONLY grant target,
    deleting that workspace cascades the grant away, so the agent (guarded
    beforehand) becomes a candidate and is handled in the same run."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")  # the agent's only grant
    ws_calls = _spy_delete_workspace(db, monkeypatch)  # models delete_workspace's DB effect (FK cascade)
    agent_calls = _spy_delete_agent(db, monkeypatch)
    _record_confirm(monkeypatch, answer=True)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot")
    # Before the workspace delete, the grant guards the agent.
    assert agent_is_unused(db, "bot") is False

    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert ws_calls == ["ws-vm1"]
    # The workspace delete cascaded the only grant away, unguarding the agent.
    assert agent_calls == ["bot"]
    assert db.get_workspace("ws-vm1") is None
    assert db.get_agent("bot") is None


def test_agent_stays_guarded_when_workspace_kept(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """If the operator declines the workspace offer, the grant is never
    revoked, so the still-granted agent stays guarded (not even offered)."""
    _seed_vm(db)
    _seed_agent(db)
    db.insert_agent_grant("bot", "ws-vm1", "explicit")
    ws_calls = _spy_delete_workspace(db, monkeypatch)
    agent_calls = _spy_delete_agent(db, monkeypatch)

    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return "Workspace" not in message  # decline the workspace offer, accept any other

    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    session = _session_snapshot("s", "ws-vm1", agent_name="bot")
    _cleanup_now_empty_workspace(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)
    _cleanup_now_empty_agent(db, _StubConfig(), session, yes=False, interaction=InteractionPolicy.REFUSE)

    assert ws_calls == []
    assert db.get_workspace("ws-vm1") is not None
    # Grant intact -> agent still guarded -> no agent offer at all.
    assert not any("Agent 'bot'" in p for p in prompts)
    assert agent_calls == []
    assert db.get_agent("bot") is not None


# ---------------------------------------------------------------------------
# Integration: the wiring from delete_session into the cleanup (admin session,
# so only the workspace path fires; agent SSH machinery stays out of scope).
# ---------------------------------------------------------------------------


def _prep_delete_session(db: Database, monkeypatch: pytest.MonkeyPatch, *, created_workspace: bool = False) -> None:
    """Seed a single STOPPED admin session on ws-vm1 and stub the tmux /
    console side effects so ``delete_session`` reaches the #266 cleanup."""
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s"])
    db.update_session_pid("s", PID_STOPPED)
    if created_workspace:
        db._conn.execute("UPDATE sessions SET created_workspace = 1 WHERE name = 's'")
        db._conn.commit()
    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)


def test_delete_session_interactive_offers_now_empty_workspace(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """End to end: deleting the last admin session offers its now-empty
    workspace and an accepted offer deletes it."""
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch)
    calls = _spy_delete_workspace(db, monkeypatch)
    prompts = _record_confirm(monkeypatch, answer=True)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=InteractionPolicy.REFUSE)

    assert db.get_session("s") is None
    assert any("ws-vm1" in p and "now has no sessions" in p for p in prompts)
    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None


def test_delete_session_yes_reports_now_empty_uncreated_workspace(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """End to end under --yes: a now-empty workspace this session did not
    create is reported but kept."""
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch, created_workspace=False)
    calls = _spy_delete_workspace(db, monkeypatch)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=InteractionPolicy.REFUSE)

    assert db.get_session("s") is None
    assert calls == []
    assert db.get_workspace("ws-vm1") is not None
    assert any(
        "ws-vm1" in w and "now has no sessions" in w and "agw workspace delete ws-vm1" in w
        for w in captured_output.warnings
    )


def test_delete_session_yes_auto_deletes_created_workspace(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """End to end under --yes: a workspace created with this session is
    auto-deleted (the pre-#266 behavior, preserved)."""
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch, created_workspace=True)
    calls = _spy_delete_workspace(db, monkeypatch)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=InteractionPolicy.REFUSE)

    assert calls == ["ws-vm1"]
    assert db.get_workspace("ws-vm1") is None


def test_delete_session_warns_when_now_empty_workspace_delete_raises(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """If the confirmed workspace delete raises AgentworksError, the session
    is still reported deleted and the command completes without propagating."""
    from agentworks.sessions import manager as manager_mod

    _prep_delete_session(db, monkeypatch)

    def _boom(*a: object, **k: object) -> None:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", _boom)
    _record_confirm(monkeypatch, answer=True)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=InteractionPolicy.REFUSE)

    assert db.get_session("s") is None
    assert "Session 's' deleted" in captured_output.info
    assert any("Could not delete empty workspace 'ws-vm1'" in w for w in captured_output.warnings)


def test_non_interactive_without_yes_reports_and_never_prompts_or_deletes(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Without a TTY and without --yes, the helper degrades to the
    report-but-keep path for created and non-created resources alike:
    confirm() would EOF into UserAbort after the primary command already
    mutated state, and auto-delete strictly requires --yes. Pinned for the
    shared helper so every consumer (session delete's workspace/agent/console
    cascade, and #287's remove-sessions) inherits the scripted-caller safety."""
    from agentworks import output as output_mod
    from agentworks.sessions._resource_cleanup import cleanup_now_empty_resource

    monkeypatch.setattr(output_mod, "is_interactive", lambda: False)
    confirms: list[str] = []
    monkeypatch.setattr(output_mod, "confirm", lambda msg: confirms.append(msg) or True)
    deletes: list[str] = []

    for created in (False, True):
        cleanup_now_empty_resource(
            kind="workspace",
            name=f"ws-{created}",
            created=created,
            delete=functools.partial(deletes.append, f"ws-{created}"),
            manual_command=f"agw workspace delete ws-{created}",
            yes=False,
            empty_clause="now has no sessions",
            report_clause="now has no sessions",
        )

    assert confirms == []
    assert deletes == []
    warnings = "\n".join(captured_output.warnings)
    assert "ws-False" in warnings and "ws-True" in warnings
    assert "delete it with" in warnings
