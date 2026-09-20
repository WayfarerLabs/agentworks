"""Bounded, single-thread I/O for one owned local carrier process.

Python 3.12 supports nonblocking anonymous pipes on Windows as well as POSIX.
No thread or borrowed stream survives return. Execution uses the caller's
deadline; killing and reaping the local process gets at most 0.5 seconds more.
That allowance never resumes execution. Process results establish neither guest
dispatch nor target termination.

Cleanup is guarded only after process construction and loop-state initialization.
An earlier control-flow interruption can leave a child alive, including without
a returned handle; this pump does not satisfy the launch-interruption contract.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._byte_io import SinkWriteError, try_write_to_sink
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    Failure,
    FiniteInput,
    LiveInput,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import IO

    from agentworks.execution.carrier import ByteSink, ByteSource, CarrierIO, Deadline

_CHUNK = 65_536
_POLL_SECONDS = 0.01
_CLEANUP_SECONDS = 0.5
_EXIT_DRAIN_SECONDS = 0.1


@dataclass
class _Output:
    retention: Retention
    limit: int
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
        elif self.retention == Retention.CAPTURED:
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

    def report(self) -> CapturedOutput:
        complete = self.eof and self.pending is None and not self.limited
        return CapturedOutput(bytes(self.data), complete, retention=self.retention)


@dataclass
class _Input:
    source: ByteSource | None = field(default=None, repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    eof: bool = False

    @classmethod
    def from_io(cls, io: CarrierIO) -> _Input:
        if isinstance(io.input, FiniteInput):
            return cls(pending=memoryview(io.input.data), eof=True)
        if isinstance(io.input, LiveInput):
            return cls(source=io.input.source)
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


@dataclass(frozen=True)
class ProcessResult:
    started: bool
    local_status: int | None
    # Only a status observed before local termination can establish completion.
    exit_status: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    failure: Failure | None


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
        while True:
            try:
                pid, wait_status = os.waitpid(self.process.pid, os.WNOHANG)
                break
            except InterruptedError:
                continue
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


def output_retention(io: CarrierIO) -> Retention:
    """Use one retention policy for pre-dispatch and process evidence."""
    return (
        Retention.DELIVERED
        if isinstance(io.output, SinkOutput)
        else Retention.SUPPRESSED
        if io.sensitive
        else Retention.CAPTURED
        if isinstance(io.output, Capture)
        else Retention.DISCARDED
    )


def run_process(
    argv: list[str],
    *,
    io: CarrierIO,
    deadline: Deadline,
    env: Mapping[str, str] | None = None,
    live_stdio: bool = False,
) -> ProcessResult:
    """Fairly pump bounded input and output without retaining borrowed endpoints."""
    retention = output_retention(io)
    limit = io.output.max_bytes if isinstance(io.output, Capture) else 0
    stdout_sink = io.output.stdout if isinstance(io.output, SinkOutput) else None
    stderr_sink = io.output.stderr if isinstance(io.output, SinkOutput) else None
    stdout = _Output(retention, limit, stdout_sink)
    stderr = _Output(retention, limit, stderr_sink)
    requires_live = isinstance(io.input, LiveInput) or (isinstance(io.output, SinkOutput) and io.output.require_live)
    if requires_live and not live_stdio:
        raise ValidationError("Live carrier I/O is unavailable on this channel")
    if deadline.expired:
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), Failure.DEADLINE)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if isinstance(io.input, FiniteInput | LiveInput) else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
    except (OSError, ValueError):
        return ProcessResult(False, None, None, stdout.report(), stderr.report(), Failure.DISPATCH)

    status = _ProcessStatus(process)
    failure: Failure | None = None
    exit_status: int | None = None
    post_exit_drain: _PostExitDrain | None = None
    interruption: BaseException | None = None
    try:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
        assert process.stdout is not None and process.stderr is not None
        input_state = _Input.from_io(io)
        output_first = True
        while True:
            exit_status = status.poll()
            if status.lost:
                failure = Failure.OBSERVATION
                break
            if deadline.expired:
                failure = Failure.DEADLINE
                break
            pending_delivery = stdout.pending is not None or stderr.pending is not None
            if exit_status is not None:
                if post_exit_drain is None:
                    post_exit_drain = _PostExitDrain(paused=pending_delivery)
                elif post_exit_drain.update(paused=pending_delivery):
                    # A continuously writing descendant must not extend even
                    # an explicitly unbounded invocation after its client exits.
                    failure = Failure.OUTPUT
                    break
            progressed = False
            outputs = list(
                ((stdout, process.stdout), (stderr, process.stderr))
                if output_first
                else ((stderr, process.stderr), (stdout, process.stdout))
            )
            output_first = not output_first
            if exit_status is not None and pending_delivery:
                outputs = [item for item in outputs if item[0].pending is not None]
            for output, pipe in outputs:
                try:
                    output_progressed, output_failed = output.advance(pipe)
                except OSError:
                    output_failed = True
                    output_progressed = False
                progressed = output_progressed or progressed
                if output_failed:
                    failure = Failure.OUTPUT
                    break
            if failure is not None:
                break
            if process.stdin is not None and not process.stdin.closed:
                if exit_status is not None and not input_state.complete:
                    failure = Failure.INPUT
                    break
                input_progressed, input_failed = input_state.advance(process.stdin)
                progressed = input_progressed or progressed
                if input_failed:
                    failure = Failure.INPUT
                    break
            if exit_status is not None:
                if not input_state.complete:
                    failure = Failure.INPUT
                    break
                if stdout.eof and stderr.eof:
                    break
                assert post_exit_drain is not None
                pending_delivery = stdout.pending is not None or stderr.pending is not None
                if post_exit_drain.update(paused=pending_delivery):
                    failure = Failure.OUTPUT
                    break
            if not progressed:
                remaining = deadline.remaining()
                time.sleep(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))
    except OSError:
        failure = Failure.OBSERVATION
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
        failure = Failure.OBSERVATION
    elif failure is None and (stdout.limited or stderr.limited):
        failure = Failure.OUTPUT_LIMIT
    return ProcessResult(True, status.status, exit_status, stdout.report(), stderr.report(), failure)
