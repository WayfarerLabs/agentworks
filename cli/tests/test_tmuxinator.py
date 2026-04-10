"""Tests for tmuxinator config generation and tmux socket helpers."""

from __future__ import annotations

from agentworks.db import TaskRow
from agentworks.tasks.tmux import AGENT_SOCKET_ROOT, agent_socket_path
from agentworks.workspaces.tmuxinator import GENERATED_HEADER, console_session_name, generate_config


def test_generate_config_no_tasks() -> None:
    config = generate_config("myproject", "/home/agentworks/workspaces/myproject")
    assert config.startswith(GENERATED_HEADER)
    assert f"name: {console_session_name('myproject')}" in config
    assert "root: /home/agentworks/workspaces/myproject" in config
    assert "  - admin-shell:" in config
    # no task windows
    assert "while tmux has-session" not in config


def test_generate_config_with_tasks() -> None:
    tasks = [
        TaskRow(
            name="build",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
        TaskRow(
            name="test",
            workspace_name="ws-1",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws-1", "/home/agentworks/workspaces/ws-1", tasks=tasks)

    assert "  - admin-shell:" in config
    assert "  - ws-1--build:" in config
    assert "  - ws-1--test:" in config


def test_generate_config_admin_window_first() -> None:
    """Admin shell window should always be first."""
    tasks = [
        TaskRow(
            name="alpha",
            workspace_name="ws",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", tasks=tasks)
    lines = config.splitlines()

    admin_idx = next(i for i, line in enumerate(lines) if "- admin-shell:" in line)
    task_idx = next(i for i, line in enumerate(lines) if "- ws--alpha:" in line)
    assert admin_idx < task_idx


def test_agent_socket_path() -> None:
    path = agent_socket_path("agt--alice", "myws", "dev")
    assert path == f"{AGENT_SOCKET_ROOT}/agt--alice/myws--dev.sock"


def test_generate_config_agent_task_socket() -> None:
    """Agent-mode tasks should use -S <socket> in wrapper commands."""
    tasks = [
        TaskRow(
            name="dev",
            workspace_name="ws",
            template="default",
            mode="agent",
            status="running",
            created_at="",
            updated_at="",
            agent_name="alice",
        ),
    ]
    sock = "/run/agentworks/agent-tmux-sockets/agt--alice/ws--dev.sock"
    config = generate_config("ws", "/tmp/ws", tasks=tasks, socket_paths={"ws--dev": sock})
    assert f"-S {sock}" in config
    assert "tmux -S" in config


def test_generate_config_admin_task_no_socket() -> None:
    """Admin-mode tasks should not have -S in wrapper commands."""
    tasks = [
        TaskRow(
            name="build",
            workspace_name="ws",
            template="default",
            mode="admin",
            status="running",
            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", tasks=tasks)
    assert "-S " not in config
