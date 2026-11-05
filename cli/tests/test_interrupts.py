"""``termination_as_interrupt``: the signals that end a command unwind it like Ctrl-C, and only inside the block."""

from __future__ import annotations

import io
import os
import signal
import sys

import pytest

from agentworks.interrupts import termination_as_interrupt

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP and os.kill delivery are POSIX")
# SIGBREAK, Ctrl-Break's signal, exists only on Windows.
_CTRL_BREAK: int | None = getattr(signal, "SIGBREAK", None)


@pytest.mark.windows
def test_a_sigterm_inside_the_block_is_an_interrupt_and_the_handler_is_restored() -> None:
    """Windows has SIGTERM handlers but no external delivery to them, so the signal is raised in-process."""
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM was not delivered as an interrupt")

    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.windows
@pytest.mark.skipif(_CTRL_BREAK is None, reason="SIGBREAK is Windows only")
def test_a_ctrl_break_inside_the_block_is_an_interrupt_and_the_handler_is_restored() -> None:
    """Ctrl-Break ends a Windows console command; it must unwind, so a cloud's allow is removed."""
    assert _CTRL_BREAK is not None
    before = signal.getsignal(_CTRL_BREAK)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        signal.raise_signal(_CTRL_BREAK)
        raise AssertionError("SIGBREAK was not delivered as an interrupt")

    assert signal.getsignal(_CTRL_BREAK) == before


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_inside_the_block_is_an_interrupt_and_the_handler_is_restored() -> None:
    before = signal.getsignal(signal.SIGHUP)

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        os.kill(os.getpid(), signal.SIGHUP)
        raise AssertionError("SIGHUP was not delivered as an interrupt")

    assert signal.getsignal(signal.SIGHUP) == before


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_leaves_the_unwind_writing_to_the_null_device() -> None:
    """The hangup's terminal is gone, so the unwind, and every process it starts, writes nowhere."""
    sys.stdout = io.StringIO()  # a stream on no descriptor, as an embedding host may install

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        os.kill(os.getpid(), signal.SIGHUP)

    null = os.stat(os.devnull)
    assert all(os.path.samestat(os.fstat(descriptor), null) for descriptor in (1, 2))
    assert sys.stdout.name == os.devnull
    print("after the hangup")


@posix_only
@pytest.mark.usefixtures("standard_streams")
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
@pytest.mark.usefixtures("standard_streams")
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
@pytest.mark.usefixtures("standard_streams")
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


def test_a_block_off_the_main_thread_changes_nothing() -> None:
    """Only the main thread may set handlers; a worker that reaches a cloud route must not raise for it."""
    import threading

    before = signal.getsignal(signal.SIGTERM)
    seen: list[object] = []

    def worker() -> None:
        with termination_as_interrupt():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == [before]
