"""``stream`` in a real process: the operator's stdin stays open, the guest exits, and the interpreter exits cleanly.

The pump copies stdin from a daemon thread. A thread blocked in a buffered
read aborts the interpreter at exit (status 134), so the call takes the raw
stream. That only shows in a separate process whose stdin is still open when
the command finishes.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.windows

CHILD = r"""
import getpass, sys
from pathlib import Path
from agentworks.execution import client, ssh_process
from agentworks.execution.access import Admin, VMAccess
from agentworks.execution.client import SSHClient
from agentworks.execution.requests import Command

# A local shell stands in for the account shell, and python3 for the guest.
client.resolve_ssh = lambda: SSHClient("/nonexistent/ssh", (9, 9))
ssh_process.build_argv = lambda ssh, policy, destination, mode, command, *a, **k: [
    "/bin/sh", "-c", command.replace(ssh_process.DEFAULT_PYTHON, sys.executable, 1),
]
access = VMAccess(
    vm_name="local", host="127.0.0.1", user=getpass.getuser(),
    identity_file=Path("/nonexistent"), identity=Admin(),
)
status = access.stream(
    Command(("sh", "-c", "echo out; echo err >&2; exit 3"), deadline=30),
    stdin=sys.stdin.buffer.raw, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
)
sys.stdout.flush()
sys.exit(status)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in account shell is /bin/sh")
def test_the_guest_exits_while_stdin_is_still_open_and_the_process_exits_cleanly() -> None:
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        # Never close stdin: the daemon thread is blocked reading it when the command finishes.
        child.wait(timeout=30)
        assert child.stdout is not None and child.stderr is not None
        stdout, stderr = child.stdout.read(), child.stderr.read()
    finally:
        child.kill()
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()

    assert child.returncode == 3, stderr  # not -6 (the interpreter aborting at exit)
    assert stdout == b"out\n"
    assert stderr == b"err\n"
