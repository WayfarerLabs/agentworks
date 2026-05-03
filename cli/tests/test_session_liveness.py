"""Tests for session liveness checking functions."""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentworks.db import SessionHealth, SessionRow
from agentworks.sessions.manager import (
    batch_check_status,
    check_session_health,
    check_session_status,
)
from agentworks.sessions.tmux import (
    force_kill_tmux_server,
    get_tmux_server_pid,
)


@dataclass
class _FakeResult:
    stdout: str = ""
    ok: bool = True


class _FakeTarget:
    """Fake ExecTarget that returns canned responses keyed by substring match."""

    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self._responses = responses or {}
        self.commands: list[str] = []

    def run(
        self,
        command: str,
        *,
        check: bool = True,
        sudo: bool = False,
        tty: bool | None = None,
        timeout: int | None = None,
    ) -> _FakeResult:
        self.commands.append(command)
        for pattern, result in self._responses.items():
            if pattern in command:
                return result
        return _FakeResult()


def _session(
    name: str, pid: int | None = None, socket_path: str | None = None
) -> SessionRow:
    return SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode="admin",
        created_at="",
        updated_at="",
        pid=pid,
        socket_path=socket_path,
    )


# -- check_session_status ---------------------------------------------------


def test_check_session_status_alive() -> None:
    target = _FakeTarget({"kill -0 42": _FakeResult(ok=True)})
    assert check_session_status(42, target=target) is True


def test_check_session_status_dead() -> None:
    target = _FakeTarget({"kill -0 99": _FakeResult(ok=False)})
    assert check_session_status(99, target=target) is False


# -- batch_check_status -----------------------------------------------------


def test_batch_check_status_mixed() -> None:
    sessions = [
        _session("s1", pid=100),
        _session("s2", pid=200),
        _session("s3", pid=None),
    ]
    target = _FakeTarget(
        {
            "kill -0": _FakeResult(
                ok=True,
                stdout="STATUS:s1:0\nSTATUS:s2:1\n",
            ),
        }
    )
    result = batch_check_status(sessions, target=target)
    assert result == {"s1": True, "s2": False}
    assert "s3" not in result


def test_batch_check_status_empty() -> None:
    assert batch_check_status([], target=_FakeTarget()) == {}


def test_batch_check_status_all_missing_pid() -> None:
    sessions = [_session("s1", pid=None)]
    assert batch_check_status(sessions, target=_FakeTarget()) == {}


def test_batch_check_status_builds_compound_command() -> None:
    sessions = [_session("s1", pid=100), _session("s2", pid=200)]
    target = _FakeTarget(
        {"kill -0": _FakeResult(ok=True, stdout="STATUS:s1:0\nSTATUS:s2:0\n")}
    )
    batch_check_status(sessions, target=target)
    assert len(target.commands) == 1
    assert "kill -0 100" in target.commands[0]
    assert "kill -0 200" in target.commands[0]


# -- check_session_health ---------------------------------------------------


def test_health_ok() -> None:
    session = _session("s1", pid=42, socket_path="/sock")
    target = _FakeTarget(
        {
            "kill -0 42": _FakeResult(ok=True),
            "has-session": _FakeResult(ok=True),
        }
    )
    assert check_session_health(session, target=target) == SessionHealth.OK


def test_health_stopped() -> None:
    session = _session("s1", pid=42)
    target = _FakeTarget({"kill -0 42": _FakeResult(ok=False)})
    assert check_session_health(session, target=target) == SessionHealth.STOPPED


def test_health_broken() -> None:
    session = _session("s1", pid=42, socket_path="/sock")
    target = _FakeTarget(
        {
            "kill -0 42": _FakeResult(ok=True),
            "has-session": _FakeResult(ok=False),
        }
    )
    assert check_session_health(session, target=target) == SessionHealth.BROKEN


def test_health_unknown_no_pid() -> None:
    session = _session("s1", pid=None)
    assert check_session_health(session, target=_FakeTarget()) == SessionHealth.UNKNOWN


# -- get_tmux_server_pid ----------------------------------------------------


def test_get_pid_success() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="12345\n")})
    assert get_tmux_server_pid(target=target) == 12345


def test_get_pid_not_running() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=False)})
    assert get_tmux_server_pid(target=target) is None


def test_get_pid_with_socket() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="99999\n")})
    result = get_tmux_server_pid(target=target, socket_path="/run/test.sock")
    assert result == 99999
    assert "-S /run/test.sock" in target.commands[0]


# -- force_kill_tmux_server -------------------------------------------------


def test_force_kill_sigterm_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    target = _FakeTarget({"kill -0": _FakeResult(ok=False)})
    assert force_kill_tmux_server(42, target=target) is True
    # No SIGKILL sent (process died after SIGTERM)
    assert not any("kill -9" in cmd for cmd in target.commands)


def test_force_kill_escalates_to_sigkill(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    call_count = 0

    class _EscalationTarget:
        def __init__(self):
            self.commands: list[str] = []

        def run(self, command, *, check=True, sudo=False, tty=None, timeout=None):
            nonlocal call_count
            self.commands.append(command)
            if "kill -0 42" in command:
                call_count += 1
                # First check: alive (needs SIGKILL). Second: dead.
                return _FakeResult(ok=(call_count == 1))
            return _FakeResult(ok=True)

    target = _EscalationTarget()
    assert force_kill_tmux_server(42, target=target) is True
    assert any("kill -9 42" in cmd for cmd in target.commands)


def test_force_kill_cleans_socket(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    target = _FakeTarget({"kill -0": _FakeResult(ok=False)})
    force_kill_tmux_server(42, target=target, socket_path="/run/test.sock")
    assert any("rm -f" in cmd and "test.sock" in cmd for cmd in target.commands)
