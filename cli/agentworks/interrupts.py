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
interrupt, the hangup's handler points whichever of standard output and
standard error is a terminal at the null device, so the cleanup runs to the
end, silently there. Output going to a file or a pipe is left where it goes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
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
    """Point descriptors 1 and 2 that are terminals, and a Python stream on another terminal, at the null device.

    The descriptors cover what child processes inherit and the streams that
    sit on them. A descriptor that is a file or a pipe keeps its target, and
    a closed one stays closed. A stream on some other terminal (an embedding
    host's) is replaced, since that terminal may have gone too. Everything is
    decided before anything moves, since a redirected descriptor is no longer
    a terminal.
    """
    terminals = [descriptor for descriptor in (1, 2) if _is_terminal(descriptor)]
    streams = [name for name in ("stdout", "stderr") if _on_another_terminal(getattr(sys, name))]
    if terminals:
        null = _null_descriptor()
        try:
            for descriptor in terminals:
                os.dup2(null, descriptor)
        finally:
            os.close(null)
    for name in streams:
        setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - lives as long as the process


def _null_descriptor() -> int:
    """The null device on a descriptor above 2.

    With 1 or 2 closed, ``open`` would take that number, and closing the
    null device after the copies would then close the standard descriptor.
    """
    import fcntl  # POSIX, as SIGHUP is

    null = os.open(os.devnull, os.O_WRONLY)
    if null > 2:
        return null
    try:
        return fcntl.fcntl(null, fcntl.F_DUPFD, 3)
    finally:
        os.close(null)


def _on_another_terminal(stream: object) -> bool:
    try:
        descriptor = getattr(stream, "fileno")()  # noqa: B009 - a stream may lack it
    except (AttributeError, OSError, ValueError):
        return False
    return descriptor not in (1, 2) and _is_terminal(descriptor)


def _is_terminal(descriptor: int) -> bool:
    """Whether ``descriptor`` is open on a character device, as a terminal is.

    ``os.isatty`` cannot tell: once the terminal has hung up it answers
    no, since asking the terminal's settings fails, while every write
    still fails. The null device is a character device too, which is
    harmless to redirect.
    """
    try:
        return stat.S_ISCHR(os.fstat(descriptor).st_mode)
    except OSError:
        return False


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
