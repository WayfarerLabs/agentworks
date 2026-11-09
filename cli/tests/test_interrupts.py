"""``termination_as_interrupt`` and ``interrupts_deferred``: the signals that end a command, inside the block only."""

from __future__ import annotations

import os
import signal
import sys

import pytest

from agentworks.interrupts import interrupts_deferred, termination_as_interrupt

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP and os.kill delivery are POSIX")
# SIGBREAK, Ctrl-Break's signal, exists only on Windows.
_CTRL_BREAK: int | None = getattr(signal, "SIGBREAK", None)
# Windows has handlers for these but no delivery to them from outside, so each test raises them in-process.
_FIRST_SIGNALS = ["SIGINT", "SIGTERM", *(["SIGHUP"] if sys.platform != "win32" else ["SIGBREAK"])]


def _deliver(name: str) -> None:
    signal.raise_signal(getattr(signal, name))


@pytest.mark.windows
@pytest.mark.parametrize("name", _FIRST_SIGNALS)
def test_each_signal_inside_the_block_is_an_interrupt_and_the_handlers_are_restored(name: str) -> None:
    """Ctrl-Break ends a Windows console command and SIGTERM or SIGHUP a POSIX one; each must unwind."""
    before = {number: signal.getsignal(number) for number in (signal.SIGINT, getattr(signal, name))}

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        _deliver(name)
        raise AssertionError(f"{name} was not delivered as an interrupt")

    assert {number: signal.getsignal(number) for number in before} == before


@pytest.mark.windows
@pytest.mark.parametrize("first", _FIRST_SIGNALS)
def test_after_any_first_signal_a_termination_does_not_cut_the_unwind_short(first: str) -> None:
    """Ctrl-C, then the window closes: the cleanup the first signal started (closing a cloud allow) runs to the end."""
    terminations = [name for name in _FIRST_SIGNALS if name != "SIGINT"]
    unwound = False

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            _deliver(first)
            raise AssertionError(f"{first} was not delivered as an interrupt")
        finally:
            try:
                for name in terminations:
                    _deliver(name)
            except KeyboardInterrupt:
                pytest.fail("a termination interrupted the unwind")
            unwound = True

    assert unwound


@pytest.mark.windows
@pytest.mark.parametrize("first", _FIRST_SIGNALS)
def test_a_second_ctrl_c_still_interrupts_the_unwind(first: str) -> None:
    """A second Ctrl-C is how the operator abandons a cleanup that hangs, so it is never ignored."""
    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            _deliver(first)
        finally:
            with pytest.raises(KeyboardInterrupt):
                _deliver("SIGINT")


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
def test_a_second_termination_does_not_cut_an_outer_unwind_short() -> None:
    """An inner block's exit must not re-arm the handler while the outer block is still unwinding the first signal."""
    before = signal.getsignal(signal.SIGHUP)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            with termination_as_interrupt():
                os.kill(os.getpid(), signal.SIGINT)
                raise AssertionError("SIGINT was not delivered as an interrupt")
        finally:
            try:
                os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("a termination interrupted the outer unwind")

    assert signal.getsignal(signal.SIGHUP) == before


@posix_only
def test_a_block_opened_during_the_unwind_does_not_re_arm_a_hangup() -> None:
    """A rollback that opens a cloud route opens a block of its own; a hangup in it must not interrupt the rollback."""
    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM was not delivered as an interrupt")
        finally:
            try:
                with termination_as_interrupt():
                    os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("a hangup inside a block the unwind opened interrupted it")


def test_a_block_off_the_main_thread_changes_nothing() -> None:
    """Only the main thread may set handlers; a worker that reaches a cloud route must not raise for it."""
    import threading

    before = signal.getsignal(signal.SIGTERM)
    seen: list[object] = []

    def worker() -> None:
        with termination_as_interrupt(), interrupts_deferred():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == [before]


# -- Deferred ------------------------------------------------------------------------------------


@pytest.mark.windows
@pytest.mark.parametrize("name", _FIRST_SIGNALS)
def test_a_deferred_signal_lets_the_step_finish_and_then_interrupts(name: str) -> None:
    """Closing an allow is one step: an interrupt that lands inside it must wait for the provider call to return."""
    finished = False

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt(), interrupts_deferred():
        _deliver(name)
        finished = True

    assert finished


@pytest.mark.windows
def test_a_deferred_signal_is_delivered_once_and_the_handlers_are_restored() -> None:
    """Several signals inside the step still make one interrupt, and the first sets what follows."""
    before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    raised = 0
    finished = False

    with termination_as_interrupt():
        try:
            with interrupts_deferred():
                _deliver("SIGTERM")
                _deliver("SIGINT")
                finished = True
        except KeyboardInterrupt:
            raised += 1
        # The SIGTERM came first, so the cleanup it starts ignores another one.
        _deliver("SIGTERM")

    assert finished
    assert raised == 1
    assert {number: signal.getsignal(number) for number in before} == before


@pytest.mark.windows
def test_a_step_with_no_signal_returns_normally() -> None:
    with termination_as_interrupt(), interrupts_deferred():
        pass


@posix_only
def test_an_ignored_signal_stays_ignored_through_a_deferred_step() -> None:
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with interrupts_deferred():
            os.kill(os.getpid(), signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)
