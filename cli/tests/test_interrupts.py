"""``termination_as_interrupt``: the signals that end a command unwind it like Ctrl-C, and only inside the block."""

from __future__ import annotations

import os
import signal
import sys

import pytest

from agentworks.interrupts import termination_as_interrupt

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
def test_a_termination_inside_the_block_is_an_interrupt_and_the_handler_is_restored(signal_name: str) -> None:
    number = getattr(signal, signal_name)
    before = signal.getsignal(number)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        os.kill(os.getpid(), number)
        raise AssertionError(f"{signal_name} was not delivered as an interrupt")

    assert signal.getsignal(number) == before


def test_an_ignored_hangup_stays_ignored_inside_the_block() -> None:
    """``nohup`` ignores SIGHUP so the command outlives its terminal; the block must not undo that."""
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with termination_as_interrupt():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            try:
                os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("an ignored SIGHUP interrupted the block")
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)
