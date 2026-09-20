"""Fixed stdlib-only Linux guest for the private terminal handoff candidate."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import struct
import sys
from typing import Any

MAX_PAYLOAD_BYTES = 32_768
MAX_ITEMS = 128
SOURCE_FD = 3
FRAME_MAGIC = b"AGWTH1\0"
READINESS_MAGIC = b"\0AGW-TERMINAL/1:"
PAYLOAD_READY = 1
INTERACTIVE_READY = 2


class _ProtocolError(Exception):
    pass


class _Interrupted(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def take(self, length: int) -> bytes:
        if length < 0 or length > len(self.data) - self.offset:
            raise _ProtocolError
        start = self.offset
        self.offset += length
        return self.data[start : self.offset]

    def unsigned_short(self) -> int:
        return int(struct.unpack("!H", self.take(2))[0])

    def field(self) -> bytes:
        return self.take(int(struct.unpack("!I", self.take(4))[0]))

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise _ProtocolError


class _SourceDescriptor:
    def __init__(self) -> None:
        self.previous = -1
        self.previous_inheritable = False
        try:
            self.previous_inheritable = os.get_inheritable(SOURCE_FD)
            self.previous = os.dup(SOURCE_FD)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise

    def install(self, source: bytes) -> None:
        descriptor = os.memfd_create("agw-terminal-source", os.MFD_CLOEXEC)
        try:
            _write_all(descriptor, source)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if descriptor == SOURCE_FD:
                os.set_inheritable(descriptor, True)
            else:
                os.dup2(descriptor, SOURCE_FD, inheritable=True)
        finally:
            if descriptor != SOURCE_FD:
                os.close(descriptor)

    def restore(self) -> None:
        if self.previous >= 0:
            try:
                os.dup2(self.previous, SOURCE_FD, inheritable=self.previous_inheritable)
            finally:
                os.close(self.previous)
                self.previous = -1
        else:
            try:
                os.close(SOURCE_FD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def _read_exact(fd: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = os.read(fd, length - len(chunks))
        if not chunk:
            raise _ProtocolError
        chunks.extend(chunk)
    return bytes(chunks)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise _ProtocolError
        remaining = remaining[written:]


def _decode_payload(fd: int) -> tuple[tuple[bytes, ...], dict[bytes, bytes], bytes]:
    header_length = len(FRAME_MAGIC) + 4
    header = _read_exact(fd, header_length)
    if header[: len(FRAME_MAGIC)] != FRAME_MAGIC:
        raise _ProtocolError
    body_length = int(struct.unpack("!I", header[len(FRAME_MAGIC) :])[0])
    if body_length > MAX_PAYLOAD_BYTES - header_length:
        raise _ProtocolError
    reader = _Reader(_read_exact(fd, body_length))

    argument_count = reader.unsigned_short()
    if not 0 < argument_count <= MAX_ITEMS:
        raise _ProtocolError
    argv = tuple(reader.field() for _ in range(argument_count))
    if not argv[0].startswith(b"/") or b"\0" in argv[0] or any(b"\0" in value for value in argv[1:]):
        raise _ProtocolError

    environment_count = reader.unsigned_short()
    if environment_count > MAX_ITEMS:
        raise _ProtocolError
    environment: dict[bytes, bytes] = {}
    for _ in range(environment_count):
        name = reader.field()
        value = reader.field()
        if not name or b"=" in name or b"\0" in name or b"\0" in value or name in environment:
            raise _ProtocolError
        environment[name] = value

    source = reader.field()
    reader.finish()
    return argv, environment, source


def _readiness(nonce: str, kind: int) -> bytes:
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        raise _ProtocolError
    return READINESS_MAGIC + nonce.encode("ascii") + b":" + bytes((kind,))


def _interrupt(_signum: int, _frame: object) -> None:
    raise _Interrupted


def run(nonce: str) -> int:
    """Prepare one existing Linux PTY, then replace the helper with its child."""
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        return 1

    # POSIX-only imports stay behind the runtime guard so package discovery can
    # import this module on Windows.
    import termios
    import tty

    terminal_fd = 0
    saved_mode: list[Any] | None = None
    source_descriptor: _SourceDescriptor | None = None
    previous_handlers: list[tuple[int, Any]] = []
    try:
        if not os.isatty(terminal_fd) or not os.isatty(1) or os.ttyname(terminal_fd) != os.ttyname(1):
            raise _ProtocolError
        payload_ready = _readiness(nonce, PAYLOAD_READY)
        interactive_ready = _readiness(nonce, INTERACTIVE_READY)
        saved_mode = termios.tcgetattr(terminal_fd)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous_handlers.append((signum, signal.getsignal(signum)))
            signal.signal(signum, _interrupt)

        tty.setraw(terminal_fd, termios.TCSANOW)
        _write_all(1, payload_ready)
        argv, environment, source = _decode_payload(terminal_fd)
        source_descriptor = _SourceDescriptor()
        source_descriptor.install(source)

        termios.tcsetattr(terminal_fd, termios.TCSANOW, saved_mode)
        saved_mode = None
        _write_all(1, interactive_ready)
        os.execve(argv[0], argv, environment)
    except (_Interrupted, OSError, _ProtocolError, termios.error):
        return 1
    finally:
        if saved_mode is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(terminal_fd, termios.TCSANOW, saved_mode)
        if source_descriptor is not None:
            with contextlib.suppress(OSError):
                source_descriptor.restore()
        for saved_signum, handler in reversed(previous_handlers):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(saved_signum, handler)
    return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) == 2 else ""))
