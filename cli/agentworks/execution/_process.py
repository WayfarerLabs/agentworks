"""Stdlib-only bounded I/O for one owned local process.

Python 3.12 supports nonblocking anonymous pipes on Windows as well as POSIX.
The core remains Python 3.11-compatible for POSIX guest-helper reuse. A private
launch owner constructs and retains the process while the caller alone pumps
borrowed endpoints. Return leaves no task using an endpoint or live process
capability. A canceled, capability-free bootstrap or terminal native return
tail may finish after return. Execution uses the supplied deadline; killing and
reaping the local process gets at most 0.5 seconds more. That allowance never
resumes execution.

The launch admission handoff uses a condition lock for publication. It does not
assume unsynchronized Python object writes are portable. On POSIX, an external
concurrent reaper can still create a PID exit/reuse race before lost ownership
becomes observable.
"""

from __future__ import annotations

import _thread
import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import IO

_CHUNK = 65_536
_POLL_SECONDS = 0.01
_CLEANUP_SECONDS = 0.5
_EXIT_DRAIN_SECONDS = 0.1


class ByteSource(Protocol):
    """Borrowed nonblocking byte source; None means temporarily stalled."""

    def try_read(self, limit: int) -> bytes | None: ...


class ByteSink(Protocol):
    """Borrowed nonblocking byte sink; None means temporarily stalled."""

    def try_write(self, data: memoryview) -> int | None: ...


class SinkWriteError(Exception):
    """A borrowed sink failed without carrying its exception or representation."""


def try_write_to_sink(sink: ByteSink, data: memoryview) -> int | None:
    """Attempt one bounded write and validate the adapter-authored result."""
    failed = False
    try:
        written = sink.try_write(data)
    except Exception:
        failed = True
        written = None
    if failed or not data or (written is not None and (type(written) is not int or not 1 <= written <= len(data))):
        raise SinkWriteError
    return written


@dataclass(frozen=True)
class Deadline:
    """An absolute local process-observation deadline; None is unbounded."""

    expires_at: float | None

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


@dataclass(frozen=True)
class ProcessInput:
    """One finite or borrowed source; two None values select immediate EOF."""

    data: bytes | None = field(default=None, repr=False)
    source: ByteSource | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.data is not None and self.source is not None:
            raise ValueError("process input cannot be both finite and borrowed")

    @property
    def piped(self) -> bool:
        return self.data is not None or self.source is not None


@dataclass(frozen=True)
class ProcessOutput:
    """Capture up to a bound, deliver to two sinks, or only drain."""

    capture_limit: int | None = None
    stdout_sink: ByteSink | None = field(default=None, repr=False)
    stderr_sink: ByteSink | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.capture_limit is not None and (self.stdout_sink is not None or self.stderr_sink is not None):
            raise ValueError("process output cannot be both captured and delivered")
        if (self.stdout_sink is None) != (self.stderr_sink is None):
            raise ValueError("process output delivery requires two sinks")


@dataclass(frozen=True)
class StreamResult:
    data: bytes = field(repr=False)
    complete: bool


class ProcessFailure(StrEnum):
    DEADLINE = "deadline"
    DISPATCH = "dispatch"
    OBSERVATION = "observation"
    INPUT = "input"
    OUTPUT = "output"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True)
class ProcessResult:
    started: bool
    local_status: int | None
    # Only a status observed before local termination can establish completion.
    exit_status: int | None
    stdout: StreamResult
    stderr: StreamResult
    failure: ProcessFailure | None


