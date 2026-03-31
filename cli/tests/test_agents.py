"""Tests for agent manager."""

from __future__ import annotations

import pytest

from agentworks.agents.manager import derive_linux_user, workspace_group


@pytest.mark.parametrize("agent,expected", [
    ("coder", "agt--coder"),
    ("reviewer", "agt--reviewer"),
    ("a", "agt--a"),
])
def test_derive_linux_user(agent: str, expected: str) -> None:
    assert derive_linux_user(agent) == expected


@pytest.mark.parametrize("ws_name,expected", [
    ("myproject", "ws--myproject"),
    ("dev", "ws--dev"),
])
def test_workspace_group(ws_name: str, expected: str) -> None:
    assert workspace_group(ws_name) == expected
