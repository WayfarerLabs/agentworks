"""Acquisition failure retains cleanup facts across the native/relay boundary."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh._terminal_posix import AcquisitionCleanupFailure
from agentworks.execution.carriers.ssh._terminal_relay import _Attempt
from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode

if TYPE_CHECKING:
    from agentworks.execution.carriers.ssh._terminal_posix import _TerminalMode

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs")


@pytest.fixture
def endpoint() -> Iterator[tuple[int, int]]:
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize("primary_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_boundary", ["restore", "close", None])
def test_acquisition_cleanup_uncertainty_is_disclosed_safely(
    endpoint: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
    cleanup_boundary: str | None,
) -> None:
    import termios

    borrowed = endpoint[1]
    original_mode = termios.tcgetattr(borrowed)
    native_setattr, native_close, native_openpty = termios.tcsetattr, os.close, os.openpty
    primary = primary_type("primary-control")
    raw_cleanup = OSError("private-native-cleanup-canary")
    owned: list[int] = []
    calls = 0
    close_failed: int | None = None
    launch_called = False

    class Source:
        def try_read(self, limit: int) -> bytes | None:
            pytest.fail("Acquisition failure must not use preparation input")

    class Sink:
        def try_write(self, data: memoryview) -> int:
            pytest.fail("Acquisition failure must not deliver output")

    def openpty() -> tuple[int, int]:
        pair = native_openpty()
        owned.extend(pair)
        return pair

    def setattr(fd: int, when: int, mode: _TerminalMode) -> None:
        nonlocal calls
        if fd != borrowed:
            native_setattr(fd, when, mode)
            return
        calls += 1
        if calls == 1:
            native_setattr(fd, when, mode)
            raise primary
        if cleanup_boundary == "restore":
            raise raw_cleanup
        native_setattr(fd, when, mode)

    def close(fd: int) -> None:
        nonlocal close_failed
        if cleanup_boundary == "close" and fd == owned[0]:
            close_failed = fd
            raise raw_cleanup
        native_close(fd)

    def forbidden(*args: object) -> None:
        nonlocal launch_called
        launch_called = True
        pytest.fail("Failed acquisition dispatched a process")

    io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
    custody = LocalDeliveryCustody()
    attempt = _Attempt([sys.executable, "-c", "pass"], io, Deadline.after(3), custody=custody)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "openpty", openpty)
            patch.setattr(os, "close", close)
            patch.setattr(termios, "tcsetattr", setattr)
            patch.setattr(LocalProcessOwner, "start", forbidden)
            if issubclass(primary_type, Exception):
                result = attempt.run()
                assert not result.started
                assert result.failure is (Failure.OBSERVATION if cleanup_boundary else Failure.DISPATCH)
                assert "private-native-cleanup-canary" not in repr(result)
                if cleanup_boundary:
                    assert isinstance(primary.__cause__, AcquisitionCleanupFailure)
                    assert primary.__cause__.exceptions == (raw_cleanup,)
            else:
                with pytest.raises(primary_type) as raised:
                    attempt.run()
                assert raised.value is primary
                assert attempt._result is not None and not attempt._result.started
                assert attempt._result.failure is (Failure.OBSERVATION if cleanup_boundary else None)
                notes = getattr(primary, "__notes__", [])
                assert bool(notes) is bool(cleanup_boundary)
                rendered = "".join(traceback.format_exception(primary))
                assert "private-native-cleanup-canary" not in rendered
                assert primary.__cause__ is None
        assert custody.settled is (cleanup_boundary is None)
        assert not launch_called
        assert attempt._done.is_set() and len(owned) == 2
        if cleanup_boundary == "restore":
            assert termios.tcgetattr(borrowed) != original_mode
        else:
            assert_preserved_terminal_mode(termios.tcgetattr(borrowed), original_mode)
        for fd in owned:
            if fd == close_failed:
                os.fstat(fd)
            else:
                with pytest.raises(OSError):
                    os.fstat(fd)
    finally:
        # Faults precede restoration/close effects. Repair only these own fixtures
        # after removing injections, then independently verify native cleanup.
        native_setattr(borrowed, termios.TCSANOW, original_mode)
        for fd in owned:
            try:
                os.fstat(fd)
            except OSError:
                continue
            native_close(fd)
        assert_preserved_terminal_mode(termios.tcgetattr(borrowed), original_mode)
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)


def test_pre_effect_acquisition_refusal_remains_dispatch_failure(
    endpoint: tuple[int, int],
) -> None:
    class Source:
        def try_read(self, limit: int) -> bytes | None:
            pytest.fail("Invalid native descriptor used preparation input")

    class Sink:
        def try_write(self, data: memoryview) -> int:
            pytest.fail("Invalid native descriptor delivered output")

    read_fd, write_fd = os.pipe()
    try:
        io = CarrierIO(TerminalInput(read_fd, endpoint[1], "fixture", Source()), SinkOutput(Sink(), Sink()))
        custody = LocalDeliveryCustody()
        result = _Attempt([sys.executable, "-c", "pass"], io, Deadline.after(3), custody=custody).run()
        assert not result.started and result.failure is Failure.DISPATCH
        os.fstat(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)
