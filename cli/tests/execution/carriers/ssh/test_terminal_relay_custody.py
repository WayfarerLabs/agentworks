"""Finite caller waits retain the exact worker, terminal and native owner."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from threading import Event, Thread, current_thread
from typing import Any, cast

import pytest

from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import LocalProcessOwner, LocalProcessTerminal
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from agentworks.execution.carriers.ssh._terminal_relay import _Attempt
from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode, with_terminal_mode_assertion

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs")


@pytest.fixture
def endpoint(custody: LocalDeliveryCustody) -> Iterator[tuple[int, int]]:
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        assert custody.close(Deadline.after(3))
        os.close(slave)
        os.close(master)


class Source:
    def try_read(self, limit: int) -> bytes:
        return b""


class Sink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


@pytest.mark.parametrize("boundary", ["acquisition", "construction", "cleanup"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_pending_worker_retains_borrowed_terminal_until_fresh_retry(
    endpoint: tuple[int, int],
    custody: LocalDeliveryCustody,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    interrupt: bool,
) -> None:
    import fcntl
    import termios

    borrowed = endpoint[1]
    mode = termios.tcgetattr(borrowed)
    flags = fcntl.fcntl(borrowed, fcntl.F_GETFL), os.get_inheritable(borrowed)
    entered, release = Event(), Event()
    children: list[subprocess.Popen[bytes]] = []
    closers: list[Thread] = []
    releasers: list[tuple[PosixTerminal, Thread]] = []
    held_fds: list[int] = []
    native_acquire = PosixTerminal.acquire
    native_release = PosixTerminal.release
    native_launch = subprocess.Popen
    native_close = LocalProcessOwner.close_bounded
    primary = KeyboardInterrupt("owned-caller-control")

    def pause() -> None:
        entered.set()
        assert release.wait(3)

    def acquire(terminal: PosixTerminal) -> None:
        if boundary == "acquisition":
            pause()
        native_acquire(terminal)
        held_fds.extend((terminal.master_fd, terminal.slave_fd))

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        if boundary == "construction":
            # Pending Popen still borrows the original owned slave descriptor.
            os.fstat(kwargs["stdin"])
            pause()
            os.fstat(kwargs["stdin"])
        child = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        children.append(child)
        return child

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> LocalProcessTerminal | None:
        closers.append(current_thread())
        if boundary == "cleanup":
            pause()
        return native_close(owner, deadline)

    def restore(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        assert release.is_set()
        assert all(child.returncode is not None for child in children)
        releasers.append((terminal, current_thread()))
        return native_release(terminal)

    monkeypatch.setattr(PosixTerminal, "acquire", acquire)
    monkeypatch.setattr(PosixTerminal, "release", restore)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
    deadline = Deadline.after(0.1 if not interrupt else 2)
    attempt = _Attempt([sys.executable, "-c", "pass"], io, deadline, custody)
    resource = attempt._terminal
    owner = attempt._owner
    assert custody._owner is owner and custody._cleanup is attempt
    assert resource._owner_thread is attempt._worker
    native_wait = attempt._operation_done.wait
    native_cleanup_wait = attempt._done.wait

    if interrupt:

        def wait(timeout: float | None = None) -> bool:
            assert entered.wait(2)
            raise primary

        # Cleanup entry follows operation completion, so interrupt its caller
        # cleanup wait instead when the held boundary is native closure.
        event = attempt._done if boundary == "cleanup" else attempt._operation_done
        monkeypatch.setattr(event, "wait", wait)
    try:
        started = time.monotonic()
        if interrupt:
            with pytest.raises(KeyboardInterrupt) as raised:
                attempt.run()
            assert raised.value is primary
            assert primary.__cause__ is None and primary.__notes__
        else:
            result = attempt.run()
            assert result.failure is (Failure.OBSERVATION if boundary == "cleanup" else Failure.DEADLINE)
            assert result.started is (boundary != "acquisition")
            if boundary == "cleanup":
                assert result.exit_status == 0
        monkeypatch.setattr(attempt._operation_done, "wait", native_wait)
        monkeypatch.setattr(attempt._done, "wait", native_cleanup_wait)
        assert time.monotonic() - started < 1
        assert entered.is_set() and not attempt._done.is_set()
        assert not custody.settled
        assert not custody.close(Deadline.after(0.02))
        with pytest.raises(StateError):
            custody.begin_process()
        assert not releasers
        assert attempt._terminal is resource and custody._owner is owner
        for fd in (*endpoint, *held_fds):
            os.fstat(fd)
        assert (termios.tcgetattr(borrowed) == mode) is (boundary == "acquisition")
        assert (fcntl.fcntl(borrowed, fcntl.F_GETFL), os.get_inheritable(borrowed)) == flags
    finally:
        release.set()
        monkeypatch.setattr(attempt._operation_done, "wait", native_wait)
        monkeypatch.setattr(attempt._done, "wait", native_cleanup_wait)
        assert custody.close(Deadline.after(3))
    assert custody.settled and attempt._done.is_set()
    assert releasers == [(resource, attempt._worker)]
    assert closers and all(thread is attempt._worker for thread in closers)
    assert_preserved_terminal_mode(termios.tcgetattr(borrowed), mode)
    assert (fcntl.fcntl(borrowed, fcntl.F_GETFL), os.get_inheritable(borrowed)) == flags
    assert len(children) == (0 if boundary == "acquisition" else 1)
    for child in children:
        assert child.returncode is not None
        assert child.stdout is not None and child.stdout.closed
        assert child.stderr is not None and child.stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
    for fd in held_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    os.fstat(borrowed)


def test_daemon_caller_retains_non_daemon_terminal_worker_until_fresh_retry(
    endpoint: tuple[int, int], custody: LocalDeliveryCustody, monkeypatch: pytest.MonkeyPatch
) -> None:
    import termios

    borrowed = endpoint[1]
    mode = termios.tcgetattr(borrowed)
    acquired, resume, caller_done = Event(), Event(), Event()
    attempts: list[_Attempt] = []
    errors: list[BaseException] = []
    held_fds: list[int] = []
    releasers: list[tuple[PosixTerminal, Thread]] = []
    native_acquire, native_release = PosixTerminal.acquire, PosixTerminal.release

    def acquire(terminal: PosixTerminal) -> None:
        native_acquire(terminal)
        held_fds.extend((terminal.master_fd, terminal.slave_fd))
        acquired.set()
        assert resume.wait(3)

    def restore(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        releasers.append((terminal, current_thread()))
        return native_release(terminal)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Cancelled acquisition admitted a native client")

    def call() -> None:
        try:
            io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
            attempt = _Attempt([sys.executable, "-c", "pass"], io, Deadline.after(0.1), custody)
            attempts.append(attempt)
            result = attempt.run()
            assert not result.started and result.failure is Failure.DEADLINE
        except BaseException as error:
            errors.append(error)
        finally:
            caller_done.set()

    monkeypatch.setattr(PosixTerminal, "acquire", acquire)
    monkeypatch.setattr(PosixTerminal, "release", restore)
    monkeypatch.setattr(LocalProcessOwner, "start", forbidden)
    caller = Thread(target=call, daemon=True)
    caller.start()
    try:
        assert acquired.wait(2) and caller_done.wait(2)
        if errors:
            raise errors[0]
        attempt = attempts[0]
        assert caller.daemon and not attempt._worker.daemon
        assert custody._owner is attempt._owner and custody._cleanup is attempt
        assert attempt._terminal._owner_thread is attempt._worker
        assert not attempt._done.is_set() and not custody.settled and not releasers
        assert termios.tcgetattr(borrowed) != mode
        for fd in (*endpoint, *held_fds):
            os.fstat(fd)
        assert not custody.close(Deadline.after(0.02))
        with pytest.raises(StateError):
            custody.begin_process()
    finally:
        resume.set()
        assert caller_done.wait(3)
        caller.join(1)
        assert custody.close(Deadline.after(3))
    assert custody.settled and attempt._done.is_set()
    assert releasers == [(attempt._terminal, attempt._worker)]
    assert_preserved_terminal_mode(termios.tcgetattr(borrowed), mode)
    for fd in held_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    for fd in endpoint:
        os.fstat(fd)


def test_thread_start_refusal_settles_inert_owner_without_restart(
    endpoint: tuple[int, int], custody: LocalDeliveryCustody, monkeypatch: pytest.MonkeyPatch
) -> None:
    io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", Source()), SinkOutput(Sink(), Sink()))
    attempt = _Attempt([sys.executable, "-c", "pass"], io, Deadline.after(1), custody)

    def refusal() -> None:
        assert custody._owner is attempt._owner and custody._cleanup is attempt
        raise RuntimeError("inert worker start refusal")

    def forbidden(terminal: PosixTerminal) -> None:
        pytest.fail("Unadmitted worker touched the terminal")

    monkeypatch.setattr(attempt._worker, "start", refusal)
    monkeypatch.setattr(PosixTerminal, "acquire", forbidden)
    result = attempt.run()
    assert not result.started and result.failure is Failure.DISPATCH
    assert custody.settled
    assert attempt._cancelled and not attempt._admitted and not attempt._native_start_attempted


def test_retryable_native_cleanup_keeps_terminal_raw_until_fresh_close(
    endpoint: tuple[int, int], custody: LocalDeliveryCustody, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import termios

    borrowed = endpoint[1]
    mode = termios.tcgetattr(borrowed)
    children: list[subprocess.Popen[bytes]] = []
    kills: list[int] = []
    releases: list[Thread] = []
    native_launch, native_kill, native_release = subprocess.Popen, os.kill, PosixTerminal.release

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        children.append(child)
        return child

    def kill(pid: int, sig: int) -> None:
        assert children and pid == children[0].pid and sig == signal.SIGKILL
        kills.append(pid)
        if len(kills) == 1:
            raise PermissionError("owned first native cleanup refusal")
        native_kill(pid, sig)

    def release(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        assert len(kills) == 2 and children[0].returncode is not None
        releases.append(current_thread())
        return native_release(terminal)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(PosixTerminal, "release", release)
    io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
    attempt = _Attempt([sys.executable, "-c", "import time; time.sleep(30)"], io, Deadline.after(0.2), custody)
    try:
        result = attempt.run()
        assert result.started and result.exit_status is None and result.failure is Failure.DEADLINE
        failed = attempt._owner.snapshot().terminal
        assert failed is not None and not failed.cleaned and failed.cleanup_retryable
        assert not custody.settled and not attempt._done.is_set() and releases == []
        assert termios.tcgetattr(borrowed) != mode
        os.fstat(attempt._terminal.slave_fd)
        with pytest.raises(StateError):
            custody.begin_process()
    finally:
        assert custody.close(Deadline.after(3))
    assert releases == [attempt._worker]
    assert_preserved_terminal_mode(termios.tcgetattr(borrowed), mode)
    assert children[0].returncode == -signal.SIGKILL
    assert children[0].stdout is not None and children[0].stdout.closed
    assert children[0].stderr is not None and children[0].stderr.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


def test_retryable_cleanup_natural_exit_settles_without_fresh_close(
    endpoint: tuple[int, int], custody: LocalDeliveryCustody, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import termios

    borrowed = endpoint[1]
    mode = termios.tcgetattr(borrowed)
    children: list[subprocess.Popen[bytes]] = []
    kills: list[int] = []
    releases: list[Thread] = []
    held_fds: list[int] = []
    native_launch, native_kill, native_release = subprocess.Popen, os.kill, PosixTerminal.release

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        children.append(child)
        return child

    def kill(pid: int, sig: int) -> None:
        assert children and pid == children[0].pid and sig == signal.SIGKILL
        kills.append(pid)
        if len(kills) == 1:
            raise PermissionError("owned first native cleanup refusal")
        native_kill(pid, sig)

    def release(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        held_fds.extend((terminal.master_fd, terminal.slave_fd))
        releases.append(current_thread())
        return native_release(terminal)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(PosixTerminal, "release", release)
    io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
    attempt = _Attempt([sys.executable, "-c", "import time; time.sleep(0.5)"], io, Deadline.after(0.1), custody)
    try:
        result = attempt.run()
        assert result.started and result.exit_status is None and result.failure is Failure.DEADLINE
        failed = attempt._owner.snapshot().terminal
        assert failed is not None and not failed.cleaned and failed.cleanup_retryable
        assert not custody.settled and termios.tcgetattr(borrowed) != mode
        requested = attempt._cleanup_requested
        assert attempt._done.wait(2)
        assert custody.settled and attempt._cleanup_requested == requested
        assert kills == [children[0].pid] and releases == [attempt._worker]
        assert_preserved_terminal_mode(termios.tcgetattr(borrowed), mode)
        assert result.started and result.exit_status is None and result.failure is Failure.DEADLINE
        assert children[0].returncode == 0
        assert children[0].stdout is not None and children[0].stdout.closed
        assert children[0].stderr is not None and children[0].stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
        for fd in held_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        for fd in endpoint:
            os.fstat(fd)
    finally:
        # A fresh finite rescue also settles the old-runtime negative control.
        assert custody.close(Deadline.after(3))


def test_nonretryable_native_loss_finishes_worker_without_terminal_release() -> None:
    # Confine permanent native loss to an owned probe. A regressed parked worker
    # cannot hold the pytest interpreter at shutdown; its exact child is reaped
    # before loss is introduced, and all terminal descriptors belong to the probe.
    code = r"""
