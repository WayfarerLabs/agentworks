"""Turn the signals that end a foreground command into the operator's interrupt.

Python ends on an unhandled ``SIGTERM`` or ``SIGHUP`` without unwinding, so
no ``finally`` runs: a cloud's temporary firewall allow, or any other state a
command holds open, would outlive it. :func:`termination_as_interrupt` raises
``KeyboardInterrupt`` for them instead, as Ctrl-C does, for the span of a
command that waits on something the operator ends (an interactive shell, a
port forward), and restores the previous handlers afterwards. Once one has
arrived, further ones are ignored until the block exits, so a second signal
cannot cut short the cleanup the first one started. A signal the
process already ignores stays ignored, so a command started under ``nohup``
keeps running after its terminal closes.
"""

from __future__ import annotations

import contextlib
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# SIGHUP exists only on POSIX.
_TERMINATIONS = tuple(
    number for number in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if number is not None
)


def _interrupt(_signal: int, _frame: object) -> None:
    for number in _TERMINATIONS:
        if signal.getsignal(number) is _interrupt:
            signal.signal(number, signal.SIG_IGN)
    raise KeyboardInterrupt


@contextlib.contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """Raise ``KeyboardInterrupt`` on ``SIGTERM``, and ``SIGHUP`` where it exists, inside the block, unless ignored.

    Inside an enclosing block it changes nothing, so the outermost block owns
    the handlers and their restoration.
    """
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
