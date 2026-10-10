"""Owned PTYs and synthetic clients prove owner-mediated local resize delivery."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event
from typing import Any, cast

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import LocalProcessOwner, LocalProcessTerminal, ResizeNotification
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh import _terminal_relay as relay
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs and SIGWINCH")

_READY = "import os,signal,termios,time,tty; tty.setraw(0); os.write(1,b'READY'); "


@dataclass
class Endpoint:
    master: int
    slave: int
    owned: list[int]


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody) -> Iterator[Endpoint]:
    import termios

    master, slave = os.openpty()
    original_mode = termios.tcgetattr(slave)
    owned: list[int] = []
    native_acquire = PosixTerminal.acquire

    def acquire(terminal: PosixTerminal) -> None:
        native_acquire(terminal)
        owned.extend((terminal.master_fd, terminal.slave_fd))

    monkeypatch.setattr(PosixTerminal, "acquire", acquire)
    try:
        yield Endpoint(master, slave, owned)
    finally:
        assert custody.close(Deadline.after(3))
        try:
            assert termios.tcgetattr(slave) == original_mode
            for fd in owned:
                with pytest.raises(OSError):
                    os.fstat(fd)
        finally:
            # Repair only this fixture before closure if an assertion failed.
            termios.tcsetattr(slave, termios.TCSANOW, original_mode)
            os.close(slave)
            os.close(master)


@pytest.fixture(autouse=True)
def children(monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody) -> Iterator[None]:
    native_launch = subprocess.Popen
    owned: list[subprocess.Popen[bytes]] = []

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        owned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        yield
    finally:
        assert custody.close(Deadline.after(3))
        for process in owned:
            try:
                assert process.returncode is not None
                with pytest.raises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)
                assert all(pipe is None or pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
            finally:
                if process.returncode is None:
                    process.kill()
                    process.wait(timeout=2)


class Source:
    def try_read(self, limit: int) -> bytes:
        return b""


class Diagnostics:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class GeometrySink:
    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.data = bytearray()
        self.changed = False

    def try_write(self, data: memoryview) -> int:
        import termios

        self.data.extend(data)
        if b"READY" in self.data and not self.changed:
            self.changed = True
            termios.tcsetwinsize(self.endpoint.slave, (42, 113))
        return len(data)


@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize("inheritable", [False, True])
def test_repeated_geometry_reaches_actual_child_and_preserves_borrowed_flags(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    blocking: bool,
    inheritable: bool,
    custody: LocalDeliveryCustody,
) -> None:
    import fcntl
    import termios

    os.set_blocking(endpoint.slave, blocking)
    os.set_inheritable(endpoint.slave, inheritable)
    original_flags = fcntl.fcntl(endpoint.slave, fcntl.F_GETFL)
    termios.tcsetwinsize(endpoint.slave, (31, 97))
    output = bytearray()
    changed = 0
    notifications: list[ResizeNotification] = []
    native_notify = LocalProcessOwner.notify_resize

    class Sink:
        def try_write(self, data: memoryview) -> int:
            nonlocal changed
            output.extend(data)
            if changed == 0 and b"READY" in output:
                changed += 1
                termios.tcsetwinsize(endpoint.slave, (42, 113))
            elif changed == 1 and b"42,113;" in output:
                changed += 1
                termios.tcsetwinsize(endpoint.slave, (58, 144))
            return len(data)

    def notify(owner: LocalProcessOwner, deadline: ProcessDeadline) -> ResizeNotification:
        outcome = native_notify(owner, deadline)
        notifications.append(outcome)
        return outcome

    monkeypatch.setattr(LocalProcessOwner, "notify_resize", notify)
    sink = Sink()
    io = CarrierIO(TerminalInput(endpoint.slave, endpoint.slave, "fixture", Source()), SinkOutput(sink, Diagnostics()))
    code = """
import os,signal,termios,time,tty
tty.setraw(0)
observed=[]
def resize(*args):
    observed.append(termios.tcgetwinsize(0))
    rows,columns=observed[-1]
    os.write(1,f'{rows},{columns};'.encode())
