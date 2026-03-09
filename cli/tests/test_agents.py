"""Tests for agent manager."""

from __future__ import annotations

import pytest

from agentworks.agents.manager import derive_linux_user


@pytest.mark.parametrize("workspace,agent,expected", [
    ("ws-1", "coder", "ws-1--coder"),
    ("myproject", "reviewer", "myproject--reviewer"),
    ("dev", "a", "dev--a"),
])
def test_derive_linux_user(workspace: str, agent: str, expected: str) -> None:
    assert derive_linux_user(workspace, agent) == expected
