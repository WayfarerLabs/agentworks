"""Tests for agent manager."""

from __future__ import annotations

import pytest

from agentworks.agents.manager import (
    derive_linux_user,
    grant_workspaces,
    revoke_workspaces,
    workspace_group,
)
from agentworks.db import Database
from agentworks.output import AgentError


@pytest.mark.parametrize(
    "agent,expected",
    [
        ("coder", "agt-coder"),
        ("reviewer", "agt-reviewer"),
        ("a", "agt-a"),
    ],
)
def test_derive_linux_user(agent: str, expected: str) -> None:
    assert derive_linux_user(agent) == expected


@pytest.mark.parametrize(
    "ws_name,expected",
    [
        ("myproject", "ws-myproject"),
        ("dev", "ws-dev"),
    ],
)
def test_workspace_group(ws_name: str, expected: str) -> None:
    assert workspace_group(ws_name) == expected


# -- Empty-args validation -------------------------------------------------
#
# These pin the service-layer contract that grant_workspaces and
# revoke_workspaces refuse no-op calls (no workspaces named, no bulk flag).
# The CLI no longer does this check; the manager does. Validation fires
# before any DB lookup, so no fixture seeding is needed.


def test_grant_workspaces_rejects_empty_request(db: Database) -> None:
    with pytest.raises(AgentError, match="needs at least one workspace name"):
        grant_workspaces(
            db,
            config=None,  # type: ignore[arg-type]
            agent_name="any-agent",
            workspace_names=[],
            grant_all=False,
        )


def test_revoke_workspaces_rejects_empty_request(db: Database) -> None:
    with pytest.raises(AgentError, match="needs at least one workspace name"):
        revoke_workspaces(
            db,
            config=None,  # type: ignore[arg-type]
            agent_name="any-agent",
            workspace_names=[],
            revoke_all=False,
        )