import json,os,subprocess,sys,termios
from typing import Any,cast
import pytest
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import LocalProcessOwner,LocalProcessTerminal
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution.carrier import CarrierIO,Deadline,Failure,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from agentworks.execution.carriers.ssh._terminal_relay import _Attempt
class Source:
    def try_read(self,limit):return b''
class Sink:
    def try_write(self,data):return len(data)
master,slave=os.openpty()
endpoint=master,slave
with pytest.MonkeyPatch.context() as monkeypatch:

    # This case owns and deliberately externally reaps its exact native child.
    # That destroys owner status proof without leaving a real running process.
    # Its lost terminal resource has a separate, explicit fixture repair below.
    custody = LocalDeliveryCustody()
    borrowed = endpoint[1]
    mode = termios.tcgetattr(borrowed)
    children: list[subprocess.Popen[bytes]] = []
    owned_fds: list[int] = []
    native_launch, native_acquire = subprocess.Popen, PosixTerminal.acquire
    close_calls: list[LocalProcessOwner] = []
    native_close = LocalProcessOwner.close_bounded

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        children.append(child)
        pid, status = os.waitpid(child.pid, 0)
        assert pid == child.pid and os.waitstatus_to_exitcode(status) == 0
        return child

    def acquire(terminal: PosixTerminal) -> None:
        native_acquire(terminal)
        owned_fds.extend((terminal.master_fd, terminal.slave_fd))

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> LocalProcessTerminal | None:
        close_calls.append(owner)
        return native_close(owner, deadline)

    def forbidden_release(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        pytest.fail("Native custody loss cannot establish safe terminal restoration")

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(PosixTerminal, "acquire", acquire)
    monkeypatch.setattr(PosixTerminal, "release", forbidden_release)
    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    io = CarrierIO(TerminalInput(borrowed, borrowed, "fixture", Source()), SinkOutput(Sink(), Sink()))
    attempt = _Attempt([sys.executable, "-c", "pass"], io, Deadline.after(2), custody)
    try:
        result = attempt.run()
        assert result.started and result.exit_status is result.local_status is None
        assert result.failure is Failure.OBSERVATION
        process = attempt._owner.snapshot().terminal
        assert process is not None and not process.cleaned and not process.cleanup_retryable
        assert attempt._done.is_set() and not attempt.settled and not custody.settled
        assert not attempt._terminal.settled and termios.tcgetattr(borrowed) != mode
        assert custody._owner is attempt._owner and custody._cleanup is attempt
        assert len(close_calls) == 1
        assert not custody.close(Deadline.after(0.1))
        assert len(close_calls) == 1
        with pytest.raises(StateError):
            custody.begin_process()
        for fd in (*endpoint, *owned_fds):
            os.fstat(fd)
        assert len(children) == 1
        assert children[0].stdout is not None and children[0].stdout.closed
        assert children[0].stderr is not None and children[0].stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    finally:
        assert attempt._done.wait(3)
        # The exact child was already reaped above, and the product borrower has
        # finished. Repair only these known owned fixture fds, without retrying
        # product release or changing its permanent lost-custody evidence.
        termios.tcsetattr(borrowed, termios.TCSANOW, mode)
        for fd in owned_fds:
            os.close(fd)
    assert not custody.settled
    assert_preserved_terminal_mode(termios.tcgetattr(borrowed), mode)
    os.close(slave)
    os.close(master)
    print(json.dumps({'worker_done':attempt._done.is_set(),'settled':custody.settled,
        'releases':0,'native_reaped':True,'fixture_repaired':True}),flush=True)
"""
    code = with_terminal_mode_assertion(code)
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=8, check=False)
    assert completed.returncode == 0, (completed.stdout[-4096:], completed.stderr[-4096:])
    import json

    proof = json.loads(completed.stdout)
    assert proof["worker_done"] and not proof["settled"]
    assert proof["releases"] == 0 and proof["native_reaped"] and proof["fixture_repaired"]
    assert completed.stderr == b""
