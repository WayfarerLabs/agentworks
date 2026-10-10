"""Stdlib-only bounded I/O for one owned local process.

Python 3.12 supports nonblocking anonymous pipes on Windows as well as POSIX.
The core remains Python 3.11-compatible for POSIX guest-helper reuse. A private
launch owner constructs and retains the process while the caller alone pumps
borrowed endpoints. The waiting runner leaves no task using a borrowed endpoint.
Bounded owner closure can return pending while construction or cleanup still
owns native capabilities; its caller must retain the same owner and caller-owned
stdin and pass_fds descriptors while construction is pending. A canceled,
capability-free bootstrap or terminal native return
tail may finish after return. Execution uses the supplied deadline. Local cleanup
uses a 0.5-second wait allowance; native calls are not made interruptible. That
allowance never resumes execution.

The launch admission handoff uses a condition lock for publication. It does not
assume unsynchronized Python object writes are portable. On POSIX, an external
concurrent reaper can still create a PID exit/reuse race before lost ownership
becomes observable.
"""

from __future__ import annotations

import _thread
import math
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

    def advance(self, pipes: LocalProcessPipes) -> tuple[bool, bool]:
        """Advance bounded input I/O and request EOF through the original records."""
        pipe = pipes.stdin
        assert pipe is not None
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
        if self.complete:
            try:
                pipes.close_stdin()
            except Exception:
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
    pipes: LocalProcessPipes | None = None
    process_retired: bool = False

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
            # CPython 3.11-3.14 gates Popen destructor polling on this flag.
            # Retire that bookkeeping without fabricating a returncode.
            self.process._child_created = False  # type: ignore[attr-defined]
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


class ResizeNotification(StrEnum):
    """Evidence available for one local terminal resize notification."""

    NOT_SENT = "not_sent"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


@dataclass
class _ResizeRequest:
    deadline: Deadline
    claimed: bool = False
    result: ResizeNotification | None = None


class _Admission(StrEnum):
    WAITING = "waiting"
    ADMITTED = "admitted"
    CANCELLED = "cancelled"


class LocalProcessInput(StrEnum):
    """Owned stdin endpoint selected without native descriptor access."""

    EOF = "eof"
    PIPE = "pipe"


@dataclass(frozen=True)
class BorrowedProcessStdin:
    """Caller-held descriptor retained until launch ownership settles.

    Construction neither inspects nor duplicates the descriptor. The caller
    must not close or reuse it before the owner's terminal publication.
    """

    descriptor: int = field(repr=False)

    def __post_init__(self) -> None:
        # Negative subprocess constants must not become borrowed descriptors.
        if type(self.descriptor) is not int or self.descriptor < 0:
            raise ValueError("borrowed process stdin requires a nonnegative descriptor")


@dataclass(frozen=True)
class LocalProcessRequest:
    """Immutable configuration for one local process admission."""

    argv: tuple[str, ...] = field(repr=False)
    input: LocalProcessInput | BorrowedProcessStdin
    env: tuple[tuple[str, str], ...] | None = field(default=None, repr=False)
    cwd: str | None = field(default=None, repr=False)
    pass_fds: tuple[int, ...] = field(default=(), repr=False)
    start_new_session: bool = False

    def __post_init__(self) -> None:
        if type(self.input) not in {LocalProcessInput, BorrowedProcessStdin}:
            raise ValueError("local process request requires an explicit stdin choice")


@dataclass(frozen=True)
class LocalProcessPipes:
    """Raw borrowed I/O endpoints with their original managed-close evidence.

    Borrowers use the streams for I/O and request stdin EOF through
    ``close_stdin``. They have no authority to close the raw streams.
    """

    stdin: IO[bytes] | None = field(repr=False)
    stdout: IO[bytes] = field(repr=False)
    stderr: IO[bytes] = field(repr=False)
    _records: list[_PipeClose] = field(default_factory=list, init=False, repr=False, compare=False)

    def _record(self, pipe: IO[bytes]) -> _PipeClose:
        for record in self._records:
            if record.pipe is pipe:
                return record
        record = _PipeClose(pipe)
        self._records.append(record)
        return record

    def _install_close_records(self) -> None:
        for pipe in (self.stdin, self.stdout, self.stderr):
            if pipe is not None:
                self._record(pipe)

    def close_stdin(self) -> None:
        """Request EOF once, retaining uncertainty if native close raises."""
        if self.stdin is not None:
            self._record(self.stdin).close()

    @property
    def _stdin_close_attempted(self) -> bool:
        return any(record.pipe is self.stdin and record.attempted for record in self._records)

    @property
    def _retired(self) -> bool:
        return all(
            any(record.pipe is pipe and record.confirmed for record in self._records)
            for pipe in (self.stdin, self.stdout, self.stderr)
            if pipe is not None
        )

    @property
    def _retryable(self) -> bool:
        return any(
            not any(record.pipe is pipe and record.attempted for record in self._records)
            for pipe in (self.stdin, self.stdout, self.stderr)
            if pipe is not None
        )


