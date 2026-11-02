"""``run_process`` over Python children, so the root channel's process loop is proven on Windows too.

WSL2's ``run_as_root`` runs only on Windows, through ``PipeSession(nonce=None)``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time

import pytest

from agentworks.execution import root_script
from agentworks.execution.pump import CAPTURE_LIMIT_BYTES

pytestmark = pytest.mark.windows

# The base interpreter, not the venv's. On Windows a venv's Scripts\python.exe
# is a launcher that runs the base interpreter as its own child: it keeps the
# stdio handles open and kills that child when it exits, so a test of streams
# closing or of a lingering background child would measure the launcher.
_PYTHON = getattr(sys, "_base_executable", sys.executable)


def child(program: str) -> list[str]:
    return [_PYTHON, "-c", program]


def test_the_deadline_returns_with_what_the_child_printed() -> None:
    program = "import sys, time; sys.stdout.write('started'); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()

    raw = root_script.run_process(child(program), b"", 1)

    assert time.monotonic() - started < 10
    assert raw.stdout == b"started"


def test_a_writer_stops_at_the_output_limit() -> None:
    program = "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)"
    started = time.monotonic()

    raw = root_script.run_process(child(program), b"", 60)

    assert time.monotonic() - started < 20
    assert len(raw.stdout) == CAPTURE_LIMIT_BYTES


def test_a_background_child_holding_stdout_does_not_stall_it() -> None:
    grandchild = "import time; time.sleep(30)"
    program = (
        "import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}], stdout=sys.stdout)\n"
        "sys.stderr.write(str(p.pid)); print('done', flush=True)"
    )
    started = time.monotonic()

    raw = root_script.run_process(child(program), b"", 60)

    try:
        assert time.monotonic() - started < 10
        assert raw.stdout.replace(b"\r\n", b"\n") == b"done\n"
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(raw.stderr), signal.SIGTERM)


def test_the_script_reaches_stdin_whole_and_then_end_of_file() -> None:
    program = "import sys; data = sys.stdin.buffer.read(); sys.stdout.write(str(len(data)))"
    script = b"x" * 200_000

    raw = root_script.run_process(child(program), script, 60)

    assert raw.stdout == b"200000"


def test_both_streams_closing_ends_the_run_before_the_child_exits() -> None:
    program = "import os, time; os.close(1); os.close(2); time.sleep(30)"
    started = time.monotonic()

    root_script.run_process(child(program), b"", 60)

    assert time.monotonic() - started < 10
