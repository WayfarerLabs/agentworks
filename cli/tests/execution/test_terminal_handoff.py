"""Host and local Linux PTY checks for the private terminal handoff candidate."""

from __future__ import annotations

import sys

import pytest

if sys.platform != "linux":  # pragma: no cover - platform guard
    pytest.skip("the terminal handoff candidate requires Linux", allow_module_level=True)

import contextlib
import hashlib
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Iterator
from pathlib import Path

from agentworks.errors import ValidationError
from agentworks.execution._terminal_guest import (
    FRAME_MAGIC,
    INTERACTIVE_READY,
    MAX_PAYLOAD_BYTES,
    PAYLOAD_READY,
    READINESS_MAGIC,
)
from agentworks.execution._terminal_handoff import (
    PreparedTerminalHandoff,
    TerminalHandoffError,
    TerminalHandoffFailure,
    prepare_terminal_handoff,
)


class CollectSink:
    def __init__(self, *, max_write: int | None = None, stalled: bool = False) -> None:
        self.data = bytearray()
        self.max_write = max_write
        self.stalled = stalled

    def try_write(self, data: memoryview) -> int | None:
        if self.stalled:
            return None
        length = len(data) if self.max_write is None else min(len(data), self.max_write)
        self.data.extend(data[:length])
        return length


def _marker(prepared: PreparedTerminalHandoff, kind: int) -> bytes:
    return READINESS_MAGIC + prepared.nonce.encode("ascii") + b":" + bytes((kind,))


def _drain_bootstrap(prepared: PreparedTerminalHandoff, limit: int = 11) -> bytes:
    payload = bytearray()
    while True:
        chunk = prepared.bootstrap.try_read(limit)
        if chunk is None:
            return bytes(payload)
        assert chunk
        payload.extend(chunk)


def _prepared(presentation: CollectSink | None = None) -> tuple[PreparedTerminalHandoff, CollectSink]:
    selected = presentation or CollectSink()
    prepared = prepare_terminal_handoff(
        (b"/bin/example", b"", b"two words"),
        {b"VALUE": b"line1\nline2"},
        b"source\0bytes\r\n",
        selected,
    )
    return prepared, selected


def test_two_gates_release_only_the_finite_payload_then_handoff_eof() -> None:
    prepared, presentation = _prepared(CollectSink(max_write=2))
    first = b"setup-withheld" + _marker(prepared, PAYLOAD_READY)
    for byte in first:
        assert prepared.stdout.try_write(memoryview(bytes((byte,)))) == 1

    payload = _drain_bootstrap(prepared)
    assert payload.startswith(FRAME_MAGIC)
    assert len(payload) <= MAX_PAYLOAD_BYTES
    assert prepared.bootstrap.try_read(4) is None

    second = b"more-setup" + _marker(prepared, INTERACTIVE_READY) + b"abcdef"
    consumed = prepared.stdout.try_write(memoryview(second))
    assert consumed == len(second) - 4
    assert bytes(presentation.data) == b"ab"
    remainder = memoryview(second)[consumed:]
    while remainder:
        written = prepared.stdout.try_write(remainder)
        assert written is not None
        remainder = remainder[written:]

    marker_text = _marker(prepared, PAYLOAD_READY)
    remaining = memoryview(marker_text)
    while remaining:
        written = prepared.stdout.try_write(remaining)
        assert written is not None
        remaining = remaining[written:]
    assert bytes(presentation.data) == b"abcdef" + marker_text
    assert prepared.bootstrap.try_read(4) == b""
    assert prepared.handed_off and prepared.failure is None


def test_stalled_presentation_acknowledges_only_filtered_prefix() -> None:
    presentation = CollectSink(stalled=True)
    prepared, _ = _prepared(presentation)
    assert prepared.stdout.try_write(memoryview(_marker(prepared, PAYLOAD_READY))) is not None
    _drain_bootstrap(prepared)
    value = _marker(prepared, INTERACTIVE_READY) + b"suffix"

    assert prepared.stdout.try_write(memoryview(value)) == len(value) - len(b"suffix")
    assert prepared.stdout.try_write(memoryview(b"suffix")) is None
    assert bytes(presentation.data) == b""


@pytest.mark.parametrize(
    "drive",
    [
        lambda prepared: _marker(prepared, INTERACTIVE_READY),
        lambda prepared: _marker(prepared, PAYLOAD_READY) + _marker(prepared, PAYLOAD_READY),
        lambda prepared: _marker(prepared, PAYLOAD_READY) + _marker(prepared, INTERACTIVE_READY),
        lambda prepared: READINESS_MAGIC + prepared.nonce.encode("ascii") + b":\xff",
    ],
)
def test_wrong_order_duplicate_early_and_malformed_readiness_fail_closed(drive) -> None:
    prepared, _ = _prepared()

    with pytest.raises(TerminalHandoffError) as caught:
        prepared.stdout.try_write(memoryview(drive(prepared)))

    assert caught.value.failure is TerminalHandoffFailure.PROTOCOL
    assert prepared.failure is TerminalHandoffFailure.PROTOCOL
    with pytest.raises(TerminalHandoffError):
        prepared.bootstrap.try_read(1)


