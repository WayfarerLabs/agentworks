"""Owned local PTYs prove terminal resource behavior without SSH or operator state."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from collections.abc import Callable, Iterator
from functools import wraps
from pathlib import Path
from threading import Thread, current_thread

import pytest

from agentworks.execution.carriers.ssh._terminal_posix import AcquisitionCleanupFailure, PosixTerminal

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX terminal descriptors")


def _native_worker[**P](test: Callable[P, None]) -> Callable[P, None]:
    """Keep one owned worker through a native case, returning its exception intact."""

    @wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        errors: list[BaseException] = []

        def work() -> None:
            try:
                test(*args, **kwargs)
            except BaseException as error:
                errors.append(error)

        worker = Thread(target=work)
        worker.start()
        worker.join()
        assert not worker.is_alive()
        if errors:
            raise errors[0]

    return run


@pytest.fixture
def endpoint() -> Iterator[tuple[int, int]]:
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        os.close(slave)
        os.close(master)


def _flags(fd: int) -> tuple[int, bool]:
    import fcntl

    return fcntl.fcntl(fd, fcntl.F_GETFL), os.get_inheritable(fd)


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("borrowed_nonblocking,inheritable", [(False, False), (True, True)])
@_native_worker
def test_modes_geometry_flags_and_borrowed_lifetime(
    endpoint: tuple[int, int], borrowed_nonblocking: bool, inheritable: bool
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    mode[3] &= ~termios.ECHO
    mode[6][termios.VINTR] = b"\x07"
    mode[6][termios.VMIN] = b"\x07"
    mode[6][termios.VTIME] = b"\x03"
    termios.tcsetattr(borrowed, termios.TCSANOW, mode)
    termios.tcsetwinsize(borrowed, (31, 97))
    os.set_blocking(borrowed, not borrowed_nonblocking)
    os.set_inheritable(borrowed, inheritable)
    flags = _flags(borrowed)
    terminal = PosixTerminal(borrowed, borrowed, current_thread())
    terminal.acquire()
    master, slave = terminal.master_fd, terminal.slave_fd
    try:
        assert termios.tcgetattr(slave) == mode
        assert termios.tcgetwinsize(slave) == (31, 97)
        raw = termios.tcgetattr(borrowed)
        assert not raw[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
        assert _flags(borrowed) == flags
        assert not os.get_blocking(master)
        assert os.get_blocking(slave)
        assert not terminal.refresh_dimensions()
        termios.tcsetwinsize(borrowed, (42, 113))
        assert terminal.refresh_dimensions()
        assert termios.tcgetwinsize(slave) == (42, 113)
    finally:
        assert terminal.release() == ()
    assert termios.tcgetattr(borrowed) == mode
    assert _flags(borrowed) == flags
    os.fstat(borrowed)
    _assert_closed(master)
    _assert_closed(slave)
    assert terminal.release() == ()
    with pytest.raises(RuntimeError):
        _ = terminal.master_fd
    with pytest.raises(RuntimeError):
        _ = terminal.slave_fd


@_native_worker
def test_acquisition_preserves_queued_input(endpoint: tuple[int, int]) -> None:
    import termios

    master, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    # Queue a complete canonical line before acquisition. Processing that already
    # happened is not reversible; acquisition must not discard the resulting bytes.
    os.write(master, b"early keyboard\n")
    assert select.select([borrowed], [], [], 1)[0] == [borrowed]
    terminal = PosixTerminal(borrowed, borrowed, current_thread())
    terminal.acquire()
    try:
        assert select.select([borrowed], [], [], 1)[0] == [borrowed]
        assert os.read(borrowed, 100) == b"early keyboard\n"
        os.write(master, b"raw\x07\r")
        assert select.select([borrowed], [], [], 1)[0] == [borrowed]
        assert os.read(borrowed, 100) == b"raw\x07\r"
        os.write(master, b"after restore\n")
        assert select.select([borrowed], [], [], 1)[0] == [borrowed]
    finally:
        assert terminal.release() == ()
    assert termios.tcgetattr(borrowed) == mode
    assert select.select([borrowed], [], [], 1)[0] == [borrowed]
    assert os.read(borrowed, 100) == b"after restore\n"


@pytest.mark.parametrize(
    "bad_endpoint,bad_side",
    [(kind, side) for kind in ("negative", "closed", "pipe", "file") for side in ("input", "output")]
    + [("write_input", "input")],
)
@_native_worker
def test_bad_fd_refuses_without_changing_terminal(
    endpoint: tuple[int, int], bad_endpoint: str, bad_side: str, tmp_path: Path
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    flags = _flags(borrowed)
    opened: list[int] = []
    input_fd = output_fd = borrowed
    if bad_endpoint == "negative":
        input_fd = -1
    elif bad_endpoint == "closed":
        input_fd = os.dup(borrowed)
        os.close(input_fd)
    elif bad_endpoint == "pipe":
        input_fd, write_fd = os.pipe()
        opened.extend((input_fd, write_fd))
    elif bad_endpoint == "file":
        input_fd = os.open(tmp_path / "ordinary", os.O_CREAT | os.O_RDWR, 0o600)
        opened.append(input_fd)
    elif bad_endpoint == "write_input":
        input_fd = os.open(os.ttyname(borrowed), os.O_WRONLY | os.O_NOCTTY)
        opened.append(input_fd)
    if bad_side == "output":
        input_fd, output_fd = borrowed, input_fd
    try:
        with pytest.raises((OSError, ValueError, termios.error)):
            resource = PosixTerminal(input_fd, output_fd, current_thread())
            resource.acquire()
        assert termios.tcgetattr(borrowed) == mode
        assert _flags(borrowed) == flags
        for fd in opened:
            os.fstat(fd)
    finally:
        for fd in opened:
            os.close(fd)


@_native_worker
def test_distinct_read_only_output_supplies_geometry(endpoint: tuple[int, int]) -> None:
    import termios

    _, borrowed = endpoint
    output_master, output_slave = os.openpty()
    output_fd: int | None = None
    try:
        output_fd = os.open(os.ttyname(output_slave), os.O_RDONLY | os.O_NOCTTY)
        flags = _flags(output_fd)
        termios.tcsetwinsize(borrowed, (31, 97))
        termios.tcsetwinsize(output_slave, (37, 121))
        terminal = PosixTerminal(borrowed, output_fd, current_thread())
        terminal.acquire()
        try:
            assert termios.tcgetwinsize(terminal.slave_fd) == (37, 121)
            assert _flags(output_fd) == flags
            termios.tcsetwinsize(output_slave, (41, 132))
            assert terminal.refresh_dimensions()
            assert termios.tcgetwinsize(terminal.slave_fd) == (41, 132)
            assert termios.tcgetwinsize(borrowed) == (31, 97)
        finally:
            assert terminal.release() == ()
        os.fstat(output_fd)
        assert _flags(output_fd) == flags
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(output_slave)
        os.close(output_master)


@pytest.mark.parametrize("stage", ["allocation", "copy", "raw"])
@pytest.mark.parametrize("failure", [OSError("native failure"), KeyboardInterrupt(), SystemExit(17)])
@_native_worker
def test_acquisition_failure_restores_and_closes(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, stage: str, failure: BaseException
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    flags = _flags(borrowed)
    owned: list[int] = []
    openpty = os.openpty
    set_mode = termios.tcsetattr

    def allocate() -> tuple[int, int]:
        if stage == "allocation":
            raise failure
        pair = openpty()
        owned.extend(pair)
        return pair

    def change(fd: int, when: int, attributes: list) -> None:
        set_mode(fd, when, attributes)
        if (stage == "copy" and fd != borrowed) or (stage == "raw" and attributes != mode):
            raise failure

    monkeypatch.setattr(os, "openpty", allocate)
    monkeypatch.setattr(termios, "tcsetattr", change)
    with pytest.raises(type(failure)) as caught:
        resource = PosixTerminal(borrowed, borrowed, current_thread())
        resource.acquire()
    assert caught.value is failure
    assert termios.tcgetattr(borrowed) == mode
    assert _flags(borrowed) == flags
    os.fstat(borrowed)
    for fd in owned:
        _assert_closed(fd)


@pytest.mark.parametrize("failure", [OSError("restore failed"), KeyboardInterrupt(), SystemExit(23)])
@_native_worker
def test_release_reports_restore_failure_and_still_closes(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    terminal = PosixTerminal(borrowed, borrowed, current_thread())
    terminal.acquire()
    owned = (terminal.master_fd, terminal.slave_fd)
    set_mode = termios.tcsetattr

    def fail_restore(*args: object) -> None:
        raise failure

    monkeypatch.setattr(termios, "tcsetattr", fail_restore)
    try:
        errors = terminal.release()
        assert errors == (failure,)
        assert terminal.release() is errors
        assert termios.tcgetattr(borrowed) != mode
        os.fstat(borrowed)
        for fd in owned:
            _assert_closed(fd)
    finally:
        set_mode(borrowed, termios.TCSANOW, mode)


@_native_worker
def test_acquisition_retains_primary_and_cleanup_failures(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    set_mode = termios.tcsetattr
    primary = KeyboardInterrupt()
    prior_cause = ValueError("earlier failure")
    prior_context = RuntimeError("earlier context")
    primary.__cause__ = prior_cause
    primary.__context__ = prior_context
    cleanup = OSError("restore failed")
    owned: list[int] = []
    openpty = os.openpty

    def allocate() -> tuple[int, int]:
        pair = openpty()
        owned.extend(pair)
        return pair

    def change(fd: int, when: int, attributes: list) -> None:
        if fd != borrowed:
            set_mode(fd, when, attributes)
        elif attributes == mode:
            raise cleanup
        else:
            set_mode(fd, when, attributes)
            raise primary

    monkeypatch.setattr(os, "openpty", allocate)
    monkeypatch.setattr(termios, "tcsetattr", change)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            resource = PosixTerminal(borrowed, borrowed, current_thread())
            resource.acquire()
        assert caught.value is primary
        assert isinstance(caught.value.__cause__, AcquisitionCleanupFailure)
        assert caught.value.__cause__.exceptions == (cleanup,)
        assert caught.value.__cause__.__cause__ is prior_cause
        assert caught.value.__context__ is prior_context
        for fd in owned:
            _assert_closed(fd)
    finally:
        set_mode(borrowed, termios.TCSANOW, mode)


@pytest.mark.parametrize("failure", [OSError("close failed"), KeyboardInterrupt()])
@_native_worker
def test_uncertain_close_is_reported_without_retry(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    _, borrowed = endpoint
    terminal = PosixTerminal(borrowed, borrowed, current_thread())
    terminal.acquire()
    master, slave = terminal.master_fd, terminal.slave_fd
    close = os.close
    calls: list[int] = []

    def close_then_fail(fd: int) -> None:
        calls.append(fd)
        close(fd)
        if fd == master:
            raise failure

    # An error after close can leave that number reused by an unrelated owner.
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close_then_fail)
        errors = terminal.release()
        assert errors == (failure,)
        assert terminal.release() is errors
        assert calls == [master, slave]
    _assert_closed(master)
    _assert_closed(slave)
    os.fstat(borrowed)


def test_main_thread_acquisition_refuses_before_native_work(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    flags = _flags(borrowed)

    def unexpected_native(*args: object) -> None:
        raise AssertionError("Native admission ran on the main thread")

    with monkeypatch.context() as patch:
        patch.setattr(termios, "tcgetattr", unexpected_native)
        patch.setattr(termios, "tcgetwinsize", unexpected_native)
        patch.setattr(os, "openpty", unexpected_native)
        with pytest.raises(RuntimeError):
            resource = PosixTerminal(borrowed, borrowed, current_thread())
            resource.acquire()
    assert termios.tcgetattr(borrowed) == mode
    assert _flags(borrowed) == flags
    os.fstat(borrowed)


@_native_worker
def test_wrong_owner_refuses_resize_and_release_before_effects(endpoint: tuple[int, int]) -> None:
    import termios

    _, borrowed = endpoint
    mode = termios.tcgetattr(borrowed)
    termios.tcsetwinsize(borrowed, (31, 97))
    terminal = PosixTerminal(borrowed, borrowed, current_thread())
    terminal.acquire()
    master, slave = terminal.master_fd, terminal.slave_fd
    raw_mode = termios.tcgetattr(borrowed)

    @_native_worker
    def wrong_owner() -> None:
        with pytest.raises(RuntimeError):
            terminal.refresh_dimensions()
        with pytest.raises(RuntimeError):
            terminal.release()

    try:
        termios.tcsetwinsize(borrowed, (42, 113))
        wrong_owner()
        assert termios.tcgetattr(borrowed) == raw_mode
        assert termios.tcgetwinsize(slave) == (31, 97)
        os.fstat(master)
        os.fstat(slave)
        assert terminal.refresh_dimensions()
        assert termios.tcgetwinsize(slave) == (42, 113)
    finally:
        assert terminal.release() == ()
    assert termios.tcgetattr(borrowed) == mode
    _assert_closed(master)
    _assert_closed(slave)


def test_main_sigint_keeps_native_worker_available_for_cleanup() -> None:
    # Own the process as well as its PTY, so the real signal cannot reach pytest
    # or an operator terminal. The worker stays retained until cleanup settles.
    code = """
