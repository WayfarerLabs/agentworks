"""Local Linux PTY evidence for the executable terminal bootstrap experiment."""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):  # pragma: no cover - platform guard
    pytest.skip("The terminal bootstrap experiment is Linux-only", allow_module_level=True)

import contextlib
import hashlib
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Iterator
from pathlib import Path

from tests.execution.terminal_bootstrap_probe import (
    FAILURE_STATUS,
    FRAME_MAGIC,
    INTERACTIVE_READY,
    MAX_FRAME_BYTES,
    PAYLOAD_READY,
)

PROBE = Path(__file__).with_name("terminal_bootstrap_probe.py")
PYTHON_311 = shutil.which("python3.11")
PYTHON = os.fsencode(PYTHON_311 or "")
CHILD_CODE = b"""
import hashlib, os, sys
source = bytearray()
while True:
    chunk = os.read(3, 4096)
    if not chunk:
        break
    source.extend(chunk)
arguments = b'\\0'.join(os.fsencode(value) for value in sys.argv[1:])
proof = b':'.join((hashlib.sha256(source).hexdigest().encode(),
                   hashlib.sha256(os.environb[b'SECRET']).hexdigest().encode(),
                   hashlib.sha256(arguments).hexdigest().encode()))
os.write(1, b'CHILD:' + proof + b'!')
os.write(1, b'TTY:' + os.ttyname(0).encode() + b'!')
interactive = os.read(0, 1024)
os.write(1, b'INPUT:' + interactive.hex().encode() + b'!')
"""


def _field(value: bytes) -> bytes:
    return struct.pack("!I", len(value)) + value


def _frame(argv: tuple[bytes, ...], env: dict[bytes, bytes], source: bytes) -> bytes:
    body = bytearray(struct.pack("!H", len(argv)))
    for argument in argv:
        body.extend(_field(argument))
    body.extend(struct.pack("!H", len(env)))
    for key, value in env.items():
        body.extend(_field(key))
        body.extend(_field(value))
    body.extend(_field(source))
    return FRAME_MAGIC + struct.pack("!I", len(body)) + body


class TerminalProcess:
    def __init__(self) -> None:
        if PYTHON_311 is None:
            pytest.skip("The terminal bootstrap experiment requires Python 3.11")
        self.master, self.slave = pty.openpty()
        try:
            self.original_mode = termios.tcgetattr(self.slave)
            self.output = bytearray()
            self.process = subprocess.Popen(
                [PYTHON_311, str(PROBE)],
                stdin=self.slave,
                stdout=self.slave,
                stderr=self.slave,
                close_fds=True,
            )
        except BaseException:
            for descriptor in (self.master, self.slave):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise

    def read_until(self, marker: bytes, timeout: float = 3) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for marker; output={bytes(self.output)!r}")
            readable, _, _ = select.select([self.master], [], [], remaining)
            if not readable:
                continue
            self.output.extend(os.read(self.master, 4096))
        return bytes(self.output)

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    self.process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            for name in ("master", "slave"):
                descriptor = getattr(self, name)
                if descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                    setattr(self, name, -1)


@pytest.fixture
def terminal_process() -> Iterator[TerminalProcess]:
    running = TerminalProcess()
    try:
        yield running
    finally:
        running.close()


def _assert_mode_restored(running: TerminalProcess) -> None:
    assert termios.tcgetattr(running.slave) == running.original_mode