def test_truncated_readiness_closes_without_retaining_prefix() -> None:
    prepared, _ = _prepared()
    marker = _marker(prepared, PAYLOAD_READY)
    prepared.stdout.try_write(memoryview(b"noise" + marker[:-1]))

    prepared.stdout.finish()

    assert prepared.failure is TerminalHandoffFailure.TRUNCATED
    assert "noise" not in repr(prepared)


def test_presentation_failure_drops_payload_bearing_cause() -> None:
    canary = "terminal-presentation-canary-e02a9a"

    class BrokenSink:
        @staticmethod
        def try_write(_data: memoryview) -> int:
            raise RuntimeError(canary)

    prepared = prepare_terminal_handoff((b"/bin/true",), {}, b"source", BrokenSink())
    prepared.stdout.try_write(memoryview(_marker(prepared, PAYLOAD_READY)))
    _drain_bootstrap(prepared)
    prepared.stdout.try_write(memoryview(_marker(prepared, INTERACTIVE_READY)))

    with pytest.raises(TerminalHandoffError) as caught:
        prepared.stdout.try_write(memoryview(b"application output"))

    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert canary not in repr(caught.value)
    assert prepared.failure is TerminalHandoffFailure.PRESENTATION


def test_payload_material_stays_off_fixed_argv_and_preparation_is_single_use() -> None:
    canary = b"terminal-payload-canary-c923eb"
    prepared = prepare_terminal_handoff(
        (b"/bin/tool", b"", canary),
        {b"SECRET": canary},
        canary,
        CollectSink(),
    )

    assert all(canary.decode() not in argument for argument in prepared.invocation.argv)
    assert canary.decode() not in repr(prepared)
    assert prepared.invocation.argv[-1] == prepared.nonce
    prepared.claim()
    with pytest.raises(ValidationError):
        prepared.claim()


CHILD_CODE = b"""
import hashlib, os, sys
source = bytearray()
while True:
    chunk = os.read(3, 4096)
    if not chunk:
        break
    source.extend(chunk)
proof = b':'.join((hashlib.sha256(source).hexdigest().encode(),
                   hashlib.sha256(os.environb[b'SECRET']).hexdigest().encode(),
                   os.fsencode(sys.argv[1]).hex().encode()))
os.write(1, b'PROOF:' + proof + b'!')
os.write(1, b'TTY:' + os.ttyname(0).encode() + b'!')
incoming = os.read(0, 1024)
os.write(1, b'INPUT:' + incoming.hex().encode() + b'!')
"""