import os
import signal
import termios
from threading import Event, Thread, current_thread
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal

master, borrowed = os.openpty()
mode = termios.tcgetattr(borrowed)
ready, stop = Event(), Event()
resources, errors, releases = [], [], []

def native_worker():
    terminal = None
    try:
        terminal = PosixTerminal(borrowed, borrowed, current_thread())
        terminal.acquire()
        resources.append((terminal, terminal.master_fd, terminal.slave_fd))
        ready.set()
        assert stop.wait(3)
    except BaseException as error:
        errors.append(error)
    finally:
        if terminal is not None:
            releases.append(terminal.release())
        ready.set()

worker = Thread(target=native_worker)
try:
    worker.start()
    try:
        assert ready.wait(3)
        assert not errors
        assert termios.tcgetattr(borrowed) != mode
        terminal, owned_master, owned_slave = resources[0]
        for operation in (terminal.refresh_dimensions, terminal.release):
            try:
                operation()
            except RuntimeError:
                pass
            else:
                raise AssertionError('Main thread mutated a worker resource')
        signal.raise_signal(signal.SIGINT)
        raise AssertionError('SIGINT did not interrupt the main thread')
    except KeyboardInterrupt:
        assert worker.is_alive()
        assert termios.tcgetattr(borrowed) != mode
    finally:
        stop.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert releases == [()]
    assert termios.tcgetattr(borrowed) == mode
    os.fstat(borrowed)
    for fd in (owned_master, owned_slave):
        try:
            os.fstat(fd)
        except OSError:
            pass
        else:
            raise AssertionError('Owned descriptor remained open')
finally:
    stop.set()
    worker.join(3)
    os.close(borrowed)
    os.close(master)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
