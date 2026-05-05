"""Tests for session liveness checking functions."""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentworks.db import SessionRow, SessionStatus
from agentworks.sessions.manager import (
    batch_check_status,
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
    name: str,
    pid: int | None = None,
    socket_path: str | None = None,
    mode: str = "admin",
    boot_id: str | None = None,
) -> SessionRow:
    return SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode=mode,
        created_at="",
        updated_at="",
        pid=pid,
        socket_path=socket_path,
        boot_id=boot_id,
    )


# -- batch_check_status -----------------------------------------------------


def test_batch_mixed() -> None:
    """Batch: agent OK, admin stopped, NULL-PID excluded."""
    sessions = [
        _session("a1", pid=100, socket_path="/sock1", mode="agent", boot_id="boot1"),
        _session("s1", pid=200, mode="admin"),
        _session("s2", pid=None),
    ]
    target = _FakeTarget(
        {
            "has-session": _FakeResult(
                ok=True,
                stdout="S:a1:0\nS:s1:1\n",
            ),
        }
    )
    result = batch_check_status(sessions, target=target)
    assert result["a1"] == SessionStatus.OK
    assert result["s1"] == SessionStatus.STOPPED
    assert "s2" not in result


def test_batch_empty() -> None:
    assert batch_check_status([], target=_FakeTarget()) == {}


def test_batch_all_missing_pid() -> None:
    sessions = [_session("s1", pid=None)]
    assert batch_check_status(sessions, target=_FakeTarget()) == {}


def test_batch_builds_compound_command() -> None:
    """Compound command includes has-session for both agent and admin sessions."""
    sessions = [
        _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id="b"),
        _session("s1", pid=200, mode="admin"),
    ]
    target = _FakeTarget(
        {"has-session": _FakeResult(ok=True, stdout="S:a1:0\nS:s1:0\n")}
    )
    batch_check_status(sessions, target=target)
    assert len(target.commands) == 1
    assert "has-session" in target.commands[0]


# -- check_session_status ---------------------------------------------------

BOOT_CURRENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BOOT_STALE = "11111111-2222-3333-4444-555555555555"


def test_agent_ok() -> None:
    """Agent session: has-session succeeds -> OK."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert check_session_status(session, target=target) == SessionStatus.OK


def test_agent_stopped_pid_dead() -> None:
    """Agent session: has-session fails, PID dead -> STOPPED."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({
        "has-session": _FakeResult(ok=False),
        "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
        "test -d /proc/42": _FakeResult(ok=False),
    })
    assert check_session_status(session, target=target) == SessionStatus.STOPPED


def test_agent_stopped_stale_boot() -> None:
    """Agent session: has-session fails, stale boot -> STOPPED (no PID check)."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_STALE)
    target = _FakeTarget({
        "has-session": _FakeResult(ok=False),
        "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
    })
    assert check_session_status(session, target=target) == SessionStatus.STOPPED
    # PID should NOT be checked (stale boot short-circuits)
    assert not any("test -d /proc" in cmd for cmd in target.commands)


def test_agent_broken() -> None:
    """Agent session: has-session fails, same boot, PID alive -> BROKEN."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({
        "has-session": _FakeResult(ok=False),
        "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
        "test -d /proc/42": _FakeResult(ok=True),
    })
    assert check_session_status(session, target=target) == SessionStatus.BROKEN


def test_admin_ok() -> None:
    """Admin session: has-session succeeds -> OK."""
    session = _session("s1", pid=42, boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert check_session_status(session, target=target) == SessionStatus.OK


def test_admin_stopped() -> None:
    """Admin session: has-session fails -> STOPPED (no PID follow-up)."""
    session = _session("s1", pid=42, boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=False)})
    assert check_session_status(session, target=target) == SessionStatus.STOPPED


def test_unknown_no_pid() -> None:
    session = _session("s1", pid=None)
    assert check_session_status(session, target=_FakeTarget()) == SessionStatus.UNKNOWN


def test_unknown_no_boot_id() -> None:
    """PID present but boot_id missing -> UNKNOWN (triggers auto-repair)."""
    session = _session("s1", pid=42, boot_id=None)
    assert check_session_status(session, target=_FakeTarget()) == SessionStatus.UNKNOWN


