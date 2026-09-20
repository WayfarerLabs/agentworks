"""Validation shared by carriers that borrow nonblocking byte endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.execution.carrier import ByteSink


class SinkWriteError(Exception):
    """A borrowed sink failed without carrying its exception or representation."""


def try_write_to_sink(sink: ByteSink, data: memoryview) -> int | None:
    """Attempt one bounded write and validate the adapter-authored result."""
    failed = False
    try:
        written = sink.try_write(data)
    except Exception:
        failed = True
        written = None
    if failed or not data or (written is not None and (type(written) is not int or not 1 <= written <= len(data))):
        raise SinkWriteError
    return written
