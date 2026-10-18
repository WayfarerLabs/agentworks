"""Helper-owned pipe adapters and one bounded incremental evidence writer."""

from __future__ import annotations

import hashlib
import os
import select
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ._evidence_wire import Frame, FrameKind, encode_frame
from ._inline_control import StreamName

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def nonblocking_pipe(descriptor: int) -> Iterator[None]:
    """Temporarily change only the helper-owned descriptor's nonblocking flag."""
    import fcntl

    original = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    try:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, original | os.O_NONBLOCK)
        yield
    finally:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, original)


class PipeSource:
    """Read an already nonblocking helper input pipe without waiting."""

    def try_read(self, limit: int) -> bytes | None:
        try:
            return os.read(0, limit)
        except BlockingIOError:
            return None


class FrameWriter:
    """Serialize controls and data, retaining at most one unfinished data frame."""

    def __init__(self, nonce: str) -> None:
        self._nonce = nonce
        self._sequence = 0
        self._pending: memoryview | None = None
        self._body: bytes = b""
        self._stream: StreamName | None = None
        self._write(b"\n")

    def emit(self, kind: FrameKind, body: bytes) -> None:
        if self._pending is not None:
            raise OSError("unfinished evidence record")
        self._write(encode_frame(self._nonce, Frame(self._sequence, kind, body)))
        self._sequence += 1

    @staticmethod
    def _write(data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(1, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]

    def try_write(self, sink: StreamSink, data: memoryview) -> int | None:
        if self._pending is None:
            self._body = bytes(data[:4096])
            self._stream = sink.stream
            kind = FrameKind.STDOUT if sink.stream is StreamName.STDOUT else FrameKind.STDERR
            self._pending = memoryview(encode_frame(self._nonce, Frame(self._sequence, kind, self._body)))
        elif self._stream is not sink.stream:
            return None
        if not self._advance():
            return None
        return self._acknowledge(sink)

    def _advance(self) -> bool:
        assert self._pending is not None
        try:
            written = os.write(1, self._pending)
        except BlockingIOError:
            return False
        if written <= 0 or written > len(self._pending):
            raise OSError
        self._pending = self._pending[written:]
        return not self._pending

    def _acknowledge(self, sink: StreamSink) -> int:
        count = len(self._body)
        sink.record(self._body)
        self._sequence += 1
        self._pending = None
        self._body = b""
        self._stream = None
        return count

    def finish_pending(self, stdout: StreamSink, stderr: StreamSink) -> None:
        """Complete a partial record after the pump stops, or stop evidence safely."""
        expires = time.monotonic() + 0.5
        while self._pending is not None:
            if self._advance():
                self._acknowledge(stdout if self._stream is StreamName.STDOUT else stderr)
                return
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise OSError("unfinished evidence record")
            select.select([], [1], [], remaining)


class StreamSink:
    """Acknowledge framed bytes with a bounded count and rolling digest."""

    def __init__(self, writer: FrameWriter, stream: StreamName) -> None:
        self._writer = writer
        self.stream = stream
        self.count = 0
        self._digest = hashlib.sha256()

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def record(self, body: bytes) -> None:
        if self.count + len(body) > 2**63 - 1:
            raise OSError("evidence stream count overflow")
        self.count += len(body)
        self._digest.update(body)

    def try_write(self, data: memoryview) -> int | None:
        if self.count + min(len(data), 4096) > 2**63 - 1:
            raise OSError("evidence stream count overflow")
        return self._writer.try_write(self, data)
