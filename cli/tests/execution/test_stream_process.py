"""``stream`` in a real process: the operator's stdin stays open, the guest exits, and the interpreter exits cleanly.

The pump copies stdin from a daemon thread. A thread blocked in a buffered
read aborts the interpreter at exit (status 134), so ``stream_to_operator``
passes the raw stream. That only shows in a separate process whose stdin is still open when
the command finishes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.windows

CHILD = r"""
import getpass, sys
from pathlib import Path
from agentworks.execution import client, ssh_process
from agentworks.execution.access import VMAccess, stream_to_operator
from agentworks.execution.client import SSHClient
from agentworks.execution.requests import Command

# A local shell stands in for the account shell, and python3 for the guest.
client.resolve_ssh = lambda: SSHClient("/nonexistent/ssh", (9, 9))
ssh_process.build_argv = lambda ssh, policy, destination, mode, command, *a, **k: [
    "/bin/sh", "-c", command.replace(ssh_process.DEFAULT_PYTHON, sys.executable, 1),
]
access = VMAccess(
    vm_name="local", host="127.0.0.1", user=getpass.getuser(),
    identity_file=Path("/nonexistent"),
)
SETUP
status = stream_to_operator(access, Command(COMMAND, deadline=60))
sys.stdout.flush()
sys.exit(status)
"""


def child_source(command: tuple[str, ...], setup: str = "") -> str:
    return CHILD.replace("COMMAND", repr(command)).replace("SETUP", setup)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in account shell is /bin/sh")
def test_the_guest_exits_while_stdin_is_still_open_and_the_process_exits_cleanly() -> None:
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", child_source(("sh", "-c", "echo out; echo err >&2; exit 3"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in account shell is /bin/sh and the pipe is POSIX")
def test_output_is_byte_exact_when_the_operators_stdout_is_a_non_blocking_pipe_with_a_slow_reader() -> None:
    # The guest writes small records, so the pump's chunks fit the stream's buffer and it is the
    # flush, not the write, that meets the full pipe.
    records, size = 1_200, 997
    expected = b"".join(bytes([n % 251]) * size for n in range(records))
    program = (
        "import sys, time\n"
        f"for n in range({records}):\n"
        f"    sys.stdout.buffer.write(bytes([n % 251]) * {size}); sys.stdout.buffer.flush(); time.sleep(0.0002)\n"
    )
    source = child_source((sys.executable, "-c", program), setup="import os; os.set_blocking(1, False)")
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", source], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    received = bytearray()
    try:
        assert child.stdout is not None
        while chunk := os.read(child.stdout.fileno(), 4_096):
            received += chunk
            time.sleep(0.005)  # slower than the guest, so the pipe stays full
        child.wait(timeout=30)
        assert child.stderr is not None
        stderr = child.stderr.read()
    finally:
        child.kill()
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()

    assert child.returncode == 0, stderr
    assert len(received) == len(expected)
    assert bytes(received) == expected