@dataclass
class _Output:
    limit: int | None
    sink: ByteSink | None = field(default=None, repr=False)
    data: bytearray = field(default_factory=bytearray, repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    eof: bool = False
    limited: bool = False

    def advance(self, pipe: IO[bytes]) -> tuple[bool, bool]:
        """Advance one bounded read or borrowed-sink write."""
        if self.eof:
            return False, False
        if self.pending is not None:
            return self._write_pending()
        try:
            chunk = os.read(pipe.fileno(), _CHUNK)
        except BlockingIOError:
            return False, False
        if not chunk:
            self.eof = True
            return False, False
        if self.sink is not None:
            self.pending = memoryview(chunk)
            _, failed = self._write_pending()
            return True, failed
        elif self.limit is not None:
            available = self.limit - len(self.data)
            self.data.extend(chunk[:available])
            self.limited |= len(chunk) > available
        return True, False

    def _write_pending(self) -> tuple[bool, bool]:
        assert self.sink is not None and self.pending is not None
        try:
            written = try_write_to_sink(self.sink, self.pending)
        except SinkWriteError:
            return False, True
        if written is None:
            return False, False
        if written == len(self.pending):
            self.pending = None
        else:
            self.pending = self.pending[written:]
        return True, False

    def report(self) -> StreamResult:
        complete = self.eof and self.pending is None and not self.limited
        return StreamResult(bytes(self.data), complete)


@dataclass
class _Input:
    source: ByteSource | None = field(default=None, repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    eof: bool = False

    @classmethod
    def from_spec(cls, input_spec: ProcessInput) -> _Input:
        if input_spec.data is not None:
            return cls(pending=memoryview(input_spec.data), eof=True)
        if input_spec.source is not None:
            return cls(source=input_spec.source)
        return cls(eof=True)

    @property
    def complete(self) -> bool:
        return self.eof and (self.pending is None or not self.pending)

    def advance(self, pipe: IO[bytes]) -> tuple[bool, bool]:
        """Advance one bounded source read and one bounded pipe write."""
        progressed = False
        if self.pending is None and not self.eof:
            assert self.source is not None
            try:
                chunk = self.source.try_read(_CHUNK)
            except Exception:
                return False, True
            if chunk is None:
                return False, False
            if type(chunk) is not bytes or len(chunk) > _CHUNK:
                return False, True
            progressed = True
            if not chunk:
                self.eof = True
            else:
                self.pending = memoryview(chunk)
        if self.pending:
            try:
                written = os.write(pipe.fileno(), self.pending[:_CHUNK])
            except BlockingIOError:
                return progressed, False
            except OSError:
                return progressed, True
            if written <= 0 or written > min(len(self.pending), _CHUNK):
                return progressed, True
            progressed = True
            if written == len(self.pending):
                self.pending = None
            else:
                self.pending = self.pending[written:]
        if self.complete and not pipe.closed:
            try:
                pipe.close()
            except OSError:
                return progressed, True
            progressed = True
        return progressed, False


@dataclass
class _PostExitDrain:
    remaining: float = _EXIT_DRAIN_SECONDS
    checked_at: float = field(default_factory=time.monotonic)
    paused: bool = False

    def update(self, *, paused: bool) -> bool:
        """Spend only time available for pipe collection, never sink backpressure."""
        now = time.monotonic()
        if not self.paused:
            self.remaining -= now - self.checked_at
        self.checked_at = now
        self.paused = paused
        return self.remaining <= 0


@dataclass
class _ProcessStatus:
    """Own the one status observation path for a constructed process."""

    process: subprocess.Popen[bytes]
    status: int | None = None
    lost: bool = False

    def poll(self) -> int | None:
        if self.status is not None or self.lost:
            return self.status
        if os.name == "nt":
            self.status = self.process.poll()
            return self.status
        try:
            pid, wait_status = os.waitpid(self.process.pid, os.WNOHANG)
        except InterruptedError:
            return None
        except ChildProcessError:
            self.lost = True
            # Popen has no "reaped with unknown status" state. Retire only
            # its destructor bookkeeping; ProcessResult never reads this.
            self.process.returncode = 0
            return None
        if pid == 0:
            return None
        if pid != self.process.pid:
            raise OSError("exact-PID wait returned a different process")
        if not os.WIFEXITED(wait_status) and not os.WIFSIGNALED(wait_status):
            return None
        self.status = os.waitstatus_to_exitcode(wait_status)
        self.process.returncode = self.status
        return self.status


class _Admission(StrEnum):
    WAITING = "waiting"
    ADMITTED = "admitted"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class _LaunchRequest:
    argv: tuple[str, ...] = field(repr=False)
    input_piped: bool
    env: tuple[tuple[str, str], ...] | None = field(default=None, repr=False)
    cwd: str | None = field(default=None, repr=False)
    pass_fds: tuple[int, ...] = field(default=(), repr=False)
    start_new_session: bool = False


@dataclass(frozen=True)
class _ProcessPipes:
    stdin: IO[bytes] | None = field(repr=False)
    stdout: IO[bytes] = field(repr=False)
    stderr: IO[bytes] = field(repr=False)


@dataclass(frozen=True)
class _OwnerTerminal:
    started: bool
    local_status: int | None
    exit_status: int | None
    cleaned: bool
    dispatch_failed: bool = False
    observation_failed: bool = False


@dataclass(frozen=True)
class _OwnerSnapshot:
    pipes: _ProcessPipes | None
    exit_status: int | None
    observation_failed: bool
    terminal: _OwnerTerminal | None


class _LaunchOwner:
    """Publish one admitted launch through lock-mediated immutable snapshots."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._admission = _Admission.WAITING
        self._request: _LaunchRequest | None = None
        self._pipes: _ProcessPipes | None = None
        self._exit_status: int | None = None
        self._observation_failed = False
        self._pump_stopped = False
        self._terminal: _OwnerTerminal | None = None

    def admit(self, request: _LaunchRequest) -> bool:
        with self._condition:
            if self._admission is not _Admission.WAITING:
                return False
            self._request = request
            self._admission = _Admission.ADMITTED
            self._condition.notify_all()
            return True

    def cancel_if_waiting(self) -> bool:
        """Cancel default-deny admission, returning whether launch was admitted."""
        with self._condition:
            if self._admission is _Admission.WAITING:
                self._request = None
                self._admission = _Admission.CANCELLED
                self._condition.notify_all()
            return self._admission is _Admission.ADMITTED

    def take_request(self) -> _LaunchRequest | None:
        with self._condition:
            while self._admission is _Admission.WAITING:
                self._condition.wait(_POLL_SECONDS)
            if self._admission is _Admission.CANCELLED:
                return None
            request = self._request
            self._request = None
            assert request is not None
            return request

    def publish_ready(self, pipes: _ProcessPipes) -> None:
        with self._condition:
            self._pipes = pipes
            self._condition.notify_all()

    def publish_exit(self, status: int) -> None:
        with self._condition:
            if self._exit_status is None:
                self._exit_status = status
                self._condition.notify_all()

    def publish_observation_failure(self) -> None:
        with self._condition:
            self._observation_failed = True
            self._condition.notify_all()

    def wait_for_pump_stop(self, status: _ProcessStatus) -> tuple[bool, int | None]:
        """Observe exact status until the caller relinquishes all pipe use."""
        while True:
            if not self._observation_failed:
                try:
                    observed = status.poll()
                except OSError:
                    self.publish_observation_failure()
                else:
                    if status.lost:
                        self.publish_observation_failure()
                    elif observed is not None:
                        self.publish_exit(observed)
            with self._condition:
                if self._pump_stopped:
                    return self._observation_failed, self._exit_status
                self._condition.wait(_POLL_SECONDS)

    def stop_pump(self) -> None:
        with self._condition:
            self._pump_stopped = True
            self._condition.notify_all()

    def wait_until_pump_stopped(self) -> None:
        with self._condition:
            while not self._pump_stopped:
                self._condition.wait(_POLL_SECONDS)

    def snapshot(self) -> _OwnerSnapshot:
        with self._condition:
            return _OwnerSnapshot(
                self._pipes,
                self._exit_status,
                self._observation_failed,
                self._terminal,
            )

    def wait_terminal(self) -> _OwnerTerminal:
        with self._condition:
            while self._terminal is None:
                self._condition.wait(_POLL_SECONDS)
            return self._terminal

    def publish_terminal(self, terminal: _OwnerTerminal) -> None:
        with self._condition:
            self._request = None
            self._pipes = None
            self._terminal = terminal
            self._condition.notify_all()


def _cleanup(status: _ProcessStatus) -> bool:
    """Close owned pipes and bound local kill and reap, even on interruption."""
    process = status.process
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()
    if status.lost:
        return False
    if os.name == "nt":
        try:
            if status.poll() is None:
                process.kill()
            status.status = process.wait(timeout=_CLEANUP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    try:
        running = status.poll() is None
    except OSError:
        running = True
    if status.lost:
        return False
    if running:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
    cleanup_deadline = time.monotonic() + _CLEANUP_SECONDS
    while True:
        try:
            if status.poll() is not None:
                return True
        except OSError:
            pass
        remaining = cleanup_deadline - time.monotonic()
        if status.lost or remaining <= 0:
            return False
        time.sleep(min(_POLL_SECONDS, remaining))


def _run_launch_owner(owner: _LaunchOwner) -> None:
    """Own an admitted request and all process capabilities through cleanup."""
    request = owner.take_request()
    if request is None:
        return

    status: _ProcessStatus | None = None
    process: subprocess.Popen[bytes] | None = None
    pipes: _ProcessPipes | None = None
    started = False
    dispatch_failed = False
    observation_failed = False
    exit_status: int | None = None
    cleaned = True
    try:
        try:
            process = subprocess.Popen(
                request.argv,
                stdin=subprocess.PIPE if request.input_piped else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=None if request.env is None else dict(request.env),
                cwd=request.cwd,
                pass_fds=request.pass_fds,
                start_new_session=request.start_new_session,
                shell=False,
                close_fds=True,
                preexec_fn=None,
            )
        except (OSError, ValueError):
            dispatch_failed = True
            return
        started = True
        status = _ProcessStatus(process)
        assert process.stdout is not None and process.stderr is not None
        pipes = _ProcessPipes(process.stdin, process.stdout, process.stderr)
        request = None
        owner.publish_ready(pipes)
        observation_failed, exit_status = owner.wait_for_pump_stop(status)
    except BaseException:
        # Raw thread exceptions would expose request details through
        # sys.unraisablehook. Keep the owner closed and report only a category.
        observation_failed = started
        dispatch_failed = not started
        owner.publish_observation_failure()
        if started:
            if status is None:
                assert process is not None
                status = _ProcessStatus(process)
            while True:
                try:
                    owner.wait_until_pump_stopped()
                    break
                except BaseException:
                    continue
    finally:
        request = None
        if status is not None:
            try:
                cleaned = _cleanup(status)
            except BaseException:
                cleaned = False
            local_status = status.status
            status = None
            process = None
            pipes = None
        else:
            local_status = None
        owner.publish_terminal(
            _OwnerTerminal(
                started=started,
                local_status=local_status,
                exit_status=exit_status,
                cleaned=cleaned,
                dispatch_failed=dispatch_failed,
                observation_failed=observation_failed,
            )
        )


def _launch_owner_entry(owner: _LaunchOwner) -> None:
    """Keep every raw native-thread failure away from sys.unraisablehook."""
    with suppress(BaseException):
        _run_launch_owner(owner)
        # The owner path itself contains the normal fail-closed publication.
        # This final guard exists only to keep raw thread diagnostics private.


def _stop_and_wait(
    owner: _LaunchOwner,
    interruption: BaseException | None,
) -> tuple[_OwnerTerminal, BaseException | None]:
    """Preserve the first control exception until owner cleanup completes."""
    stopped = False
    while not stopped:
        try:
            owner.stop_pump()
            stopped = True
        except BaseException as error:
            if interruption is None or (isinstance(interruption, Exception) and not isinstance(error, Exception)):
                interruption = error
    while True:
        try:
            return owner.wait_terminal(), interruption
        except BaseException as error:
            if interruption is None or (isinstance(interruption, Exception) and not isinstance(error, Exception)):
                interruption = error


def _cancel_admission(
    owner: _LaunchOwner,
    interruption: BaseException | None,
) -> tuple[bool, BaseException | None]:
    """Terminally cancel ambiguous admission while retaining control flow."""
    while True:
        try:
            return owner.cancel_if_waiting(), interruption
        except BaseException as error:
            # The interruption that made launch ownership ambiguous remains the
            # caller-visible one; later interruptions only delay cancellation.
            if interruption is None or (isinstance(interruption, Exception) and not isinstance(error, Exception)):
                interruption = error
            continue


def _pump_owned_pipes(
    owner: _LaunchOwner,
    pipes: _ProcessPipes,
    input_spec: ProcessInput,
    deadline: Deadline,
    stdout: _Output,
    stderr: _Output,
) -> tuple[ProcessFailure | None, int | None]:
    """Pump only caller-owned state while the launch owner observes status."""
    for pipe in (pipes.stdin, pipes.stdout, pipes.stderr):
        if pipe is not None:
            os.set_blocking(pipe.fileno(), False)
    input_state = _Input.from_spec(input_spec)
    output_first = True
    failure: ProcessFailure | None = None
    exit_status: int | None = None
    post_exit_drain: _PostExitDrain | None = None
    while True:
        snapshot = owner.snapshot()
        exit_status = snapshot.exit_status
        if snapshot.observation_failed:
            failure = ProcessFailure.OBSERVATION
            break
        if deadline.expired:
            failure = ProcessFailure.DEADLINE
            break
        pending_delivery = stdout.pending is not None or stderr.pending is not None
        if exit_status is not None:
            if post_exit_drain is None:
                post_exit_drain = _PostExitDrain(paused=pending_delivery)
            elif post_exit_drain.update(paused=pending_delivery):
                # A continuously writing descendant must not extend even an
                # explicitly unbounded invocation after its client exits.
                failure = ProcessFailure.OUTPUT
                break
        progressed = False
        outputs = list(
            ((stdout, pipes.stdout), (stderr, pipes.stderr))
            if output_first
            else ((stderr, pipes.stderr), (stdout, pipes.stdout))
        )
        output_first = not output_first
        if exit_status is not None and pending_delivery:
            # Resume collection only after the drain clock is unpaused.
            outputs = [item for item in outputs if item[0].pending is not None]
        for output_state, pipe in outputs:
            if (
                exit_status is not None
                and output_state.pending is None
                and (stdout.pending is not None or stderr.pending is not None)
            ):
                continue
            try:
                output_progressed, output_failed = output_state.advance(pipe)
            except OSError:
                output_failed = True
                output_progressed = False
            progressed = output_progressed or progressed
            if output_failed:
                failure = ProcessFailure.OUTPUT
                break
        if failure is not None:
            break
        if pipes.stdin is not None and not pipes.stdin.closed:
            if exit_status is not None and not input_state.complete:
                failure = ProcessFailure.INPUT
                break
            input_progressed, input_failed = input_state.advance(pipes.stdin)
            progressed = input_progressed or progressed
            if input_failed:
                failure = ProcessFailure.INPUT
                break
        if exit_status is not None:
            if not input_state.complete:
                failure = ProcessFailure.INPUT
                break
            if stdout.eof and stderr.eof:
                break
            assert post_exit_drain is not None
            pending_delivery = stdout.pending is not None or stderr.pending is not None
            if post_exit_drain.update(paused=pending_delivery):
                failure = ProcessFailure.OUTPUT
                break
        if not progressed:
            remaining = deadline.remaining()
            time.sleep(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))
    return failure, exit_status


def run_owned_process(
    argv: list[str],
    *,
    input: ProcessInput,
    output: ProcessOutput,
    deadline: Deadline,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    pass_fds: tuple[int, ...] = (),
    start_new_session: bool = False,
) -> ProcessResult:
    """Fairly pump bounded input and output without retaining borrowed endpoints.

    Passed descriptors remain caller-owned; only child inheritance is configured.
    POSIX session creation is a launch control, not descendant containment.
    """
    stdout = _Output(output.capture_limit, output.stdout_sink)
    stderr = _Output(output.capture_limit, output.stderr_sink)
    if deadline.expired:
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DEADLINE)

    try:
        request = _LaunchRequest(
            tuple(argv),
            input.piped,
            None if env is None else tuple(env.items()),
            cwd,
            tuple(pass_fds),
            start_new_session,
        )
    except (OSError, ValueError):
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)

    owner = _LaunchOwner()
    failure: ProcessFailure | None = None
    exit_status: int | None = None
    interruption: BaseException | None = None
    pipes: _ProcessPipes | None = None
    terminal: _OwnerTerminal | None = None
    start_returned = False
    admitted = False
    owner_admitted = False
    ownership_settled = False
    try:
        _thread.start_new_thread(_launch_owner_entry, (owner,))
        start_returned = True
        admitted = owner.admit(request)
        if not admitted:
            raise RuntimeError("local launch admission failed")
        del request
        while pipes is None:
            snapshot = owner.snapshot()
            if snapshot.terminal is not None:
                break
            if snapshot.pipes is not None:
                pipes = snapshot.pipes
                break
            if snapshot.observation_failed:
                failure = ProcessFailure.OBSERVATION
                break
            if deadline.expired:
                failure = ProcessFailure.DEADLINE
                break
            remaining = deadline.remaining()
            time.sleep(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

        if pipes is not None:
            failure, exit_status = _pump_owned_pipes(owner, pipes, input, deadline, stdout, stderr)
    except OSError as error:
        if admitted:
            failure = ProcessFailure.OBSERVATION
        else:
            interruption = error
    except BaseException as error:
        interruption = error
    finally:
        while not ownership_settled:
            try:
                if admitted:
                    owner_admitted = True
                else:
                    owner_admitted, interruption = _cancel_admission(owner, interruption)
                if owner_admitted:
                    terminal, interruption = _stop_and_wait(owner, interruption)
                    exit_status = terminal.exit_status
                    if not terminal.cleaned and interruption is not None:
                        interruption.add_note("Local carrier process cleanup did not complete within its bound.")
                ownership_settled = True
            except BaseException as error:
                if interruption is None or (isinstance(interruption, Exception) and not isinstance(error, Exception)):
                    interruption = error
    if interruption is not None:
        if not start_returned and isinstance(interruption, Exception):
            return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)
        raise interruption
    if not owner_admitted:
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)
    assert terminal is not None
    if terminal.dispatch_failed:
        failure = ProcessFailure.DISPATCH
    elif not terminal.cleaned or terminal.observation_failed:
        failure = ProcessFailure.OBSERVATION
    elif failure is None and (stdout.limited or stderr.limited):
        failure = ProcessFailure.OUTPUT_LIMIT
    return ProcessResult(
        terminal.started,
        terminal.local_status,
        exit_status,
        stdout.report(),
        stderr.report(),
        failure,
    )
