"""Session secrets never leak through the SSH op log or raised error text.

The session env (secret-backed env directives, claude-code's OAuth token)
is embedded in the ``tmux new-session`` command string as ``-e KEY=VAL``
flags (``_tmux_env_flags``); the transport writes that command to the
per-operation ``SSHLogger`` and, on failure, embeds it in the raised
``SSHError``. These tests drive the GENUINE path: the real
``tmux.create_session`` builds the command, a real ``SSHTransport``
carries it (``subprocess.run`` stubbed at the bottom), and a real
``SSHLogger`` with a registered redaction guards both surfaces.

The registration side (the session manager registering every resolved
secret value on the op logger) is pinned in
``tests/sessions/test_claude_code_oauth_orchestrated.py``; the pure
transport contract lives in ``tests/transports/test_ssh.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.sessions.tmux import create_session
from agentworks.ssh import SSHError, SSHLogger
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from pathlib import Path

_TOKEN = "sk-oauth-supersecret-xyz"
_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN, "AGENTWORKS_SESSION": "s1"}


class _Completed:
    """``subprocess.CompletedProcess`` shape for the stub below."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch, *, new_session_rc: int
) -> None:
    """Route the remote command (the ssh argv's last element): no stale
    socket (``test -e`` fails), ``new-session`` exits ``new_session_rc``,
    everything else (socket-root setup, pid probe) succeeds."""

    def _fake_run(args: list[str], **_kwargs: object) -> _Completed:
        remote = args[-1]
        if "test -e" in remote:
            return _Completed(1)
        if "new-session" in remote:
            return _Completed(new_session_rc, stderr="launch failed" if new_session_rc else "")
        return _Completed(0)

    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", _fake_run)


def _transport_with_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SSHTransport, SSHLogger]:
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "session-create")
    logger.add_redaction(_TOKEN)  # what the session manager registers
    return SSHTransport(host="vm1", user="admin", logger=logger), logger


def test_secret_env_never_reaches_the_op_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success path: the tmux launch command (with its ``-e`` env flags)
    is logged, and the registered token value is redacted from it."""
    transport, logger = _transport_with_logger(tmp_path, monkeypatch)
    _stub_subprocess(monkeypatch, new_session_rc=0)

    create_session(
        "s1",
        "/home/admin/ws1",
        "",
        "admin",
        run_command=transport.run,
        target=transport,
        admin_username="admin",
        is_admin=True,
        env=_ENV,
    )

    content = logger.path.read_text()
    assert "new-session" in content  # the launch command WAS logged
    assert _TOKEN not in content
    assert "CLAUDE_CODE_OAUTH_TOKEN=[REDACTED]" in content


def test_secret_env_never_reaches_a_failing_launch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure path: the SSHError for a failing ``tmux new-session``
    embeds the command string, redacted; and the log stays clean too."""
    transport, logger = _transport_with_logger(tmp_path, monkeypatch)
    _stub_subprocess(monkeypatch, new_session_rc=1)

    with pytest.raises(SSHError) as excinfo:
        create_session(
            "s1",
            "/home/admin/ws1",
            "",
            "admin",
            run_command=transport.run,
            target=transport,
            admin_username="admin",
            is_admin=True,
            env=_ENV,
        )

    assert _TOKEN not in str(excinfo.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN=[REDACTED]" in str(excinfo.value)
    assert _TOKEN not in logger.path.read_text()
