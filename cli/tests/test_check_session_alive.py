"""Tests for check_session_alive (sudo fallback for single-session checks)."""

from __future__ import annotations

from unittest.mock import MagicMock

from agentworks.sessions.tmux import SessionState, check_session_alive
from agentworks.ssh import SSHResult


def _ok() -> SSHResult:
    return SSHResult(returncode=0, stdout="", stderr="")


def _fail() -> SSHResult:
    return SSHResult(returncode=1, stdout="", stderr="")


def test_alive_without_sudo() -> None:
    """Normal case: non-sudo check succeeds, no sudo needed."""
    target = MagicMock()
    target.run.return_value = _ok()
    assert check_session_alive(target, "s1", "/sock") == SessionState.ALIVE
    assert target.run.call_count == 1
    assert "sudo" not in target.run.call_args[0][0]


def test_dead_no_socket() -> None:
    """Default-server session, not alive. No sudo fallback."""
    target = MagicMock()
    target.run.return_value = _fail()
    assert check_session_alive(target, "s1") == SessionState.DEAD
    assert target.run.call_count == 1


def test_dead_with_socket() -> None:
    """Socket session, both non-sudo and sudo fail -> actually dead."""
    target = MagicMock()
    target.run.side_effect = [_fail(), _fail()]
    assert check_session_alive(target, "s1", "/sock") == SessionState.DEAD
    assert target.run.call_count == 2


def test_inaccessible_with_socket() -> None:
    """Non-sudo fails, sudo succeeds -> INACCESSIBLE."""
    target = MagicMock()
    target.run.side_effect = [_fail(), _ok()]
    assert check_session_alive(target, "s1", "/sock") == SessionState.INACCESSIBLE
    assert target.run.call_count == 2
    assert "sudo" in target.run.call_args_list[1][0][0]


def test_no_sudo_fallback_for_default_server() -> None:
    """Default-server sessions (no socket) should not try sudo."""
    target = MagicMock()
    target.run.return_value = _fail()
    assert check_session_alive(target, "s1", None) == SessionState.DEAD
    assert target.run.call_count == 1
