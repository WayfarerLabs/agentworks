"""A stream nothing reads any more never fails the command writing to it."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.cli._typer_output import TyperHandler

if TYPE_CHECKING:
    from collections.abc import Iterator

HANDLERS = [TyperHandler, output._DefaultHandler]


def _is_null(descriptor: int) -> bool:
    return os.path.samestat(os.fstat(descriptor), os.stat(os.devnull))


@contextlib.contextmanager
def _streams(stdout: io.TextIOBase, stderr: io.TextIOBase, handler: object) -> Iterator[None]:
    """Entered in the test body: the runner's capture puts its own streams back around each fixture."""
    saved = (sys.stdout, sys.stderr, output.get_handler())
    sys.stdout, sys.stderr = stdout, stderr  # type: ignore[assignment]
    output.set_handler(handler)  # type: ignore[arg-type]
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved[0], saved[1]
        output.set_handler(saved[2])


@pytest.mark.windows
@pytest.mark.parametrize("handler", HANDLERS, ids=["typer", "default"])
def test_lines_to_a_pipe_whose_reader_left_are_dropped_and_the_pipe_points_at_the_null_device(
    handler: type,
) -> None:
    """``| head`` has exited: every line, on either stream, goes nowhere, and so does what is left at exit."""
    reader, writer = os.pipe()
    os.close(reader)
    with open(writer, "w", encoding="utf-8", buffering=1) as stream, _streams(stream, stream, handler()):
        output.info("first")
        output.warn("second")
        with output.section("Rolling back"):
            output.progress("Terminating").done()
        output.result("done")

        assert _is_null(writer)
        stream.flush()


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal is POSIX")
@pytest.mark.parametrize("handler", HANDLERS, ids=["typer", "default"])
def test_lines_to_a_terminal_that_hung_up_are_dropped(handler: type) -> None:
    import pty

    controller, terminal = pty.openpty()
    os.close(controller)
    with open(terminal, "w", encoding="utf-8", buffering=1) as stream, _streams(stream, stream, handler()):
        output.warn("Interrupted: cleaning up")
        output.info("Closed SSH route")

        assert _is_null(terminal)


class _FullDisk(io.StringIO):
    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.windows
@pytest.mark.parametrize("handler", HANDLERS, ids=["typer", "default"])
def test_a_failure_that_does_not_mean_the_reader_left_still_raises(handler: type) -> None:
    """``agw vm list > out.txt`` on a full disk: the output is wanted, so losing it must not pass silently."""
    full = _FullDisk()
    with _streams(full, io.StringIO(), handler()), pytest.raises(OSError, match="No space"):
        output.info("a row")


# The reader takes the first line and exits; every later line, and the interpreter's own flush at exit,
# then meets a pipe nobody reads. None of it may change the exit status or print a traceback. With
# LATE=print, nothing after the first line goes through the output module, so only the flush at exit
# meets the dead pipe.
LATE_OUTPUT = """
import os, time
import typer
from agentworks import cli, output
from agentworks.cli._entry import main

app = typer.Typer()

@app.command()
def emit() -> None:
    output.info("first")
    time.sleep(0.5)
    if os.environ["LATE"] == "lines":
        for index in range(1000):
            output.info(f"line {index}")
            output.warn(f"warning {index}")
    print("buffered, flushed only at exit")

cli.app = app
main()
"""


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("head") is None, reason="needs a POSIX pipeline")
@pytest.mark.parametrize("late", ["lines", "print"])
@pytest.mark.parametrize("merged", [True, False], ids=["2>&1 | head", "| head"])
def test_a_command_whose_reader_left_exits_as_it_would_have_and_quietly(merged: bool, late: str) -> None:
    writer = subprocess.Popen(
        [sys.executable, "-I", "-c", LATE_OUTPUT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merged else subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "LATE": late},
    )
    assert writer.stdout is not None
    reader = subprocess.Popen(["head", "-1"], stdin=writer.stdout, stdout=subprocess.PIPE)
    writer.stdout.close()

    taken, _ = reader.communicate(timeout=30)
    _, stderr = writer.communicate(timeout=30)

    assert taken == b"first\n"
    assert writer.returncode == 0
    if not merged:
        # A live stderr keeps every warning, and nothing else.
        assert stderr.decode().splitlines() == [
            f"Warning: warning {index}" for index in range(1000 if late == "lines" else 0)
        ]