def test_stopped_pid_sentinel() -> None:
    from agentworks.db import PID_STOPPED

    session = _session("s1", pid=PID_STOPPED)
    assert check_session_status(session, target=_FakeTarget()) == SessionStatus.STOPPED


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
    target = _FakeTarget({"test -d /proc/42": _FakeResult(ok=False)})
    assert force_kill_tmux_server(42, target=target) is True
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
            if "test -d /proc/42" in command:
                call_count += 1
                # First check: alive (needs SIGKILL). Second: dead.
                return _FakeResult(ok=(call_count == 1))
            return _FakeResult(ok=True)

    target = _EscalationTarget()
    assert force_kill_tmux_server(42, target=target) is True
    assert any("kill -9 42" in cmd for cmd in target.commands)


def test_force_kill_cleans_socket(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    target = _FakeTarget({"test -d /proc/42": _FakeResult(ok=False)})
    from agentworks.sessions.tmux import AGENT_SOCKET_ROOT

    sock = f"{AGENT_SOCKET_ROOT}/agt--test/test.sock"
    force_kill_tmux_server(42, target=target, socket_path=sock)
    assert any("rm -f" in cmd and "test.sock" in cmd for cmd in target.commands)


# -- batch unknown detection ------------------------------------------------


def test_batch_status_pid_stopped_not_unknown() -> None:
    """PID_STOPPED sessions should NOT appear in batch status_map (by design)
    and should NOT be treated as unknown by batch commands."""
    from agentworks.db import PID_STOPPED

    sessions = [
        _session("ok1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
        _session("stopped1", pid=PID_STOPPED, boot_id=BOOT_CURRENT),
    ]
    target = _FakeTarget({"has-session": _FakeResult(ok=True, stdout="S:ok1:0\n")})
    result = batch_check_status(sessions, target=target)

    # ok1 should be in the map, stopped1 should NOT (excluded by design)
    assert result["ok1"] == SessionStatus.OK
    assert "stopped1" not in result

    # The unknown detection logic should NOT flag stopped1:
    # s.pid == PID_STOPPED -> skip
    unknown = [
        s for s in sessions
        if s.pid != PID_STOPPED
        and (s.pid is None or s.boot_id is None or s.name not in result)
    ]
    assert unknown == []


# -- _check_dedicated_agent_session edge cases ------------------------------


def test_agent_unknown_when_boot_id_unreadable() -> None:
    """If boot_id can't be read, return UNKNOWN (don't offer --force on unverified PID)."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({
        "has-session": _FakeResult(ok=False),
        "boot_id": _FakeResult(ok=False, stdout=""),
    })
    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


# -- batch_check_status edge cases ------------------------------------------


def test_batch_empty_boot_id_omits_from_map() -> None:
    """If boot_id read fails in compound command, session is omitted from status_map."""
    sessions = [
        _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
    ]
    # Agent failure with empty boot_id field
    target = _FakeTarget({
        "has-session": _FakeResult(ok=True, stdout="S:a1:1::0\n"),
    })
    result = batch_check_status(sessions, target=target)
    assert "a1" not in result  # omitted, not misclassified


# -- _ensure_pid strict gate ------------------------------------------------


def test_ensure_pid_raises_on_unresolvable() -> None:
    """_ensure_pid raises SessionError when PID/boot_id can't be recovered."""
    import pytest

    from agentworks.sessions.manager import _ensure_pid

    session = _session("s1", pid=None, socket_path="/sock", mode="agent")

    class _FailTarget:
        def run(self, command, *, check=True, sudo=False, tty=None, timeout=None):
            # has-session succeeds but display-message fails -> can't recover PID
            if "has-session" in command:
                return _FakeResult(ok=True)
            if "display-message" in command:
                return _FakeResult(ok=False, stdout="")
            return _FakeResult(ok=True)

    class _FakeDb:
        def get_session(self, name):
            return session

    with pytest.raises(Exception, match="alive but PID/boot ID recovery failed"):
        _ensure_pid(session, target=_FailTarget(), db=_FakeDb())
