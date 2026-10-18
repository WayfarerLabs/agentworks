"""Finite caller waits retain the exact worker, terminal and native owner."""

from __future__ import annotations

import os
import subprocess
import sys
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
    entered, release, boundary_exited = Event(), Event(), Event()
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
        try:
            assert release.wait(3)
        finally:
            boundary_exited.set()

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
        # Native startup precedes cleanup; prove return while its boundary is
        # still held, including detection of the fixture's own hold timeout.
        assert not release.is_set() and not boundary_exited.is_set()
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


def test_exact_wait_loss_retires_locally_and_restores_on_original_worker() -> None:
    _run_retirement_probe("confirmed")


@pytest.mark.parametrize("close_effect", ["before", "after_reuse"])
def test_original_pipe_close_uncertainty_retains_terminal_and_native_objects(close_effect: str) -> None:
    _run_retirement_probe(close_effect)


def _run_retirement_probe(close_effect: str) -> None:
    # Permanent close uncertainty belongs in a bounded disposable interpreter.
    # Its exact child is externally reaped before the owner discovers wait loss.
    code = r"""
import json,os,subprocess,sys,termios
from threading import current_thread
import pytest
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution.carrier import CarrierIO,Deadline,Failure,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from agentworks.execution.carriers.ssh._terminal_relay import _Attempt
class Source:
    def try_read(self,limit):return b''
class Sink:
    def try_write(self,data):return len(data)
close_effect = sys.argv[1]
master,borrowed=os.openpty()
mode=termios.tcgetattr(borrowed)
custody=LocalDeliveryCustody()
children=[]
owned_fds=[]
releases=[]
waits=[]
signals=[]
close_calls=[]
pipe_calls=[]
reused=[]
original_pipes=[]
fixture_closers=[]
published=[]
native_publish=LocalProcessOwner._publish_ready
native_launch,native_wait=subprocess.Popen,os.waitpid
native_acquire,native_release=PosixTerminal.acquire,PosixTerminal.release
native_close=LocalProcessOwner.close_bounded
with pytest.MonkeyPatch.context() as monkeypatch:
    def launch(*args, **kwargs):
        child=native_launch(*args, **kwargs)
        pid,status=native_wait(child.pid,0)
        assert pid==child.pid and os.waitstatus_to_exitcode(status)==23
        children.append(child)
        assert child.stdout is not None and child.stderr is not None
        original_pipes.extend((child.stdout,child.stderr))
        if close_effect != 'confirmed':
            pipe=child.stdout
            original_close=pipe.close
            fixture_closers.append(original_close)
            original_fd=pipe.fileno()
            def uncertain_close():
                pipe_calls.append(pipe)
                assert len(pipe_calls)==1
                if close_effect=='after_reuse':
                    original_close()
                    fd=os.open(os.devnull,os.O_RDONLY)
                    if fd!=original_fd:
                        os.dup2(fd,original_fd)
                        os.close(fd)
                    reused.append(original_fd)
                raise OSError('owned original pipe close uncertainty')
            monkeypatch.setattr(pipe,'close',uncertain_close)
        return child
    def wait(pid,options):
        waits.append(pid)
        assert len(children)==1 and pid==children[0].pid and len(waits)==1
        return native_wait(pid,options)
    def forbidden_signal(*args,**kwargs):
        signals.append((args,kwargs))
        pytest.fail('Lost numeric PID authority was used for a signal')
    def acquire(terminal):
        native_acquire(terminal)
        owned_fds.extend((terminal.master_fd,terminal.slave_fd))
    def publish(owner,pipes):
        assert (pipes.stdout,pipes.stderr)==tuple(original_pipes)
        published.append((pipes,tuple(pipes._records)))
        native_publish(owner,pipes)
    def close(owner,deadline):
        close_calls.append((owner,current_thread()))
        return native_close(owner,deadline)
    def release(terminal):
        assert close_effect=='confirmed'
        assert terminal is attempt._terminal
        process=attempt._owner.snapshot().terminal
        assert process is not None and process.cleaned and process.observation_failed
        assert all(pipe.closed for pipe in original_pipes)
        releases.append((terminal,current_thread()))
        return native_release(terminal)
    monkeypatch.setattr(subprocess,'Popen',launch)
    monkeypatch.setattr(os,'waitpid',wait)
    monkeypatch.setattr(os,'kill',forbidden_signal)
    monkeypatch.setattr(PosixTerminal,'acquire',acquire)
    monkeypatch.setattr(PosixTerminal,'release',release)
    monkeypatch.setattr(LocalProcessOwner,'close_bounded',close)
    monkeypatch.setattr(LocalProcessOwner,'_publish_ready',publish)
    io=CarrierIO(TerminalInput(borrowed,borrowed,'fixture',Source()),SinkOutput(Sink(),Sink()))
    attempt=_Attempt([sys.executable,'-c','import sys; sys.exit(23)'],io,Deadline.after(3),custody)
    owner=attempt._owner
    terminal=attempt._terminal
    try:
        result=attempt.run()
        assert result.started and result.exit_status is result.local_status is None
        assert result.failure is Failure.OBSERVATION
        assert attempt._done.wait(2)
        attempt._worker.join(1)
        assert not attempt._worker.is_alive()
        process=owner.snapshot().terminal
        assert process is not None and process.observation_failed and not process.cleanup_retryable
        assert process.local_status is process.exit_status is children[0].returncode is None
        assert custody._owner is owner and custody._cleanup is attempt
        assert terminal._owner_thread is attempt._worker
        assert len(close_calls)==1 and close_calls[0]==(owner,attempt._worker)
        if close_effect=='confirmed':
            assert process.cleaned and attempt.settled and custody.settled and terminal.settled
            assert releases==[(terminal,attempt._worker)]
            assert_preserved_terminal_mode(termios.tcgetattr(borrowed),mode)
            for fd in owned_fds:
                with pytest.raises(OSError):os.fstat(fd)
        else:
            assert not process.cleaned and not attempt.settled and not custody.settled
            assert not terminal.settled and termios.tcgetattr(borrowed)!=mode
            assert not releases
            status=owner._retained_status
            assert status is not None and status.lost and status.process_retired
            assert status.process is owner._retained_process is children[0]
            pipes=status.pipes
            assert len(published)==1 and pipes is published[0][0]
            assert pipes.stdout is original_pipes[0] and pipes.stderr is original_pipes[1]
            records=published[0][1]
            assert len(pipes._records)==len(records)
            assert all(actual is original for actual,original in zip(pipes._records,records,strict=True))
            uncertain=next(record for record in records if record.pipe is original_pipes[0])
            assert uncertain.attempted and not uncertain.confirmed
            assert all(record.confirmed for record in records if record is not uncertain)
            assert original_pipes[0].closed is (close_effect=='after_reuse')
            assert original_pipes[1].closed
            for fd in (master,borrowed,*owned_fds,*reused):os.fstat(fd)
            with pytest.raises(StateError):custody.begin_process()
        assert custody.close(Deadline.after(.1)) is (close_effect=='confirmed')
        assert custody.close(Deadline.after(.1)) is (close_effect=='confirmed')
        assert len(close_calls)==1
        assert waits==[children[0].pid] and not signals
        assert len(pipe_calls)==(0 if close_effect=='confirmed' else 1)
        assert owner.snapshot().terminal is process
        if close_effect!='confirmed':
            assert owner._retained_status is status and status.pipes is pipes
            assert all(actual is original for actual,original in zip(pipes._records,records,strict=True))
            assert not uncertain.confirmed and not releases
            for fd in reused:os.fstat(fd)
        # All product observations above precede exact fixture-only rescue.
        proof={'worker_done':True,'settled':custody.settled,'releases':len(releases),
            'native_reaped':True,'waits':len(waits),'pipe_calls':len(pipe_calls)}
    finally:
        assert attempt._done.wait(2)
        attempt._worker.join(1)
        assert not attempt._worker.is_alive()
        # Rescue only these fixtures after observations, including a failed
        # positive-case assertion. Never retry product close or change records.
        # The exact child has already been externally reaped.
        if not terminal.settled:
            termios.tcsetattr(borrowed,termios.TCSANOW,mode)
        if fixture_closers and not original_pipes[0].closed:
            fixture_closers[0]()
        if not terminal.settled:
            for fd in owned_fds:os.close(fd)
        for fd in reused:os.close(fd)
        assert_preserved_terminal_mode(termios.tcgetattr(borrowed),mode)
        os.close(borrowed)
        os.close(master)
print(json.dumps(proof),flush=True)
"""
    code = with_terminal_mode_assertion(code)
    completed = subprocess.run([sys.executable, "-c", code, close_effect], capture_output=True, timeout=10, check=False)
    assert completed.returncode == 0, (completed.stdout[-4096:], completed.stderr[-4096:])
    import json

    proof = json.loads(completed.stdout)
    assert proof["worker_done"] and proof["settled"] is (close_effect == "confirmed")
    assert proof["releases"] == (1 if close_effect == "confirmed" else 0)
    assert proof["native_reaped"] and proof["waits"] == 1
    assert proof["pipe_calls"] == (0 if close_effect == "confirmed" else 1)
    assert completed.stderr == b""
