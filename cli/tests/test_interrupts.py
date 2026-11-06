"""``termination_as_interrupt``: the signals that end a command unwind it like Ctrl-C, and only inside the block."""

from __future__ import annotations

import io
import os
import signal
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.interrupts import termination_as_interrupt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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


@pytest.fixture
def terminal() -> Iterator[int]:
    """The far side of a pseudo-terminal, standing in for the terminal a hangup takes away."""
    import pty

    controller, terminal = pty.openpty()
    try:
        yield terminal
    finally:
        os.close(terminal)
        os.close(controller)


def _hang_up() -> None:
    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        os.kill(os.getpid(), signal.SIGHUP)


def _is_null(descriptor: int) -> bool:
    return os.path.samestat(os.fstat(descriptor), os.stat(os.devnull))


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_points_the_terminal_descriptors_at_the_null_device(terminal: int) -> None:
    """The hangup's terminal is gone, so the unwind, and every process it starts, writes nowhere."""
    os.dup2(terminal, 1)
    os.dup2(terminal, 2)

    _hang_up()

    assert _is_null(1)
    assert _is_null(2)


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_redirects_a_terminal_that_has_already_hung_up() -> None:
    """A hung-up terminal no longer answers ``isatty``, but every write to it still fails."""
    import pty

    controller, terminal = pty.openpty()
    os.dup2(terminal, 1)
    os.dup2(terminal, 2)
    os.close(terminal)
    os.close(controller)

    _hang_up()

    assert _is_null(1)
    assert _is_null(2)


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_redirects_a_pipe_whose_reader_has_gone() -> None:
    """``agw vm create 2>&1 | tee create.log``: closing the terminal ends ``tee`` too, so the pipe fails every write."""
    reader, writer = os.pipe()
    os.dup2(writer, 1)
    os.dup2(writer, 2)
    os.close(writer)
    os.close(reader)

    _hang_up()

    assert _is_null(1)
    assert _is_null(2)


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_leaves_output_to_a_file_where_it_goes(terminal: int, tmp_path: Path) -> None:
    log = os.open(tmp_path / "out.log", os.O_WRONLY | os.O_CREAT)
    os.dup2(log, 1)
    os.close(log)
    os.dup2(terminal, 2)

    _hang_up()

    assert os.path.samestat(os.fstat(1), os.stat(tmp_path / "out.log"))
    assert _is_null(2)


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_leaves_a_closed_descriptor_closed(terminal: int) -> None:
    os.close(1)
    os.dup2(terminal, 2)

    _hang_up()

    with pytest.raises(OSError):
        os.fstat(1)
    assert _is_null(2)


@posix_only
@pytest.mark.usefixtures("standard_streams")
def test_a_hangup_replaces_a_stream_on_another_terminal_and_keeps_one_on_none(terminal: int) -> None:
    on_terminal = open(terminal, "w", encoding="utf-8", closefd=False)  # noqa: SIM115 - the fixture puts the stream back
    on_nothing = io.StringIO()
    sys.stdout, sys.stderr = on_terminal, on_nothing

    _hang_up()

    assert sys.stdout is not on_terminal
    assert sys.stdout.name == os.devnull
    assert sys.stderr is on_nothing
    on_terminal.close()


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
def test_a_hangup_after_another_termination_still_redirects_the_unwind() -> None:
    """``SIGTERM`` starts a rollback, then the terminal closes: the rollback's writes must not fail on it."""
    import pty

    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    unwound = False

    with pytest.raises(KeyboardInterrupt), termination_as_interrupt():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM was not delivered as an interrupt")
        finally:
            controller, terminal = pty.openpty()
            os.dup2(terminal, 1)
            os.dup2(terminal, 2)
            os.close(terminal)
            os.close(controller)
            try:
                os.kill(os.getpid(), signal.SIGHUP)
            except KeyboardInterrupt:
                pytest.fail("a hangup interrupted the unwind")
            os.write(1, b"rolling back\n")
            os.write(2, b"rolling back\n")
            unwound = True

    assert unwound
    assert _is_null(1)
    assert _is_null(2)
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


@posix_only
@pytest.mark.usefixtures("standard_streams")
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
        with termination_as_interrupt():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == [before]