def test_split_sensitive_frame_and_first_interactive_input_share_one_pty(
    terminal_process: TerminalProcess,
) -> None:
    source_canary = b"source-argv-canary-4f920c"
    source = bytes(range(256)) * 4 + b"\r\n" + source_canary + b"\0tail"
    secret = b"environment\n\r\x01\x03\x04\x11\x13-sensitive"
    child_arguments = (b"two words", b"line\nargument", b"\x01control")
    argv = (PYTHON, b"-c", CHILD_CODE, *child_arguments)
    frame = _frame(argv, {b"SECRET": secret}, source)
    assert len(frame) <= MAX_FRAME_BYTES + len(FRAME_MAGIC) + 4

    terminal_process.read_until(PAYLOAD_READY)
    payload_mode = termios.tcgetattr(terminal_process.slave)
    assert not payload_mode[3] & (termios.ECHO | termios.ICANON)
    offset = 0
    for chunk_length in (1, 2, 5, 3, 11, 7):
        if offset == len(frame):
            break
        next_offset = min(len(frame), offset + chunk_length)
        os.write(terminal_process.master, frame[offset:next_offset])
        offset = next_offset
    while offset < len(frame):
        next_offset = min(len(frame), offset + 13)
        os.write(terminal_process.master, frame[offset:next_offset])
        offset = next_offset

    terminal_process.read_until(INTERACTIVE_READY)
    _assert_mode_restored(terminal_process)
    assert source_canary not in terminal_process.output
    assert secret not in terminal_process.output
    expected = b":".join(
        (
            hashlib.sha256(source).hexdigest().encode(),
            hashlib.sha256(secret).hexdigest().encode(),
            hashlib.sha256(b"\0".join(child_arguments)).hexdigest().encode(),
        )
    )
    terminal_process.read_until(b"CHILD:" + expected + b"!")
    terminal_process.read_until(b"TTY:" + os.ttyname(terminal_process.slave).encode() + b"!")
    command_line = Path(f"/proc/{terminal_process.process.pid}/cmdline").read_bytes()
    assert source_canary not in command_line
    assert secret not in command_line

    os.write(terminal_process.master, b"first-interactive\r")
    terminal_process.read_until(b"INPUT:66697273742d696e7465726163746976650a!")
    process_path = Path(f"/proc/{terminal_process.process.pid}")
    assert terminal_process.process.wait(timeout=3) == 0
    assert not process_path.exists()
    _assert_mode_restored(terminal_process)


def test_input_sent_before_interactive_ack_keeps_raw_input_semantics(
    terminal_process: TerminalProcess,
) -> None:
    frame = _frame((PYTHON, b"-c", CHILD_CODE), {b"SECRET": b"x"}, b"source")
    terminal_process.read_until(PAYLOAD_READY)
    os.write(terminal_process.master, frame + b"premature\r")
    terminal_process.read_until(INTERACTIVE_READY)
    terminal_process.read_until(b"INPUT:7072656d61747572650d!")
    assert terminal_process.process.wait(timeout=3) == 0


def test_startup_input_echoes_until_payload_ready() -> None:
    master, slave = pty.openpty()
    try:
        canary = b"startup-echo-canary\r"
        os.write(master, canary)
        readable, _, _ = select.select([master], [], [], 1)
        assert readable
        assert b"startup-echo-canary" in os.read(master, 4096)
    finally:
        os.close(master)
        os.close(slave)


def test_truncated_frame_after_peer_close_does_not_launch() -> None:
    running = TerminalProcess()
    try:
        running.read_until(PAYLOAD_READY)
        frame = _frame((b"/bin/sh", b"-c", b"exit 42"), {}, b"secret\0source")
        os.write(running.master, frame[:-5])
        os.close(running.master)
        running.master = -1
        assert running.process.wait(timeout=3) == FAILURE_STATUS
    finally:
        running.close()


@pytest.mark.parametrize(
    "frame",
    [
        FRAME_MAGIC + struct.pack("!I", MAX_FRAME_BYTES + 1),
        _frame((b"/bin/sh", b"-c", b"exit 42"), {b"BAD": b"nul\0value"}, b"source"),
        _frame((b"relative-child",), {}, b"source"),
    ],
)
def test_bad_payload_restores_terminal_and_does_not_launch(terminal_process: TerminalProcess, frame: bytes) -> None:
    terminal_process.read_until(PAYLOAD_READY)
    os.write(terminal_process.master, frame)
    assert terminal_process.process.wait(timeout=3) == FAILURE_STATUS
    _assert_mode_restored(terminal_process)


def test_interruption_while_reading_restores_terminal(terminal_process: TerminalProcess) -> None:
    terminal_process.read_until(PAYLOAD_READY)
    os.kill(terminal_process.process.pid, signal.SIGTERM)
    assert terminal_process.process.wait(timeout=3) == 128 + signal.SIGTERM
    _assert_mode_restored(terminal_process)


def test_observation_timeout_cleanup_kills_and_reaps_uncooperative_child() -> None:
    child = (
        b"import os, signal, time; "
        b"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        b"os.write(1, b'IGNORING!'); time.sleep(60)"
    )
    running = TerminalProcess()
    try:
        running.read_until(PAYLOAD_READY)
        os.write(running.master, _frame((PYTHON, b"-c", child), {}, b""))
        running.read_until(INTERACTIVE_READY)
        running.read_until(b"IGNORING!")
        with pytest.raises(AssertionError):
            running.read_until(b"never-arrives", timeout=0.01)
        process_path = Path(f"/proc/{running.process.pid}")
        running.close()
        assert running.process.returncode == -signal.SIGKILL
        assert not process_path.exists()
        assert running.master == running.slave == -1
    finally:
        running.close()
