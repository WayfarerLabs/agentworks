"""Turn the signals that end a foreground command into the operator's interrupt.

Python ends on an unhandled ``SIGTERM``, ``SIGHUP`` or ``SIGBREAK`` without
unwinding, so no ``finally`` runs: a cloud's temporary firewall allow, or any
other state a command holds open, would outlive it.
:func:`termination_as_interrupt` raises ``KeyboardInterrupt`` for them
instead, as Ctrl-C does, for the span of a command that waits on something
the operator ends (an interactive shell, a port forward) or holds a cloud
route open, and restores the previous handlers afterwards. Ctrl-C takes the
same path, so whichever signal arrives first starts the cleanup, and from then
until the block exits ``SIGTERM``, ``SIGHUP`` and ``SIGBREAK`` are ignored: a
closed terminal or a second termination cannot cut short the cleanup the
first signal started. A second Ctrl-C still interrupts, since that is how the
operator abandons a cleanup that hangs. A signal the process already ignores
stays ignored, so a command started under ``nohup`` keeps running after its
terminal closes.

A hangup needs nothing more: the cleanup's output to the terminal it took
away, or to a pipe into a program in that terminal's session, goes nowhere
(see :func:`agentworks.output.tolerating_dead_sink`) rather than failing.

:func:`interrupts_deferred` holds every one of these signals for a short step
that must not stop halfway, such as removing a firewall allow, and delivers
the first of them once the step is done.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    # What ``signal.getsignal`` returns and ``signal.signal`` takes, less the ``None`` neither can put back.
    _Handler = Callable[[int, FrameType | None], Any] | int

# SIGHUP exists only on POSIX, and SIGBREAK (Ctrl-Break) only on Windows.
_TERMINATIONS = tuple(
    number
    for number in (
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
        getattr(signal, "SIGBREAK", None),
    )
    if number is not None
)
_INTERRUPTS = (signal.SIGINT, *_TERMINATIONS)


def _interrupt(_signal_number: int, _frame: object) -> None:
    for number in _TERMINATIONS:
        if signal.getsignal(number) is _interrupt:
            signal.signal(number, signal.SIG_IGN)
    raise KeyboardInterrupt


def _replace_handlers(handler: Callable[[int, object], None], *, keep: tuple[object, ...]) -> dict[int, _Handler]:
    """Install ``handler`` for each of the interrupts whose current handler is not in ``keep``; return what it replaced.

    A handler installed outside Python reads as ``None`` and could not be put
    back, so that signal is left to whoever installed it.
    """
    previous: dict[int, _Handler] = {}
    for number in _INTERRUPTS:
        current = signal.getsignal(number)
        if current is not None and current not in keep:
            previous[number] = current
            signal.signal(number, handler)
    return previous


def _restore_handlers(previous: dict[int, _Handler]) -> None:
    """Put back each handler in ``previous``, SIGINT's last.

    A handler that runs partway through would raise out of the loop and leave
    ours installed for the rest, so on POSIX the signals are blocked for the
    restore and reach the restored handlers once it is done. Windows cannot
    block them; there SIGINT's handler, the one that raises, goes back after
    the others.
    """
    order = sorted(previous, key=lambda number: number == signal.SIGINT)
    # Read the mask before blocking: CPython runs pending handlers inside
    # pthread_sigmask itself, so the blocking call can raise and must already
    # be inside the try that puts the mask back.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, []) if hasattr(signal, "pthread_sigmask") else None
    try:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_BLOCK, _INTERRUPTS)
        for number in order:
            signal.signal(number, previous[number])
    finally:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


@contextlib.contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """Raise ``KeyboardInterrupt`` on Ctrl-C, ``SIGTERM``, ``SIGHUP`` and ``SIGBREAK`` where they exist, unless ignored.

    After the first, only Ctrl-C interrupts again until the block exits.
    Inside an enclosing block, or off the main thread, it changes nothing: the
    outermost block owns the handlers and their restoration.
    """
    if threading.current_thread() is not threading.main_thread():
        # Only the main thread may set handlers, and only it receives signals.
        yield
        return
    previous = _replace_handlers(_interrupt, keep=(signal.SIG_IGN, _interrupt))
    try:
        yield
    finally:
        _restore_handlers(previous)


@contextlib.contextmanager
def interrupts_deferred() -> Iterator[None]:
    """Hold Ctrl-C and the terminations until the block ends, then deliver the first that arrived.

    The held signal reaches whatever handler was in place before the block,
    so it does there exactly what it would have done when it arrived: inside
    :func:`termination_as_interrupt` it raises ``KeyboardInterrupt``, and
    outside one a ``SIGTERM`` ends the process. It is delivered even while an
    exception leaves the block, which it then replaces. Off the main thread
    it changes nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    arrived: list[int] = []

    def hold(signal_number: int, _frame: object) -> None:
        arrived.append(signal_number)

    previous = _replace_handlers(hold, keep=(signal.SIG_IGN,))
    try:
        yield
    finally:
        _restore_handlers(previous)
        if arrived:
            signal.raise_signal(arrived[0])
