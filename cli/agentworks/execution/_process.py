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
class LocalProcessRequest:
    """Immutable configuration for one local process admission."""

    argv: tuple[str, ...] = field(repr=False)
    input_piped: bool
    env: tuple[tuple[str, str], ...] | None = field(default=None, repr=False)
    cwd: str | None = field(default=None, repr=False)
    pass_fds: tuple[int, ...] = field(default=(), repr=False)
    start_new_session: bool = False


@dataclass(frozen=True)
class LocalProcessPipes:
    """Borrowed child pipe endpoints published after dispatch."""

    stdin: IO[bytes] | None = field(repr=False)
    stdout: IO[bytes] = field(repr=False)
    stderr: IO[bytes] = field(repr=False)


@dataclass(frozen=True)
class LocalProcessTerminal:
    """Facts retained after an admitted process or denied admission settles."""

    admitted: bool
    started: bool
    local_status: int | None
    exit_status: int | None
    cleaned: bool
    dispatch_failed: bool = False
    observation_failed: bool = False


@dataclass(frozen=True)
class LocalProcessSnapshot:
    """One immutable observation of owner publication state."""

    pipes: LocalProcessPipes | None
    exit_status: int | None
    observation_failed: bool
    terminal: LocalProcessTerminal | None


class LocalProcessOwner:
    """Own one local process while callers borrow its published pipes.

    One caller serializes ``start`` and ``close``. Other threads may observe
    immutable snapshots, but pipe borrowers must stop before ``close``.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._admission = _Admission.WAITING
        self._request: LocalProcessRequest | None = None
        self._pipes: LocalProcessPipes | None = None
        self._exit_status: int | None = None
        self._observation_failed = False
        self._borrowers_stopped = False
        self._terminal: LocalProcessTerminal | None = None
        self._start_called = False
        self._closed = False

    def _admit(self, request: LocalProcessRequest) -> bool:
        with self._condition:
            if self._admission is not _Admission.WAITING:
                return False
            self._request = request
            self._admission = _Admission.ADMITTED
            self._condition.notify_all()
            return True

    def _cancel_if_waiting(self) -> bool:
        """Cancel default-deny admission, returning whether launch was admitted."""
        with self._condition:
            if self._admission is _Admission.WAITING:
                self._request = None
                self._admission = _Admission.CANCELLED
                self._condition.notify_all()
            return self._admission is _Admission.ADMITTED

    def _take_request(self) -> LocalProcessRequest | None:
        with self._condition:
            while self._admission is _Admission.WAITING:
                self._condition.wait(_POLL_SECONDS)
            if self._admission is _Admission.CANCELLED:
                return None
            request = self._request
            self._request = None
            assert request is not None
            return request

    def _publish_ready(self, pipes: LocalProcessPipes) -> None:
        with self._condition:
            self._pipes = pipes
            self._condition.notify_all()

    def _publish_exit(self, status: int) -> None:
        with self._condition:
            if self._exit_status is None:
                self._exit_status = status
                self._condition.notify_all()

    def _publish_observation_failure(self) -> None:
        with self._condition:
            self._observation_failed = True
            self._condition.notify_all()

    def _wait_for_borrowers(self, status: _ProcessStatus) -> tuple[bool, int | None]:
        """Observe exact status until the caller relinquishes all pipe use."""
        while True:
            if not self._observation_failed:
                try:
                    observed = status.poll()
                except OSError:
                    self._publish_observation_failure()
                else:
                    if status.lost:
                        self._publish_observation_failure()
                    elif observed is not None:
                        self._publish_exit(observed)
            with self._condition:
                if self._borrowers_stopped:
                    return self._observation_failed, self._exit_status
                self._condition.wait(_POLL_SECONDS)

    def _stop_borrowers(self) -> None:
        with self._condition:
            self._borrowers_stopped = True
            self._condition.notify_all()

    def _wait_until_borrowers_stopped(self) -> None:
        with self._condition:
            while not self._borrowers_stopped:
                self._condition.wait(_POLL_SECONDS)

    def snapshot(self) -> LocalProcessSnapshot:
        with self._condition:
            return LocalProcessSnapshot(
                self._pipes,
                self._exit_status,
                self._observation_failed,
                self._terminal,
            )

    def _wait_terminal(self) -> LocalProcessTerminal:
        with self._condition:
            while self._terminal is None:
                self._condition.wait(_POLL_SECONDS)
            return self._terminal

    def _publish_terminal(self, terminal: LocalProcessTerminal) -> None:
        with self._condition:
            self._request = None
            self._pipes = None
            self._terminal = terminal
            self._condition.notify_all()

    def start(self, request: LocalProcessRequest) -> None:
        """Start the inert owner thread, then admit exactly one request."""
        if self._start_called or self._closed:
            raise RuntimeError("local process owner start is not available")
        self._start_called = True
        try:
            _thread.start_new_thread(_local_process_owner_entry, (self,))
        except BaseException as error:
            terminal, interruption = self._settle(
                error,
                dispatch_failed=isinstance(error, Exception),
            )
            if not terminal.cleaned and interruption is not None:
                interruption.add_note("Local carrier process cleanup did not complete within its bound.")
            if isinstance(interruption, Exception):
                return
            assert interruption is not None
            raise interruption from None

        try:
            admitted = self._admit(request)
            if not admitted:
                raise RuntimeError("local process admission failed")
        except BaseException as error:
            terminal, interruption = self._settle(error)
            if not terminal.cleaned and interruption is not None:
                interruption.add_note("Local carrier process cleanup did not complete within its bound.")
            assert interruption is not None
            raise interruption from None

    def close(self) -> LocalProcessTerminal:
        """Relinquish borrowed pipes and settle the one admitted process."""
        terminal, interruption = self._settle(None)
        if not terminal.cleaned and interruption is not None:
            interruption.add_note("Local carrier process cleanup did not complete within its bound.")
        if interruption is not None:
            raise interruption
        return terminal

    def _settle(
        self,
        interruption: BaseException | None,
        *,
        dispatch_failed: bool = False,
    ) -> tuple[LocalProcessTerminal, BaseException | None]:
        if self._closed:
            assert self._terminal is not None
            return self._terminal, interruption

        while True:
            try:
                admitted = self._cancel_if_waiting()
                break
            except BaseException as error:
                interruption = _retain_control_exception(interruption, error)

        if admitted:
            while True:
                try:
                    self._stop_borrowers()
                    break
                except BaseException as error:
                    interruption = _retain_control_exception(interruption, error)
            while True:
                try:
                    terminal = self._wait_terminal()
                    break
                except BaseException as error:
                    interruption = _retain_control_exception(interruption, error)
        else:
            terminal = LocalProcessTerminal(
                admitted=False,
                started=False,
                local_status=None,
                exit_status=None,
                cleaned=True,
                dispatch_failed=dispatch_failed,
            )
            self._publish_terminal(terminal)
        self._closed = True
        return terminal, interruption


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


def _run_local_process_owner(owner: LocalProcessOwner) -> None:
    """Own an admitted request and all process capabilities through cleanup."""
    request = owner._take_request()
    if request is None:
        return

    status: _ProcessStatus | None = None
    process: subprocess.Popen[bytes] | None = None
    pipes: LocalProcessPipes | None = None
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
        pipes = LocalProcessPipes(process.stdin, process.stdout, process.stderr)
        request = None
        owner._publish_ready(pipes)
        observation_failed, exit_status = owner._wait_for_borrowers(status)
    except BaseException:
        # Raw thread exceptions would expose request details through
        # sys.unraisablehook. Keep the owner closed and report only a category.
        observation_failed = started
        dispatch_failed = not started
        if started:
            owner._publish_observation_failure()
            if status is None:
                assert process is not None
                status = _ProcessStatus(process)
            while True:
                try:
                    owner._wait_until_borrowers_stopped()
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
        owner._publish_terminal(
            LocalProcessTerminal(
                admitted=True,
                started=started,
                local_status=local_status,
                exit_status=exit_status,
                cleaned=cleaned,
                dispatch_failed=dispatch_failed,
                observation_failed=observation_failed,
            )
        )


def _local_process_owner_entry(owner: LocalProcessOwner) -> None:
    """Keep every raw native-thread failure away from sys.unraisablehook."""
    with suppress(BaseException):
        _run_local_process_owner(owner)
        # The owner path itself contains the normal fail-closed publication.
        # This final guard exists only to keep raw thread diagnostics private.


def _retain_control_exception(
    interruption: BaseException | None,
    error: BaseException,
) -> BaseException:
    """Retain the first control exception over ordinary operational errors."""
    if interruption is None or (isinstance(interruption, Exception) and not isinstance(error, Exception)):
        return error
    return interruption


def _pump_owned_pipes(
    owner: LocalProcessOwner,
    pipes: LocalProcessPipes,
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
        request = LocalProcessRequest(
            tuple(argv),
            input.piped,
            None if env is None else tuple(env.items()),
            cwd,
            tuple(pass_fds),
            start_new_session,
        )
    except (OSError, ValueError):
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)

    owner = LocalProcessOwner()
    failure: ProcessFailure | None = None
    exit_status: int | None = None
    interruption: BaseException | None = None
    pipes: LocalProcessPipes | None = None
    terminal: LocalProcessTerminal | None = None
    try:
        owner.start(request)
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
        snapshot = owner.snapshot()
        if snapshot.terminal is not None and not snapshot.terminal.admitted:
            interruption = error
        else:
            failure = ProcessFailure.OBSERVATION
    except BaseException as error:
        interruption = error
    finally:
        try:
            terminal = owner.close()
        except BaseException as error:
            interruption = _retain_control_exception(interruption, error)
            terminal = owner.snapshot().terminal
        assert terminal is not None
        exit_status = terminal.exit_status
        if not terminal.cleaned and interruption is not None:
            interruption.add_note("Local carrier process cleanup did not complete within its bound.")
    if interruption is not None:
        raise interruption
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