@dataclass
class _PipeClose:
    pipe: IO[bytes] = field(repr=False)
    attempted: bool = False
    confirmed: bool = False

    def close(self) -> None:
        if self.attempted:
            return
        self.attempted = True
        self.pipe.close()
        self.confirmed = True


@dataclass(frozen=True)
class LocalProcessTerminal:
    """Immutable facts from one cleanup attempt or denied admission.

    ``cleaned`` proves local capability retirement, independently of status.
    Uncertain close evidence and actual native objects stay with the owner.
    Later attempts publish new facts without changing this observation.
    """

    admitted: bool
    started: bool
    local_status: int | None
    exit_status: int | None
    cleaned: bool
    dispatch_failed: bool = False
    observation_failed: bool = False
    cleanup_retryable: bool = False


@dataclass(frozen=True)
class LocalProcessSnapshot:
    """One immutable observation of owner publication state."""

    pipes: LocalProcessPipes | None
    exit_status: int | None
    observation_failed: bool
    terminal: LocalProcessTerminal | None


class LocalProcessOwner:
    """Own one local process while callers borrow its published pipes.

    One caller serializes ``start`` and closure. Other threads may observe
    immutable snapshots, but pipe borrowers must stop before requesting closure.
    Pending construction still borrows caller-owned stdin and pass_fds descriptors.
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
        self._first_terminal: LocalProcessTerminal | None = None
        self._start_called = False
        self._closed = False
        self._resize_request: _ResizeRequest | None = None
        self._cleanup_retry_requested = False
        self._retained_status: _ProcessStatus | None = None
        self._retained_process: subprocess.Popen[bytes] | None = None
        self._retained_pipes: LocalProcessPipes | None = None

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
            if not self._borrowers_stopped:
                self._pipes = pipes
            self._condition.notify_all()

    def _publish_observation_failure(self) -> None:
        with self._condition:
            self._observation_failed = True
            self._deny_pending_resize_locked()
            self._condition.notify_all()

    def _deny_pending_resize_locked(self) -> None:
        request = self._resize_request
        if request is not None and not request.claimed:
            request.result = ResizeNotification.NOT_SENT
            self._resize_request = None

    def _wait_for_borrowers(self, status: _ProcessStatus) -> tuple[bool, int | None]:
        """Observe exact status until the caller relinquishes all pipe use."""
        while True:
            observed: int | None = None
            observation_failed = False
            if not self._observation_failed:
                try:
                    observed = status.poll()
                except OSError:
                    observation_failed = True
                else:
                    if status.lost:
                        observation_failed = True
            request_to_signal: _ResizeRequest | None = None
            with self._condition:
                if observation_failed:
                    self._observation_failed = True
                elif observed is not None and self._exit_status is None:
                    self._exit_status = observed
                request = self._resize_request
                if request is not None and not request.claimed:
                    if (
                        self._borrowers_stopped
                        or self._observation_failed
                        or self._exit_status is not None
                        or request.deadline.expired
                    ):
                        request.result = ResizeNotification.NOT_SENT
                        self._resize_request = None
                        self._condition.notify_all()
                    else:
                        request.claimed = True
                        request_to_signal = request
                if self._borrowers_stopped and request_to_signal is None:
                    return self._observation_failed, self._exit_status
                if request_to_signal is None:
                    self._condition.wait(_POLL_SECONDS)

            if request_to_signal is not None:
                try:
                    os.kill(status.process.pid, signal.SIGWINCH)
                except ProcessLookupError:
                    result = ResizeNotification.NOT_SENT
                except BaseException:
                    # The native call's outcome is not safe to infer, but its
                    # failure must not replace process status or cleanup facts.
                    result = ResizeNotification.UNKNOWN
                else:
                    result = ResizeNotification.REQUESTED
                with self._condition:
                    request_to_signal.result = result
                    if self._resize_request is request_to_signal:
                        self._resize_request = None
                    self._condition.notify_all()

    def notify_resize(self, deadline: Deadline) -> ResizeNotification:
        """Request one bounded SIGWINCH through the existing process owner."""
        expires_at = deadline.expires_at
        if expires_at is None:
            raise ValueError("resize notification requires a finite deadline")
        try:
            finite_deadline = math.isfinite(expires_at)
        except (OverflowError, TypeError):
            finite_deadline = False
        if not finite_deadline:
            raise ValueError("resize notification requires a finite deadline")
        if deadline.expired or os.name != "posix":
            return ResizeNotification.NOT_SENT

        with self._condition:
            if (
                self._closed
                or self._borrowers_stopped
                or self._pipes is None
                or self._terminal is not None
                or self._exit_status is not None
                or self._observation_failed
                or self._resize_request is not None
            ):
                return ResizeNotification.NOT_SENT
            request = _ResizeRequest(deadline)
            try:
                self._resize_request = request
                self._condition.notify_all()
                while request.result is None:
                    remaining = deadline.remaining()
                    if remaining is not None and remaining <= 0:
                        if not request.claimed and self._resize_request is request:
                            request.result = ResizeNotification.NOT_SENT
                            self._resize_request = None
                            self._condition.notify_all()
                            return request.result
                        return ResizeNotification.UNKNOWN
                    assert remaining is not None
                    self._condition.wait(min(remaining, _POLL_SECONDS))
                return request.result
            except BaseException:
                if request.result is None and not request.claimed and self._resize_request is request:
                    request.result = ResizeNotification.NOT_SENT
                    self._resize_request = None
                raise

    def _stop_borrowers(self) -> None:
        with self._condition:
            self._borrowers_stopped = True
            self._deny_pending_resize_locked()
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

    def _wait_terminal(self, deadline: Deadline | None = None, *, first: bool = True) -> LocalProcessTerminal | None:
        with self._condition:
            while True:
                terminal = self._first_terminal if first else self._terminal
                if terminal is not None:
                    return terminal
                remaining = None if deadline is None else deadline.remaining()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(_POLL_SECONDS if remaining is None else min(remaining, _POLL_SECONDS))

    def _publish_terminal(self, terminal: LocalProcessTerminal) -> None:
        with self._condition:
            resize_request = self._resize_request
            if resize_request is not None:
                if resize_request.result is None:
                    resize_request.result = (
                        ResizeNotification.UNKNOWN if resize_request.claimed else ResizeNotification.NOT_SENT
                    )
                self._resize_request = None
            self._request = None
            self._pipes = None
            self._terminal = terminal
            if self._first_terminal is None:
                self._first_terminal = terminal
            self._condition.notify_all()

    def start(self, request: LocalProcessRequest, *, close_deadline: Deadline | None = None) -> None:
        """Start the inert owner thread, then admit exactly one request."""
        if close_deadline is not None:
            self._validate_close_deadline(close_deadline)
        if self._start_called or self._closed:
            raise RuntimeError("local process owner start is not available")
        self._start_called = True
        try:
            _thread.start_new_thread(_local_process_owner_entry, (self,))
        except BaseException as error:
            terminal, interruption = self._settle(
                error,
                dispatch_failed=isinstance(error, Exception),
                deadline=close_deadline,
            )
            if (terminal is None or not terminal.cleaned) and interruption is not None:
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
            terminal, interruption = self._settle(error, deadline=close_deadline)
            if (terminal is None or not terminal.cleaned) and interruption is not None:
                interruption.add_note("Local carrier process cleanup did not complete within its bound.")
            assert interruption is not None
            raise interruption from None

    def close(self) -> LocalProcessTerminal:
        """Relinquish borrowed pipes and wait for the first cleanup observation."""
        terminal, interruption = self._settle(None)
        assert terminal is not None
        if not terminal.cleaned and interruption is not None:
            interruption.add_note("Local carrier process cleanup did not complete within its bound.")
        if interruption is not None:
            raise interruption
        return terminal

    @staticmethod
    def _validate_close_deadline(deadline: Deadline) -> None:
        expires_at = deadline.expires_at
        try:
            finite = expires_at is not None and math.isfinite(expires_at)
        except (TypeError, OverflowError):
            finite = False
        if not finite:
            raise ValueError("bounded closure requires a finite deadline")

    def close_bounded(self, deadline: Deadline) -> LocalProcessTerminal | None:
        """Request cleanup, returning None if its observation deadline expires.

        The caller must cease pipe use first and retain this owner until positive
        retirement, including permanent close uncertainty. A pending constructor needs caller-owned stdin
        and pass_fds descriptors; pending return does not permit closing or
        reusing them or restoring terminal modes.
        Native construction and syscalls are not made interruptible. Calling
        again after a retryable failure requests one serialized cleanup retry.
        """
        self._validate_close_deadline(deadline)
        with self._condition:
            first = not self._closed or self._first_terminal is None
            if self._terminal is not None and self._terminal.cleanup_retryable:
                self._terminal = None
                self._cleanup_retry_requested = True
                self._condition.notify_all()
                first = False
        terminal, interruption = self._settle(None, deadline=deadline, first=first)
        if interruption is not None:
            if terminal is None or not terminal.cleaned:
                interruption.add_note("Local carrier process cleanup did not complete within its bound.")
            raise interruption
        return terminal

    def _settle(
        self,
        interruption: BaseException | None,
        *,
        dispatch_failed: bool = False,
        deadline: Deadline | None = None,
        first: bool = True,
    ) -> tuple[LocalProcessTerminal | None, BaseException | None]:
        with self._condition:
            terminal = self._first_terminal if first else self._terminal
            if self._closed and terminal is not None:
                return terminal, interruption
            self._closed = True

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
                    terminal = self._wait_terminal() if deadline is None else self._wait_terminal(deadline, first=first)
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
        return terminal, interruption

    def _wait_cleanup_retry_or_exit(self, status: _ProcessStatus | None) -> None:
        """Keep exact custody while observing exit, without unsolicited signals."""
        while True:
            with self._condition:
                if self._cleanup_retry_requested:
                    self._cleanup_retry_requested = False
                    return
            try:
                # Only a newly observed exit permits unsolicited bookkeeping
                # cleanup. Failure after a known exit requires explicit retry.
                exited = status is not None and status.status is None and status.poll() is not None
            except BaseException:
                exited = False
            if exited or (status is not None and status.lost):
                # Cleanup of an observed exit only closes pipes and retires
                # native bookkeeping. Lost ownership never authorizes a signal.
                with self._condition:
                    self._terminal = None
                return
            with self._condition:
                if not self._cleanup_retry_requested:
                    self._condition.wait(_POLL_SECONDS)


def _cleanup(status: _ProcessStatus) -> bool:
    """Attempt independent original closes and safe process retirement."""
    process = status.process
    if status.pipes is None:
        try:
            assert process.stdout is not None and process.stderr is not None
            status.pipes = LocalProcessPipes(process.stdin, process.stdout, process.stderr)
        except BaseException:
            # Record allocation failure cannot prevent independent process cleanup.
            pass
    pipes = status.pipes
    if pipes is not None:
        for pipe in (pipes.stdin, pipes.stdout, pipes.stderr):
            if pipe is not None:
                # A marked attempt is permanently uncertain. Other endpoints
                # and separately held process authority remain independent.
                with suppress(BaseException):
                    pipes._record(pipe).close()
    status.process_retired = _cleanup_process(status)
    return status.process_retired and pipes is not None and pipes._retired


def _cleanup_retryable(status: _ProcessStatus) -> bool:
    pipes = status.pipes
    return (
        pipes is None
        or pipes._retryable
        or (not status.lost and not status.process_retired and (os.name == "nt" or status.status is None))
    )


def _cleanup_process(status: _ProcessStatus) -> bool:
    """Bound local kill/reap; exact POSIX wait loss retires numeric PID authority."""
    process = status.process
    if status.lost:
        return True
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
        return True
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
        if status.lost:
            return True
        if remaining <= 0:
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
                stdin=(
                    request.input.descriptor
                    if isinstance(request.input, BorrowedProcessStdin)
                    else subprocess.PIPE
                    if request.input is LocalProcessInput.PIPE
                    else subprocess.DEVNULL
                ),
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
        owner._retained_process = process
        status = _ProcessStatus(process)
        owner._retained_status = status
        assert process.stdout is not None and process.stderr is not None
        pipes = LocalProcessPipes(process.stdin, process.stdout, process.stderr)
        status.pipes = pipes
        owner._retained_pipes = pipes
        pipes._install_close_records()
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
                with suppress(BaseException):
                    status = _ProcessStatus(process)
                    owner._retained_status = status
            while True:
                try:
                    owner._wait_until_borrowers_stopped()
                    break
                except BaseException:
                    continue
    finally:
        request = None
        while True:
            if status is None and process is not None:
                with suppress(BaseException):
                    status = _ProcessStatus(process)
                    owner._retained_status = status
            if status is not None:
                try:
                    cleaned = _cleanup(status)
                except BaseException:
                    cleaned = False
                local_status = status.status
                observation_failed |= status.lost
                owner._retained_pipes = status.pipes
                cleanup_retryable = not cleaned and _cleanup_retryable(status)
            else:
                local_status = None
                cleaned = process is None
                cleanup_retryable = process is not None
            if cleaned:
                owner._retained_status = None
                owner._retained_process = None
                owner._retained_pipes = None
            if not cleanup_retryable:
                status = None
                process = None
                pipes = None
            owner._publish_terminal(
                LocalProcessTerminal(
                    admitted=True,
                    started=started,
                    local_status=local_status,
                    exit_status=exit_status,
                    cleaned=cleaned,
                    dispatch_failed=dispatch_failed,
                    observation_failed=observation_failed,
                    cleanup_retryable=cleanup_retryable,
                )
            )
            if not cleanup_retryable:
                break
            owner._wait_cleanup_retry_or_exit(status)


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
        if pipes.stdin is not None and not pipes._stdin_close_attempted:
            if exit_status is not None and not input_state.complete:
                failure = ProcessFailure.INPUT
                break
            input_progressed, input_failed = input_state.advance(pipes)
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
    owner: LocalProcessOwner,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    pass_fds: tuple[int, ...] = (),
    start_new_session: bool = False,
    cleanup_allowance: float | None = None,
) -> ProcessResult:
    """Fairly pump bounded input and output without retaining borrowed endpoints.

    Passed descriptors remain caller-owned; only child inheritance is configured.
    POSIX session creation is a launch control, not descendant containment.
    The caller retains the owner, including permanent close uncertainty.
    A finite cleanup allowance selects bounded observation. Pending construction
    still requires the caller to retain any passed descriptors until settlement.
    """
    if cleanup_allowance is not None and (not math.isfinite(cleanup_allowance) or cleanup_allowance < 0):
        raise ValueError("bounded process closure requires a finite allowance")

    def close_budget() -> Deadline | None:
        return None if cleanup_allowance is None else Deadline(time.monotonic() + cleanup_allowance)

    stdout = _Output(output.capture_limit, output.stdout_sink)
    stderr = _Output(output.capture_limit, output.stderr_sink)
    if deadline.expired:
        budget = close_budget()
        owner.close_bounded(budget) if budget is not None else owner.close()
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DEADLINE)

    try:
        request = LocalProcessRequest(
            tuple(argv),
            LocalProcessInput.PIPE if input.piped else LocalProcessInput.EOF,
            None if env is None else tuple(env.items()),
            cwd,
            tuple(pass_fds),
            start_new_session,
        )
    except (OSError, ValueError):
        budget = close_budget()
        owner.close_bounded(budget) if budget is not None else owner.close()
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)

    failure: ProcessFailure | None = None
    exit_status: int | None = None
    interruption: BaseException | None = None
    pipes: LocalProcessPipes | None = None
    terminal: LocalProcessTerminal | None = None
    start_returned = False
    try:
        owner.start(request, close_deadline=close_budget())
        start_returned = True
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
            budget = close_budget()
            if budget is None:
                terminal = owner.close()
            else:
                # Admission interruption may already have requested cleanup.
                # Its failed observation is not permission for an implicit retry.
                terminal = owner.snapshot().terminal
                if terminal is None and start_returned:
                    terminal = owner.close_bounded(budget)
        except BaseException as error:
            interruption = _retain_control_exception(interruption, error)
            # Waiting close preserves its first observation even while a later
            # natural-exit cleanup makes the current snapshot pending again.
            terminal = owner.close() if cleanup_allowance is None else owner.snapshot().terminal
        if terminal is not None:
            exit_status = terminal.exit_status
        if (terminal is None or not terminal.cleaned) and interruption is not None:
            interruption.add_note("Local carrier process cleanup did not complete within its bound.")
    if interruption is not None:
        raise interruption
    if terminal is None:
        # Construction may already be admitted without published pipes. The
        # retained owner, not started=False, establishes unresolved delivery.
        return ProcessResult(
            pipes is not None, None, exit_status, stdout.report(), stderr.report(), ProcessFailure.OBSERVATION
        )
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