signal.signal(signal.SIGWINCH,resize)
os.write(1,b'READY')
until=time.monotonic()+5
while len(observed)<2 and time.monotonic()<until: time.sleep(.005)
assert observed==[(42,113),(58,144)]
"""
    result = relay.run_terminal_relay_candidate(
        [sys.executable, "-c", code], io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.started and result.exit_status == 0 and result.failure is None
    assert bytes(output) == b"READY42,113;58,144;"
    assert notifications == [ResizeNotification.REQUESTED, ResizeNotification.REQUESTED]
    assert fcntl.fcntl(endpoint.slave, fcntl.F_GETFL) == original_flags
    assert os.get_inheritable(endpoint.slave) is inheritable


@pytest.mark.parametrize("notification", [ResizeNotification.NOT_SENT, ResizeNotification.UNKNOWN])
@pytest.mark.parametrize("seconds", [None, 0.5])
def test_notification_expiry_uses_finite_original_bound_and_safe_failure(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    notification: ResizeNotification,
    seconds: float | None,
    custody: LocalDeliveryCustody,
) -> None:
    # An unbounded operation still grants only a finite local notification wait.
    # A bounded operation's absolute expiry wins over a longer local allowance.
    allowance = 0.1 if seconds is None else 2.0
    monkeypatch.setattr(relay, "_RESIZE_SECONDS", allowance)
    operation = Deadline.after(seconds)
    budgets: list[ProcessDeadline] = []

    def notify(owner: LocalProcessOwner, deadline: ProcessDeadline) -> ResizeNotification:
        budgets.append(deadline)
        assert deadline.expires_at is not None
        now = time.monotonic()
        assert deadline.expires_at <= now + allowance
        if operation.expires_at is not None:
            assert deadline.expires_at == operation.expires_at
        for fd in endpoint.owned:
            os.fstat(fd)
        while not deadline.expired:
            time.sleep(min(0.005, deadline.remaining() or 0))
        return notification

    monkeypatch.setattr(LocalProcessOwner, "notify_resize", notify)
    sink = GeometrySink(endpoint)
    io = CarrierIO(TerminalInput(endpoint.slave, endpoint.slave, "fixture", Source()), SinkOutput(sink, Diagnostics()))
    started = time.monotonic()
    result = relay.run_terminal_relay_candidate(
        [sys.executable, "-c", _READY + "time.sleep(5)"], io=io, deadline=operation, custody=custody
    )
    assert result.started and result.exit_status is None and len(budgets) == 1
    expected = (
        Failure.DEADLINE if seconds is not None and notification is ResizeNotification.NOT_SENT else Failure.OBSERVATION
    )
    assert result.failure is expected
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("notification", [ResizeNotification.NOT_SENT, ResizeNotification.UNKNOWN])
def test_exit_racing_notification_preserves_actual_completion(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, notification: ResizeNotification, custody: LocalDeliveryCustody
) -> None:
    sink = GeometrySink(endpoint)
    native_notify = LocalProcessOwner.notify_resize
    outcomes: list[ResizeNotification] = []

    class ExitSource:
        sent = False

        def try_read(self, limit: int) -> bytes | None:
            if not sink.changed:
                return None
            if not self.sent:
                self.sent = True
                return b"x"
            return b""

    def notify(owner: LocalProcessOwner, deadline: ProcessDeadline) -> ResizeNotification:
        while owner.snapshot().exit_status is None and not deadline.expired:
            time.sleep(0.005)
        assert owner.snapshot().exit_status == 17
        if notification is ResizeNotification.NOT_SENT:
            outcome = native_notify(owner, deadline)
            assert outcome is ResizeNotification.NOT_SENT
        else:
            outcome = notification
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(LocalProcessOwner, "notify_resize", notify)
    io = CarrierIO(
        TerminalInput(endpoint.slave, endpoint.slave, "fixture", ExitSource()), SinkOutput(sink, Diagnostics())
    )
    code = _READY + "assert os.read(0,1)==b'x'; os.write(1,b'TAIL'); raise SystemExit(17)"
    result = relay.run_terminal_relay_candidate(
        [sys.executable, "-c", code], io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.started and result.exit_status == result.local_status == 17
    assert outcomes == [notification]
    assert result.failure is (None if notification is ResizeNotification.NOT_SENT else Failure.OBSERVATION)
    if notification is ResizeNotification.NOT_SENT:
        assert bytes(sink.data) == b"READYTAIL"
        assert result.stdout.complete and result.stderr.complete


def test_late_requested_notification_does_not_hide_operation_expiry(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    native_notify = LocalProcessOwner.notify_resize
    operation = Deadline.after(0.5)
    monkeypatch.setattr(relay, "_RESIZE_SECONDS", 2.0)
    accepted: list[ResizeNotification] = []

    def notify(owner: LocalProcessOwner, deadline: ProcessDeadline) -> ResizeNotification:
        assert deadline.expires_at == operation.expires_at
        outcome = native_notify(owner, deadline)
        assert outcome is ResizeNotification.REQUESTED
        accepted.append(outcome)
        while not deadline.expired:
            time.sleep(min(0.005, deadline.remaining() or 0))
        return outcome

    monkeypatch.setattr(LocalProcessOwner, "notify_resize", notify)
    sink = GeometrySink(endpoint)
    io = CarrierIO(TerminalInput(endpoint.slave, endpoint.slave, "fixture", Source()), SinkOutput(sink, Diagnostics()))
    result = relay.run_terminal_relay_candidate(
        [sys.executable, "-c", _READY + "time.sleep(5)"], io=io, deadline=operation, custody=custody
    )
    assert accepted == [ResizeNotification.REQUESTED]
    assert result.started and result.exit_status is None and result.failure is Failure.DEADLINE


@pytest.mark.parametrize("notification", [ResizeNotification.REQUESTED, ResizeNotification.UNKNOWN])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_resize_control_retains_worker_and_primary_through_owner_settlement(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    notification: ResizeNotification,
    cleanup_fails: bool,
    custody: LocalDeliveryCustody,
) -> None:
    import termios

    saved_mode = termios.tcgetattr(endpoint.slave)
    primary = KeyboardInterrupt("resize-control")
    secondary = SystemExit("later-caller-control")
    closing, release_cleanup, settled, restored = Event(), Event(), Event(), Event()
    native_notify, native_close = LocalProcessOwner.notify_resize, LocalProcessOwner.close_bounded
    native_release = PosixTerminal.release
    outcomes: list[ResizeNotification] = []

    def notify(owner: LocalProcessOwner, deadline: ProcessDeadline) -> ResizeNotification:
        if notification is ResizeNotification.REQUESTED:
            outcome = native_notify(owner, deadline)
            assert outcome is ResizeNotification.REQUESTED
            outcomes.append(outcome)
            raise primary
        outcomes.append(notification)
        return notification

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> LocalProcessTerminal:
        closing.set()
        assert release_cleanup.wait(2)
        assert not restored.is_set()
        assert termios.tcgetattr(endpoint.slave) != saved_mode
        for fd in endpoint.owned:
            os.fstat(fd)
        result = native_close(owner, deadline)
        assert result is not None and result.cleaned
        settled.set()
        if cleanup_fails:
            raise OSError("private-resize-cleanup-canary")
        return result

    def release(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        assert settled.is_set()
        result = native_release(terminal)
        restored.set()
        return result

    monkeypatch.setattr(LocalProcessOwner, "notify_resize", notify)
    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    monkeypatch.setattr(PosixTerminal, "release", release)
    sink = GeometrySink(endpoint)
    io = CarrierIO(TerminalInput(endpoint.slave, endpoint.slave, "fixture", Source()), SinkOutput(sink, Diagnostics()))
    attempt = relay._Attempt([sys.executable, "-c", _READY + "time.sleep(5)"], io, Deadline.after(3), custody=custody)
    native_wait = attempt._done.wait
    calls = 0

    def wait(timeout: float | None = None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert closing.wait(2)
            assert not attempt._done.is_set() and not restored.is_set()
            raise secondary if notification is ResizeNotification.REQUESTED else primary
        if calls == 2:
            raise secondary
        release_cleanup.set()
        return native_wait(timeout)

    monkeypatch.setattr(attempt._done, "wait", wait)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            attempt.run()
        assert caught.value is primary and attempt._done.is_set()
        assert outcomes == [notification]
        assert settled.is_set() and restored.is_set()
        assert termios.tcgetattr(endpoint.slave) == saved_mode
        assert attempt._result is not None
        assert attempt._result.failure is Failure.OBSERVATION
        assert bool(getattr(primary, "__notes__", []))
        assert "private-resize-cleanup-canary" not in repr(attempt._result)
    finally:
        release_cleanup.set()
