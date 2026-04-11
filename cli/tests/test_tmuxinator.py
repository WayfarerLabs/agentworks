"""Tests for tmuxinator config generation and tmux socket helpers."""

from __future__ import annotations

from agentworks.db import SessionRow
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT, agent_socket_path
from agentworks.workspaces.tmuxinator import GENERATED_HEADER, console_session_name, generate_config


def test_generate_config_no_sessions() -> None:
    config = generate_config("myproject", "/home/agentworks/workspaces/myproject")
    assert config.startswith(GENERATED_HEADER)
    assert f"name: {console_session_name('myproject')}" in config
    assert "root: /home/agentworks/workspaces/myproject" in config
    assert "  - admin-shell:" in config
    # no session windows
    assert "while tmux has-session" not in config


def test_generate_config_with_sessions() -> None:
    sessions = [
        SessionRow(
            name="ws-1-build",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
        SessionRow(
            name="ws-1-test",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws-1", "/home/agentworks/workspaces/ws-1", sessions=sessions)

    assert "  - admin-shell:" in config
    assert "  - ws-1-build:" in config
    assert "  - ws-1-test:" in config


def test_generate_config_admin_window_first() -> None:
    """Admin shell window should always be first."""
    sessions = [
        SessionRow(
            name="ws-alpha",
            workspace_name="ws",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    lines = config.splitlines()

    admin_idx = next(i for i, line in enumerate(lines) if "- admin-shell:" in line)
    session_idx = next(i for i, line in enumerate(lines) if "- ws-alpha:" in line)
    assert admin_idx < session_idx


def test_agent_socket_path() -> None:
    path = agent_socket_path("agt--alice", "myws-dev")
    assert path == f"{AGENT_SOCKET_ROOT}/agt--alice/myws-dev.sock"


def test_generate_config_agent_session_socket() -> None:
    """Agent-mode sessions should use -S <socket> in wrapper commands."""
    sock = "/run/agentworks/agent-tmux-sockets/agt--alice/ws-dev.sock"
    sessions = [
        SessionRow(
            name="ws-dev",
            workspace_name="ws",
            template="default",
            mode="agent",
            status="running",
            created_at="",
            updated_at="",
            agent_name="alice",
            socket_path=sock,
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    assert f"-S {sock}" in config
    assert "tmux -S" in config


def test_generate_config_admin_session_no_socket() -> None:
    """Admin-mode sessions should not have -S in wrapper commands."""
    sessions = [
        SessionRow(
            name="ws-build",
            workspace_name="ws",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    assert "-S " not in config
