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
from agentworks.errors import ValidationError


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
    with pytest.raises(ValidationError, match="needs at least one workspace name"):
        grant_workspaces(
            db,
            config=None,  # type: ignore[arg-type]
            agent_name="any-agent",
            workspace_names=[],
            grant_all=False,
        )


def test_revoke_workspaces_rejects_empty_request(db: Database) -> None:
    with pytest.raises(ValidationError, match="needs at least one workspace name"):
        revoke_workspaces(
            db,
            config=None,  # type: ignore[arg-type]
            agent_name="any-agent",
            workspace_names=[],
            revoke_all=False,
        )


# --------------------------------------------------------------------------
# _assert_agent_ssh_works (Option C: pre-rollout-agent guard)
# --------------------------------------------------------------------------


def test_assert_agent_ssh_works_succeeds_when_probe_ok() -> None:
    """When `true` returns exit 0 over SSH, the helper is a no-op."""
    from unittest.mock import MagicMock

    from agentworks.agents.manager import _assert_agent_ssh_works

    target = MagicMock()
    probe_result = MagicMock()
    probe_result.ok = True
    probe_result.returncode = 0
    target.run.return_value = probe_result

    agent = MagicMock()
    agent.name = "claude"
    agent.linux_user = "claude"

    # Does not raise.
    _assert_agent_ssh_works(target, agent)
    # Probe was issued.
    target.run.assert_called_once()
    cmd = target.run.call_args[0][0]
    assert cmd == "true"


def test_assert_agent_ssh_works_raises_on_ssh_transport_failure() -> None:
    """Exit code 255 (SSH transport / auth failure) raises StateError with reinit hint."""
    from unittest.mock import MagicMock

    import pytest as _pt

    from agentworks.agents.manager import _assert_agent_ssh_works
    from agentworks.errors import StateError

    target = MagicMock()
    probe_result = MagicMock()
    probe_result.ok = False
    probe_result.returncode = 255
    target.run.return_value = probe_result

    agent = MagicMock()
    agent.name = "claude"
    agent.linux_user = "claude"

    with _pt.raises(StateError) as exc_info:
        _assert_agent_ssh_works(target, agent)
    assert "rejected" in str(exc_info.value).lower()
    # Hint mentions the reinit command for this specific agent.
    assert exc_info.value.hint is not None
    assert "agent reinit claude" in exc_info.value.hint


def test_assert_agent_ssh_works_passes_through_non_transport_failures() -> None:
    """Probe failures with non-255 exit are NOT treated as auth issues.

    A failure where `true` exited non-zero (impossible in practice, but
    defensive) should not get mis-attributed to a pre-rollout agent.
    """
    from unittest.mock import MagicMock

    from agentworks.agents.manager import _assert_agent_ssh_works

    target = MagicMock()
    probe_result = MagicMock()
    probe_result.ok = False
    probe_result.returncode = 1  # not 255
    target.run.return_value = probe_result

    agent = MagicMock()
    agent.name = "claude"
    agent.linux_user = "claude"

    # No StateError raised; the helper returns quietly. The caller is
    # responsible for handling whatever else went wrong.
    _assert_agent_ssh_works(target, agent)
