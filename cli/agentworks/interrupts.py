"""Turn the signals that end a foreground command into the operator's interrupt.

Python ends on an unhandled ``SIGTERM``, ``SIGHUP`` or ``SIGBREAK`` without
unwinding, so no ``finally`` runs: a cloud's temporary firewall allow, or any
other state a command holds open, would outlive it.
:func:`termination_as_interrupt` raises ``KeyboardInterrupt`` for them
instead, as Ctrl-C does, for the span of a command that waits on something
the operator ends (an interactive shell, a port forward) or holds a cloud
route open, and restores the previous handlers afterwards. Once one has
arrived, further ones are ignored until the block exits, so a second signal
cannot cut short the cleanup the first one started. A signal the process
already ignores stays ignored, so a command started under ``nohup`` keeps
running after its terminal closes.

A hangup means the terminal is gone, and every write to it fails, so the
first thing the cleanup printed would raise and end it. Before it raises the
interrupt, the hangup's handler points standard output and standard error at
the null device, so the cleanup runs to the end, silently.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# SIGHUP exists only on POSIX, and SIGBREAK (Ctrl-Break) only on Windows.
_HANGUP = getattr(signal, "SIGHUP", None)
_TERMINATIONS = tuple(
    number
    for number in (getattr(signal, "SIGTERM", None), _HANGUP, getattr(signal, "SIGBREAK", None))
    if number is not None
)


def _interrupt(signal_number: int, _frame: object) -> None:
    for number in _TERMINATIONS:
        if signal.getsignal(number) is _interrupt:
            signal.signal(number, signal.SIG_IGN)
    if signal_number == _HANGUP:
        _write_to_the_null_device()
    raise KeyboardInterrupt


def _write_to_the_null_device() -> None:
    """Point file descriptors 1 and 2, and the Python streams, at the null device.

    The descriptors cover what child processes inherit and the streams that
    sit on them. A stream on another descriptor (an embedding host's, a test
    runner's) is replaced, since its target may have gone with the terminal.
    """
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        for descriptor in (1, 2):
            os.dup2(null, descriptor)
    finally:
        os.close(null)
    for name, descriptor in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name)
        try:
            shared = stream.fileno() == descriptor
        except (AttributeError, OSError, ValueError):
            shared = False
        if not shared:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - lives as long as the process


@contextlib.contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """Raise ``KeyboardInterrupt`` on ``SIGTERM``, ``SIGHUP`` and ``SIGBREAK`` where they exist, unless ignored.

    Inside an enclosing block, or off the main thread, it changes nothing: the
    outermost block owns the handlers and their restoration.
    """
    if threading.current_thread() is not threading.main_thread():
        # Only the main thread may set handlers, and only it receives signals.
        yield
        return
    previous = {
        number: signal.signal(number, _interrupt)
        for number in _TERMINATIONS
        if signal.getsignal(number) not in (signal.SIG_IGN, _interrupt)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
