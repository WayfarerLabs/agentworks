"""Stuck tmux servers leave only their own rows unknown in batch status.

The real batch program runs in a local shell under the request timeout that
``batch_check_status`` asks for, with fake ``tmux`` and ``sudo`` on its PATH.
The timeouts are shortened so the test stays quick; their ratios are the
production ones.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import SessionMode, SessionRow, SessionStatus
from agentworks.errors import ConnectivityError
from agentworks.sessions.manager import SessionTmuxServer, _status
from tests.conftest import requires_posix_shell, requires_symlinks

if TYPE_CHECKING:
    from pathlib import Path

BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADMIN_SOCKETS = "/run/agentworks/admin-tmux-sockets/admin"
OWNER = "agt-owner"
AGENT_SOCKETS = f"/run/agentworks/agent-tmux-sockets/{OWNER}"


class _LocalGuest:
    """Runs a request in a local shell and fails it, like SSH, when it outlives its timeout."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def run(self, command: str, *, timeout: int, input_data: str, **kwargs: Any) -> SimpleNamespace:
        process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ConnectivityError("batch status request timed out") from None
        return SimpleNamespace(returncode=process.returncode, ok=process.returncode == 0, stdout=stdout, stderr=stderr)


def _row(name: str, *, agent: bool = False) -> tuple[SessionRow, SessionTmuxServer]:
    socket_path = f"{AGENT_SOCKETS if agent else ADMIN_SOCKETS}/{name}.sock"
    session = SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode=(SessionMode.AGENT if agent else SessionMode.ADMIN).value,
        created_at="",
        updated_at="",
        pid=os.getpid(),
        socket_path=socket_path,
        boot_id=BOOT,
    )
    return session, SessionTmuxServer(socket_path, OWNER if agent else None)


@requires_symlinks
@requires_posix_shell
def test_stuck_servers_leave_only_their_own_rows_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_status, "_BATCH_CLIENT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(_status, "GUEST_OBSERVATION_TIMEOUT_SECONDS", 2)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "od", "sleep", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    audit = tmp_path / "audit"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\ncase "$2" in *stuck*) exec sleep 30 ;; esac\nexit 0\n'
    )
    tmux.chmod(0o755)
    # Root's switch to the owner, reduced to running the client.
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\nshift\n[ "$1" = sh ] || shift 4\nexec "$@"\n')
    sudo.chmod(0o755)
    rows = [
        _row("stuck-1"),
        _row("fine-1"),
        _row("stuck-2", agent=True),
        _row("fine-2", agent=True),
        _row("stuck-3"),
    ]
    guest = _LocalGuest({"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path), "AGW_PROBE_AUDIT": str(audit)})

    statuses = _status.batch_check_status(rows, target=guest)  # type: ignore[arg-type]

    assert statuses == {
        "stuck-1": SessionStatus.UNKNOWN,
        "fine-1": SessionStatus.RUNNING,
        "stuck-2": SessionStatus.UNKNOWN,
        "fine-2": SessionStatus.RUNNING,
        "stuck-3": SessionStatus.UNKNOWN,
    }
    # A server that did not answer has-session gets no second client.
    assert [line for line in audit.read_text().splitlines() if "stuck" in line and "list-sessions" in line] == []
