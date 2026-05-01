"""Tests for batch_check_sessions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.sessions.tmux import BatchCheckError, batch_check_sessions
from agentworks.ssh import SSHError, SSHResult


def _mock_target(stdout: str = "", returncode: int = 0, raise_ssh: bool = False) -> MagicMock:
    target = MagicMock()
    if raise_ssh:
        target.run.side_effect = SSHError("connection refused")
    else:
        target.run.return_value = SSHResult(returncode=returncode, stdout=stdout, stderr="")
    return target


def test_empty_checks() -> None:
    target = _mock_target()
    assert batch_check_sessions(target, []) == {}
    target.run.assert_not_called()


def test_all_alive() -> None:
    target = _mock_target(stdout="ALIVE:session1\nALIVE:session2\nDONE\n")
    result = batch_check_sessions(target, [("session1", None), ("session2", "/path/to/sock")])
    assert result == {"session1": True, "session2": True}


def test_mixed_alive_and_dead() -> None:
    target = _mock_target(stdout="ALIVE:s1\nDONE\n")
    result = batch_check_sessions(target, [("s1", None), ("s2", "/sock")])
    assert result == {"s1": True, "s2": False}


def test_all_dead() -> None:
    target = _mock_target(stdout="DONE\n")
    result = batch_check_sessions(target, [("s1", None), ("s2", "/sock")])
    assert result == {"s1": False, "s2": False}


def test_ssh_failure_raises_batch_error() -> None:
    target = _mock_target(raise_ssh=True)
    with pytest.raises(BatchCheckError, match="SSH failed"):
        batch_check_sessions(target, [("s1", None)])


def test_missing_sentinel_raises_batch_error() -> None:
    target = _mock_target(stdout="ALIVE:s1\n")  # no DONE
    with pytest.raises(BatchCheckError, match="did not complete"):
        batch_check_sessions(target, [("s1", None)])


def test_socket_permission_error_warns(warnings: list[str]) -> None:
    """Socket exists but not readable -> ERROR marker -> warn + reinit hint."""
    target = _mock_target(stdout="ERROR:agent-session\nDONE\n")
    result = batch_check_sessions(target, [("agent-session", "/bad/socket")])
    assert result == {"agent-session": False}
    assert any("socket not accessible" in w for w in warnings)


def test_missing_socket_is_dead_not_error(warnings: list[str]) -> None:
    """Missing socket is normal (session stopped + cleaned up), no warning."""
    target = _mock_target(stdout="DONE\n")
    result = batch_check_sessions(target, [("stopped-session", "/missing/socket")])
    assert result == {"stopped-session": False}
    assert len(warnings) == 0


def test_command_includes_socket_path() -> None:
    target = _mock_target(stdout="DONE\n")
    batch_check_sessions(target, [("s1", "/run/agent/sock")])
    cmd = target.run.call_args[0][0]
    assert "/run/agent/sock" in cmd
    assert "tmux -S" in cmd


def test_command_default_server_no_socket() -> None:
    target = _mock_target(stdout="DONE\n")
    batch_check_sessions(target, [("s1", None)])
    cmd = target.run.call_args[0][0]
    assert "-S" not in cmd
    assert "has-session" in cmd
