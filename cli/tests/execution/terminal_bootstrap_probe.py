#!/usr/bin/env python3
"""Executable Linux PTY bootstrap experiment, not a production helper contract.

The fixed process switches its existing terminal to raw/no-echo before accepting
one bounded binary frame. It restores the original mode before acknowledging
that interactive input may begin, then replaces itself with the literal child.
"""

from __future__ import annotations

import contextlib
import os
import signal
import struct
import sys
import termios
import tty
from dataclasses import dataclass

PAYLOAD_READY = b"AGWPTY1:PAYLOAD!"
INTERACTIVE_READY = b"AGWPTY1:INTERACTIVE!"
FRAME_MAGIC = b"AGWPTY1\0"
MAX_FRAME_BYTES = 32_768
MAX_ITEMS = 128
SOURCE_FD = 3
FAILURE_STATUS = 125


class ProtocolError(Exception):
    """The bounded client frame cannot be launched safely."""


class Interrupted(Exception):
    """A local signal interrupted bootstrap before exec."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


@dataclass(frozen=True)
class Payload:
    argv: tuple[bytes, ...]
    env: dict[bytes, bytes]
    source: bytes


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def take(self, length: int) -> bytes:
        if length < 0 or length > len(self._data) - self._offset:
            raise ProtocolError
        start = self._offset
        self._offset += length
        return self._data[start : self._offset]

    def unsigned_short(self) -> int:
        return int(struct.unpack("!H", self.take(2))[0])

    def bytestring(self) -> bytes:
        return self.take(struct.unpack("!I", self.take(4))[0])

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise ProtocolError


def _read_exact(fd: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        try:
            chunk = os.read(fd, length - len(chunks))
        except OSError as error:
            raise ProtocolError from error
        if not chunk:
            raise ProtocolError
        chunks.extend(chunk)
    return bytes(chunks)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise ProtocolError
        remaining = remaining[written:]


def _decode_frame(fd: int) -> Payload:
    header = _read_exact(fd, len(FRAME_MAGIC) + 4)
    if header[: len(FRAME_MAGIC)] != FRAME_MAGIC:
        raise ProtocolError
    length = struct.unpack("!I", header[len(FRAME_MAGIC) :])[0]
    if length > MAX_FRAME_BYTES:
        raise ProtocolError
    reader = _Reader(_read_exact(fd, length))

    argument_count = reader.unsigned_short()
    if not 0 < argument_count <= MAX_ITEMS:
        raise ProtocolError
    argv = tuple(reader.bytestring() for _ in range(argument_count))
    if not argv[0].startswith(b"/") or any(not value or b"\0" in value for value in argv):
        raise ProtocolError

    environment_count = reader.unsigned_short()
    if environment_count > MAX_ITEMS:
        raise ProtocolError
    environment: dict[bytes, bytes] = {}
    for _ in range(environment_count):
        key = reader.bytestring()
        value = reader.bytestring()
        if not key or b"=" in key or b"\0" in key or b"\0" in value or key in environment:
            raise ProtocolError
        environment[key] = value

    source = reader.bytestring()
    reader.finish()
    return Payload(argv, environment, source)


def _source_descriptor(source: bytes) -> None:
    """Install source on fd 3 without a filesystem staging object."""
    descriptor = os.memfd_create("agw-terminal-source", os.MFD_CLOEXEC)
    try:
        _write_all(descriptor, source)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if descriptor != SOURCE_FD:
            os.dup2(descriptor, SOURCE_FD, inheritable=True)
        else:
            os.set_inheritable(descriptor, True)
    finally:
        if descriptor != SOURCE_FD:
            os.close(descriptor)


def _interrupt(signum: int, _frame: object) -> None:
    raise Interrupted(signum)


def run() -> int:
    """Consume one payload from the existing PTY and exec its child."""
    terminal_fd = 0
    saved_mode: list[object] | None = None
    try:
        if not os.isatty(terminal_fd) or not os.isatty(1) or not hasattr(os, "memfd_create"):
            return FAILURE_STATUS
        saved_mode = termios.tcgetattr(terminal_fd)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, _interrupt)
        tty.setraw(terminal_fd, termios.TCSANOW)
        _write_all(1, PAYLOAD_READY)
        payload = _decode_frame(terminal_fd)
        _source_descriptor(payload.source)

        termios.tcsetattr(terminal_fd, termios.TCSANOW, saved_mode)
        saved_mode = None
        _write_all(1, INTERACTIVE_READY)
        os.execve(payload.argv[0], payload.argv, payload.env)
    except Interrupted as interruption:
        return 128 + interruption.signum
    except (OSError, ProtocolError, termios.error):
        return FAILURE_STATUS
    finally:
        if saved_mode is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(terminal_fd, termios.TCSANOW, saved_mode)
    return FAILURE_STATUS


if __name__ == "__main__":
    sys.exit(run())
