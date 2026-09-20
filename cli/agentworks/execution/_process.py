"""Stdlib-only bounded I/O for one owned local process.

Python 3.12 supports nonblocking anonymous pipes on Windows as well as POSIX.
The core remains Python 3.11-compatible for POSIX guest-helper reuse. No thread
or borrowed stream survives return. Execution uses the supplied deadline;
killing and reaping the local process gets at most 0.5 seconds more. That
allowance never resumes execution.

Cleanup is guarded only after process construction and loop-state initialization.
An earlier control-flow interruption can leave a child alive, including without
a returned handle. On POSIX, an external concurrent reaper can still create a
PID exit/reuse race before lost ownership becomes observable.
"""

from __future__ import annotations

import os
import signal
import subprocess
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


def run_owned_process(
    argv: list[str],
    *,
    input: ProcessInput,
    output: ProcessOutput,
    deadline: Deadline,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Fairly pump bounded input and output without retaining borrowed endpoints."""
    stdout = _Output(output.capture_limit, output.stdout_sink)
    stderr = _Output(output.capture_limit, output.stderr_sink)
    if deadline.expired:
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DEADLINE)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input.piped else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
    except (OSError, ValueError):
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), ProcessFailure.DISPATCH)

    status = _ProcessStatus(process)
    failure: ProcessFailure | None = None
    exit_status: int | None = None
    post_exit_drain: _PostExitDrain | None = None
    interruption: BaseException | None = None
    try:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
        assert process.stdout is not None and process.stderr is not None
        input_state = _Input.from_spec(input)
        output_first = True
        while True:
            exit_status = status.poll()
            if status.lost:
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
                    # A continuously writing descendant must not extend even
                    # an explicitly unbounded invocation after its client exits.
                    failure = ProcessFailure.OUTPUT
                    break
            progressed = False
            outputs = list(
                ((stdout, process.stdout), (stderr, process.stderr))
                if output_first
                else ((stderr, process.stderr), (stdout, process.stdout))
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
            if process.stdin is not None and not process.stdin.closed:
                if exit_status is not None and not input_state.complete:
                    failure = ProcessFailure.INPUT
                    break
                input_progressed, input_failed = input_state.advance(process.stdin)
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
    except OSError:
        failure = ProcessFailure.OBSERVATION
    except BaseException as error:
        interruption = error
        raise
    finally:
        # Preserve natural completion even when an I/O failure wins the race.
        if exit_status is None and not status.lost:
            with suppress(OSError):
                exit_status = status.poll()
        cleaned = _cleanup(status)
        if not cleaned and interruption is not None:
            interruption.add_note("Local carrier process cleanup did not complete within its bound.")
    if not cleaned:
        failure = ProcessFailure.OBSERVATION
    elif failure is None and (stdout.limited or stderr.limited):
        failure = ProcessFailure.OUTPUT_LIMIT
    return ProcessResult(True, status.status, exit_status, stdout.report(), stderr.report(), failure)
