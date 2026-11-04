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
