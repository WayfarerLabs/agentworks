"""Bounded prefix input and decoded presentation for borrowed byte endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ._evidence_wire import FrameReader
    from .carrier import ByteSource


class _PresentationObserver(Protocol):
    def drain(self) -> bool: ...


class PrefixedSource:
    """Send the complete request before borrowing the application source."""

    def __init__(self, envelope: bytes, source: ByteSource) -> None:
        self._prefix = envelope
        self._source: ByteSource | None = source

    def try_read(self, limit: int) -> bytes | None:
        if self._prefix:
            chunk = self._prefix[:limit]
            self._prefix = self._prefix[len(chunk) :]
            return chunk
        return b"" if self._source is None else self._source.try_read(limit)

    def release(self) -> None:
        self._prefix = b""
        self._source = None


class PresentationCollector:
    """Drain one decoded chunk before allowing the next evidence frame."""

    def __init__(self, reader: FrameReader, observer: _PresentationObserver) -> None:
        self._reader = reader
        self._observer = observer
        self._awaiting_ack = False

    def try_write(self, data: memoryview) -> int | None:
        if not self._observer.drain():
            return None
        if self._awaiting_ack:
            self._awaiting_ack = False
            return 1
        consumed = self._reader.try_write(data)
        if not self._observer.drain():
            # Keep the final encoded byte pending in the carrier so it keeps
            # polling delivery even when the guest waits for application input.
            self._awaiting_ack = True
            return consumed - 1 if consumed > 1 else None
        return consumed


class DiscardSink:
    """Consume borrowed diagnostic bytes without retaining their contents."""

    @staticmethod
    def try_write(data: memoryview) -> int:
        return len(data)