class TerminalProcess:
    def __init__(
        self,
        prepared: PreparedTerminalHandoff,
        *,
        cwd: Path | None = None,
        output_flags: int = 0,
    ) -> None:
        self.prepared = prepared
        self.master, self.slave = pty.openpty()
        if output_flags:
            mode = termios.tcgetattr(self.slave)
            mode[1] |= output_flags
            termios.tcsetattr(self.slave, termios.TCSANOW, mode)
        self.original_mode = termios.tcgetattr(self.slave)
        self.raw_output = bytearray()
        try:
            self.process = subprocess.Popen(
                list(prepared.invocation.argv),
                stdin=self.slave,
                stdout=self.slave,
                stderr=self.slave,
                close_fds=True,
                cwd=cwd,
            )
        except BaseException:
            os.close(self.master)
            os.close(self.slave)
            raise

    def _read(self, timeout: float) -> bytes:
        readable, _, _ = select.select([self.master], [], [], timeout)
        if not readable:
            return b""
        data = os.read(self.master, 4096)
        self.raw_output.extend(data)
        return data

    def wait_for_payload_gate(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while True:
            chunk = self.prepared.bootstrap.try_read(13)
            if chunk is not None:
                self._write_all(chunk)
                break
            data = self._read(max(0.0, deadline - time.monotonic()))
            if not data:
                raise AssertionError("payload-ready marker did not arrive")
            assert self.prepared.stdout.try_write(memoryview(data)) == len(data)
        while True:
            chunk = self.prepared.bootstrap.try_read(13)
            if chunk is None:
                return
            self._write_all(chunk)

    def wait_for_handoff(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not self.prepared.handed_off:
            data = self._read(max(0.0, deadline - time.monotonic()))
            if not data:
                raise AssertionError("interactive-ready marker did not arrive")
            remaining = memoryview(data)
            while remaining:
                written = self.prepared.stdout.try_write(remaining)
                if written is None:
                    continue
                remaining = remaining[written:]

    def read_until_presented(self, presentation: CollectSink, marker: bytes, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while marker not in presentation.data:
            data = self._read(max(0.0, deadline - time.monotonic()))
            if not data:
                raise AssertionError(f"presentation marker did not arrive: {bytes(presentation.data)!r}")
            remaining = memoryview(data)
            while remaining:
                written = self.prepared.stdout.try_write(remaining)
                assert written is not None
                remaining = remaining[written:]

    def _write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(self.master, remaining) :]

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=0.5)
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            for descriptor in (self.master, self.slave):
                with contextlib.suppress(OSError):
                    os.close(descriptor)


@pytest.fixture
def running_processes() -> Iterator[list[TerminalProcess]]:
    running: list[TerminalProcess] = []
    try:
        yield running
    finally:
        for process in running:
            process.close()


def test_actual_pty_keeps_source_environment_and_empty_arg_off_terminal_input(
    tmp_path: Path,
    running_processes: list[TerminalProcess],
) -> None:
    source = bytes(range(256)) * 4 + b"\r\nsource-canary-913c"
    secret = b"environment\n\xff-secret"
    empty_argument = b""
    presentation = CollectSink()
    prepared = prepare_terminal_handoff(
        (b"/usr/bin/python3", b"-I", b"-S", b"-B", b"-c", CHILD_CODE, empty_argument),
        {b"SECRET": secret},
        source,
        presentation,
    )
    before = set(tmp_path.iterdir())
    running = TerminalProcess(prepared, cwd=tmp_path)
    running_processes.append(running)
    process_path = Path(f"/proc/{running.process.pid}")

    running.wait_for_payload_gate()
    payload_mode = termios.tcgetattr(running.slave)
    assert not payload_mode[3] & (termios.ECHO | termios.ICANON)
    assert source not in running.raw_output and secret not in running.raw_output
    running.wait_for_handoff()
    assert termios.tcgetattr(running.slave) == running.original_mode

    expected = b":".join(
        (
            hashlib.sha256(source).hexdigest().encode(),
            hashlib.sha256(secret).hexdigest().encode(),
            empty_argument.hex().encode(),
        )
    )
    running.read_until_presented(presentation, b"PROOF:" + expected + b"!")
    running.read_until_presented(presentation, b"TTY:" + os.ttyname(running.slave).encode() + b"!")
    running._write_all(b"terminal-line\n")
    running.read_until_presented(presentation, b"INPUT:7465726d696e616c2d6c696e650a!")

    assert running.process.wait(timeout=5) == 0
    assert not process_path.exists()
    assert set(tmp_path.iterdir()) == before
    assert source not in running.raw_output and secret not in running.raw_output


def test_actual_pty_native_shell_inherits_default_sigpipe(
    running_processes: list[TerminalProcess],
) -> None:
    presentation = CollectSink()
    prepared = prepare_terminal_handoff(
        (b"/bin/bash", b"--noprofile", b"--norc", b"-c", b"trap -p PIPE; printf SHELL-DONE!"),
        {},
        b"",
        presentation,
    )
    running = TerminalProcess(prepared)
    running_processes.append(running)

    running.wait_for_payload_gate()
    running.wait_for_handoff()
    running.read_until_presented(presentation, b"SHELL-DONE!")

    assert running.process.wait(timeout=5) == 0
    assert bytes(presentation.data) == b"SHELL-DONE!"


def test_actual_pty_restored_uppercase_output_mode_preserves_second_marker(
    running_processes: list[TerminalProcess],
) -> None:
    uppercase_output = getattr(termios, "OLCUC", None)
    if uppercase_output is None:
        pytest.skip("this host does not expose the Linux OLCUC terminal flag")
    prepared = prepare_terminal_handoff((b"/bin/true",), {}, b"", CollectSink())
    running = TerminalProcess(prepared, output_flags=termios.OPOST | uppercase_output)
    running_processes.append(running)

    running.wait_for_payload_gate()
    running.wait_for_handoff()

    assert prepared.handed_off
    assert termios.tcgetattr(running.slave) == running.original_mode
    assert running.process.wait(timeout=5) == 0


@pytest.mark.parametrize("truncated", [False, True], ids=["malformed", "truncated"])
def test_actual_pty_restores_mode_and_leaves_no_process_on_bad_payload(
    truncated: bool,
    running_processes: list[TerminalProcess],
) -> None:
    prepared, _ = _prepared()
    running = TerminalProcess(prepared)
    running_processes.append(running)
    process_path = Path(f"/proc/{running.process.pid}")

    deadline = time.monotonic() + 5
    marker = _marker(prepared, PAYLOAD_READY)
    while marker not in running.raw_output:
        data = running._read(max(0.0, deadline - time.monotonic()))
        if not data:
            raise AssertionError("payload-ready marker did not arrive")
    assert not termios.tcgetattr(running.slave)[3] & (termios.ECHO | termios.ICANON)
    if truncated:
        running._write_all(FRAME_MAGIC + struct.pack("!I", 10) + b"short")
        os.kill(running.process.pid, signal.SIGTERM)
    else:
        running._write_all(FRAME_MAGIC + struct.pack("!I", MAX_PAYLOAD_BYTES))

    assert running.process.wait(timeout=5) != 0
    assert termios.tcgetattr(running.slave) == running.original_mode
    assert not process_path.exists()
