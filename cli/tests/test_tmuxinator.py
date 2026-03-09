"""Tests for tmuxinator config generation."""

from __future__ import annotations

from agentworks.db import AgentRow
from agentworks.workspaces.tmuxinator import GENERATED_HEADER, generate_config


def test_generate_config_no_agents() -> None:
    config = generate_config("myproject", "/home/agentworks/workspaces/myproject")
    assert config.startswith(GENERATED_HEADER)
    assert "name: myproject" in config
    assert "root: /home/agentworks/workspaces/myproject" in config
    assert "  - user:" in config
    # no agent windows
    assert "su -" not in config


def test_generate_config_with_agents() -> None:
    agents = [
        AgentRow(name="coder", workspace_name="ws-1", linux_user="ws-1--coder", created_at=""),
        AgentRow(name="reviewer", workspace_name="ws-1", linux_user="ws-1--reviewer", created_at=""),
    ]
    config = generate_config("ws-1", "/home/agentworks/workspaces/ws-1", agents=agents)

    assert "  - user:" in config
    assert "  - coder:" in config
    assert "su - ws-1--coder" in config
    assert "  - reviewer:" in config
    assert "su - ws-1--reviewer" in config


def test_generate_config_user_window_first() -> None:
    """User window should always be first."""
    agents = [
        AgentRow(name="alpha", workspace_name="ws", linux_user="ws--alpha", created_at=""),
    ]
    config = generate_config("ws", "/tmp/ws", agents=agents)
    lines = config.splitlines()

    user_idx = next(i for i, l in enumerate(lines) if "- user:" in l)
    agent_idx = next(i for i, l in enumerate(lines) if "- alpha:" in l)
    assert user_idx < agent_idx
