"""The batch status script against a real tmux server, run as root over a server the admin owns.

The batch observation runs its whole script elevated, so it rests on root
being allowed to connect to an admin-owned tmux server (tmux 3.3's
``server-access`` lets the owner and root in). This runs the script exactly as
the probe sends it, under ``sudo -n``, against a server this test's user
started: the sudo lane, required where passwordless sudo is granted (CI) and
skipped elsewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworks.db import SessionMode, SessionRow, SessionStatus
from agentworks.execution.requests import Bytes, Script
from agentworks.execution.results import Completed, Result
from agentworks.sessions.manager._status import batch_check_status
from tests.execution.conformance.lanes import sudo_granted, unavailable
from tests.vm_access_support import Admin, Call, RecordingVMAccess

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("tmux") is None, reason="runs a real tmux server on Linux"
)

SESSION = "s1"
BOOT_ID = "11111111-2222-3333-4444-555555555555"


def as_root(call: Call) -> Result:
    """Run an elevated script as root with ``sudo -n /bin/sh -c``, its input on stdin, as the admin's access does."""
    request = call.request
    assert isinstance(request, Script) and request.elevated
    completed = subprocess.run(
        ["sudo", "-n", "/bin/sh", "-c", request.source],
        input=request.stdin.data if isinstance(request.stdin, Bytes) else b"",
        capture_output=True,
        timeout=30,
        check=False,
    )
    return Result(Completed(completed.returncode), completed.stdout, completed.stderr)


def root_admin() -> RecordingVMAccess:
    """The admin's access, its elevated requests run as root here."""
    access = RecordingVMAccess([], Admin())
    access.answer = as_root
    return access


@pytest.fixture
def server() -> Iterator[str]:
    """A tmux server this test's user owns, with one session; yields its socket path."""
    if not sudo_granted():
        unavailable("the batch status script runs as root, which needs passwordless sudo")
    environment = {key: value for key, value in os.environ.items() if key != "TMUX"}
    # pytest's nested tmp_path is longer than sun_path on some hosts.
    with tempfile.TemporaryDirectory(prefix="agw-batch-root-", dir="/tmp") as directory:
        socket = str(Path(directory) / "s.sock")
        tmux = ["tmux", "-f", "/dev/null", "-S", socket]
        subprocess.run([*tmux, "new-session", "-d", "-s", SESSION, "sleep 600"], check=True, env=environment)
        try:
            yield socket
        finally:
            subprocess.run([*tmux, "kill-server"], check=False, env=environment, capture_output=True)


def row(socket: str, *, name: str = SESSION, pid: int | None = 4242) -> SessionRow:
    return SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode=SessionMode.ADMIN.value,
        agent_name=None,
        pid=pid,
        boot_id=BOOT_ID,
        socket_path=socket,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_root_reads_a_server_the_admin_owns(server: str) -> None:
    statuses = batch_check_status([row(server)], admin=root_admin())

    assert statuses == {SESSION: SessionStatus.RUNNING}


def test_root_reads_a_missing_session_on_that_server_as_its_absence(server: str) -> None:
    statuses = batch_check_status([row(server, name="gone")], admin=root_admin())

    assert statuses == {"gone": SessionStatus.RESIDUAL}
