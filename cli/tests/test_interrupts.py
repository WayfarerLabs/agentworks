"""``termination_as_interrupt``: the signals that end a command unwind it like Ctrl-C, and only inside the block."""

from __future__ import annotations

import os
import signal
import sys

import pytest

from agentworks.interrupts import termination_as_interrupt

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP and os.kill delivery are POSIX")


@pytest.mark.windows
def test_a_sigterm_inside_the_block_is_an_interrupt_and_the_handler_is_restored() -> None:
    """Windows has SIGTERM handlers but no external delivery to them, so the signal is raised in-process."""
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM was not delivered as an interrupt")

    assert signal.getsignal(signal.SIGTERM) == before


@posix_only
def test_a_hangup_inside_the_block_is_an_interrupt_and_the_handler_is_restored() -> None:
    before = signal.getsignal(signal.SIGHUP)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        os.kill(os.getpid(), signal.SIGHUP)
        raise AssertionError("SIGHUP was not delivered as an interrupt")

    assert signal.getsignal(signal.SIGHUP) == before


@posix_only
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


@posix_only
def test_a_second_termination_does_not_cut_the_unwind_short() -> None:
    """The cleanup a first signal starts (closing a cloud route, say) runs to the end through a second one."""
    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    unwound = False

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            os.kill(os.getpid(), signal.SIGHUP)
            raise AssertionError("SIGHUP was not delivered as an interrupt")
        finally:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("a second termination interrupted the unwind")
            unwound = True

    assert unwound
    assert {number: signal.getsignal(number) for number in before} == before


@posix_only
def test_a_second_termination_does_not_cut_an_outer_unwind_short() -> None:
    """An inner block's exit must not re-arm the handler while the outer block is still unwinding the first signal."""
    before = signal.getsignal(signal.SIGHUP)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            with termination_as_interrupt():
                os.kill(os.getpid(), signal.SIGHUP)
                raise AssertionError("SIGHUP was not delivered as an interrupt")
        finally:
            try:
                os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("a second termination interrupted the outer unwind")

    assert signal.getsignal(signal.SIGHUP) == before
