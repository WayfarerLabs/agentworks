"""Tests for tmuxinator config generation and tmux socket helpers."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.db import SessionRow
from agentworks.sessions.tmux import (
    AGENT_SOCKET_GROUP,
    AGENT_SOCKET_ROOT,
    agent_socket_path,
    ensure_agent_socket_dir,
    ensure_agent_socket_root,
)
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

            created_at="",
            updated_at="",
        ),
        SessionRow(
            name="ws-1-test",
            workspace_name="ws-1",
            template="default",
            mode="admin",

            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws-1", "/home/agentworks/workspaces/ws-1", sessions=sessions)

    assert "  - admin-shell:" in config
    assert '  - "ws-1-build":' in config
    assert '  - "ws-1-test":' in config


def test_generate_config_admin_window_first() -> None:
    """Admin shell window should always be first."""
    sessions = [
        SessionRow(
            name="ws-alpha",
            workspace_name="ws",
            template="default",
            mode="admin",

            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    lines = config.splitlines()

    admin_idx = next(i for i, line in enumerate(lines) if "- admin-shell:" in line)
    session_idx = next(i for i, line in enumerate(lines) if '"ws-alpha":' in line)
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

            created_at="",
            updated_at="",
        ),
    ]
    config = generate_config("ws", "/tmp/ws", sessions=sessions)
    assert "-S " not in config


# -- ensure_agent_socket_root / _dir warning behavior ------------------------


@dataclass
class _FakeResult:
    stdout: str = ""
    ok: bool = True


class _FakeTarget:
    """Fake ExecTarget that returns canned output for the first probe call
    (the only call that determines warning behavior) and ignores subsequent
    setup commands. Records whether sudo was requested for each call."""

    def __init__(self, probe_stdout: str) -> None:
        self._probe_stdout = probe_stdout
        self._probe_done = False
        self.commands: list[str] = []
        self.sudo_calls: list[bool] = []

    def run(self, command: str, *, check: bool = True, sudo: bool = False, tty: bool | None = None) -> _FakeResult:
        self.commands.append(command)
        self.sudo_calls.append(sudo)
        # The first call is the probe (contains "if test -d").
        if not self._probe_done and "if test -d" in command:
            self._probe_done = True
            return _FakeResult(stdout=self._probe_stdout)
        return _FakeResult()


def test_ensure_agent_socket_root_missing_warns_by_default(warnings: list[str]) -> None:
    runner = _FakeTarget("MISSING")
    ensure_agent_socket_root(runner, "agentworks")
    assert any("missing" in w for w in warnings)
    # Probe uses sudo; getent does not; all setup commands use sudo
    assert runner.sudo_calls[0] is True  # probe
    assert runner.sudo_calls[1] is False  # getent (no root needed)
    assert all(runner.sudo_calls[2:])  # groupadd/usermod/mkdir/chown/chmod


def test_ensure_agent_socket_root_missing_silent_when_expected(warnings: list[str]) -> None:
    ensure_agent_socket_root(_FakeTarget("MISSING"), "agentworks", warn_if_missing=False)
    assert warnings == []


def test_ensure_agent_socket_root_misconfigured_warns_even_when_missing_suppressed(
    warnings: list[str],
) -> None:
    ensure_agent_socket_root(_FakeTarget("root 755"), "agentworks", warn_if_missing=False)
    assert any("misconfigured" in w for w in warnings)


def test_ensure_agent_socket_root_probe_failed_warns_even_when_missing_suppressed(
    warnings: list[str],
) -> None:
    ensure_agent_socket_root(_FakeTarget("PROBE_FAILED"), "agentworks", warn_if_missing=False)
    assert any("probe failed" in w for w in warnings)


def test_ensure_agent_socket_root_ok_fast_path_no_warning(warnings: list[str]) -> None:
    runner = _FakeTarget(f"{AGENT_SOCKET_GROUP} 2771")
    ensure_agent_socket_root(runner, "agentworks")
    assert warnings == []
    # Fast path: probe + group membership check only (no full setup)
    assert len(runner.commands) == 2
    # Both calls must use sudo (probe needs root for stat, usermod needs root)
    assert all(runner.sudo_calls)


def test_ensure_agent_socket_dir_missing_warns_by_default(warnings: list[str]) -> None:
    runner = _FakeTarget("MISSING")
    ensure_agent_socket_dir(runner, "agt--alice")
    assert any("agt--alice" in w and "missing" in w for w in warnings)
    # All commands (probe + full setup) must use sudo
    assert all(runner.sudo_calls)


def test_ensure_agent_socket_dir_missing_silent_when_expected(warnings: list[str]) -> None:
    ensure_agent_socket_dir(_FakeTarget("MISSING"), "agt--alice", warn_if_missing=False)
    assert warnings == []


def test_ensure_agent_socket_dir_misconfigured_warns_even_when_missing_suppressed(
    warnings: list[str],
) -> None:
    ensure_agent_socket_dir(_FakeTarget("root root 755"), "agt--alice", warn_if_missing=False)
    assert any("misconfigured" in w for w in warnings)


def test_ensure_agent_socket_dir_probe_failed_warns_even_when_missing_suppressed(
    warnings: list[str],
) -> None:
    ensure_agent_socket_dir(_FakeTarget("PROBE_FAILED"), "agt--alice", warn_if_missing=False)
    assert any("probe failed" in w for w in warnings)


def test_ensure_agent_socket_dir_ok_fast_path_no_warning(warnings: list[str]) -> None:
    runner = _FakeTarget(f"agt--alice {AGENT_SOCKET_GROUP} 2770")
    ensure_agent_socket_dir(runner, "agt--alice")
    # Probe must use sudo
    assert runner.sudo_calls[0] is True
    assert warnings == []
    assert len(runner.commands) == 1
