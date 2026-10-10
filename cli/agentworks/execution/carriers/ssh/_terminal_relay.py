"""Private POSIX terminal relay using the shared held-process owner.

The SSH carrier does not enable this path yet. Local resize notification uses
the shared process owner; real SSH and native platform acceptance remain open.
Readiness interpretation and emulator sanitation remain preparation policy.
"""

from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass, field, replace
from threading import Condition, Event, Thread
from typing import TYPE_CHECKING

from agentworks.execution._process import (
    BorrowedProcessStdin,
    LocalProcessOwner,
    LocalProcessRequest,
    LocalProcessTerminal,
    ResizeNotification,
    SinkWriteError,
    _retain_control_exception,
    try_write_to_sink,
)
from agentworks.execution._process import (
    Deadline as ProcessDeadline,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    Deadline,
    Failure,
    Provenance,
    Retention,
    SinkOutput,
    TerminalInput,
)
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.ssh._terminal_posix import AcquisitionCleanupFailure, PosixTerminal

if TYPE_CHECKING:
    from typing import IO

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import ByteSink

_CHUNK = 65_536
_POLL_SECONDS = 0.01
_EXIT_DRAIN_SECONDS = 0.1
_RESIZE_SECONDS = 0.1
_CLEANUP_SECONDS = 0.5


@dataclass
class _Stream:
    sink: ByteSink = field(repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    eof: bool = False
    quota: int | None = None
    probed: bool = False
    observation_failed: bool = False

    @property
    def finished(self) -> bool:
        return self.eof or ((self.probed or self.observation_failed) and self.pending is None and self.quota == 0)

    def freeze(self, pipe: IO[bytes]) -> None:
        """Fix the unread FIFO prefix once; pending bytes have already been collected."""
        if self.eof:
            return
        import array
        import fcntl
        import termios

        count = array.array("i", [0])
        fcntl.ioctl(pipe.fileno(), termios.FIONREAD, count, True)
        if count[0] < 0:
            raise OSError("Terminal pipe backlog observation failed")
        self.quota = count[0]

    def advance(self, pipe: IO[bytes]) -> bool:
        """Preserve pending and frozen bytes, then make only one bounded EOF probe."""
        if self.finished:
            return False
        if self.pending is None:
            probe = self.quota == 0
            if probe:
                self.probed = True
            limit = 1 if probe else _CHUNK if self.quota is None else min(_CHUNK, self.quota)
            try:
                chunk = os.read(pipe.fileno(), limit)
            except BlockingIOError:
                if self.quota not in (None, 0):
                    raise OSError("Terminal frozen pipe prefix was unavailable") from None
                return False
            if not chunk:
                self.eof = True
                self.quota = 0
                return True
            if self.quota is not None and not probe:
                self.quota -= len(chunk)
            self.pending = memoryview(chunk)
        written = try_write_to_sink(self.sink, self.pending)
        if written is None:
            return False
        self.pending = self.pending[written:] if written < len(self.pending) else None
        return True


@dataclass
class _Input:
    spec: TerminalInput = field(repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    keyboard: bool = False

    def advance(self, master_fd: int) -> bool:
        """Withhold keys until bootstrap EOF after its last pending byte drains."""
        chunk: bytes | None
        if self.pending is None:
            if self.keyboard:
                ready, _, _ = select.select([self.spec.input_fd], [], [], 0)
                if not ready:
                    return False
                try:
                    chunk = os.read(self.spec.input_fd, _CHUNK)
                except BlockingIOError:
                    return False
                if not chunk:
                    raise OSError("Terminal keyboard input closed")
            else:
                chunk = self._read_bootstrap()
                if chunk is None:
                    return False
                if not chunk:
                    return True
            self.pending = memoryview(chunk)
        try:
            written = os.write(master_fd, self.pending)
        except BlockingIOError:
            return False
        if written <= 0:
            raise OSError("Terminal input write failed")
        self.pending = self.pending[written:] if written < len(self.pending) else None
        return True

    def _read_bootstrap(self) -> bytes | None:
        failed = False
        try:
            chunk = self.spec.bootstrap.try_read(_CHUNK)
        except Exception:
            failed = True
            chunk = None
        if failed or (chunk is not None and (type(chunk) is not bytes or len(chunk) > _CHUNK)):
            raise OSError("Terminal bootstrap input failed") from None
        if chunk == b"":
            self.keyboard = True
        return chunk

    def complete_after_exit(self) -> bool:
        """Observe a final handoff after output delivery without sending new bytes."""
        if self.pending is not None:
            return False
        if not self.keyboard:
            self._read_bootstrap()
        return self.keyboard


class _Attempt:
    """One caller-retained coordinator and original native terminal worker.

    Cancellation and possible native admission share a guard. Only the worker
    borrows pipes, closes an admitted owner, and releases its passive terminal.
    Caller cleanup waits are finite; pending construction and lost terminal
    release remain reachable through the same delivery custody.
    """

    def __init__(self, argv: list[str], io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody) -> None:
        assert isinstance(io.input, TerminalInput) and isinstance(io.output, SinkOutput)
        self._owner = custody.begin_process()
        self._argv = tuple(argv)
        self._spec = io.input
        self._output = io.output
        self._deadline = deadline
        self._condition = Condition()
        self._admitted = False
        self._cancelled = False
        self._native_start_attempted = False
        self._stop = Event()
        self._operation_done = Event()
        self._done = Event()
        self._result: ProcessResult | None = None
        self._interruption: BaseException | None = None
        self._cleanup_deadline: Deadline | None = None
        self._cleanup_requested = 0
        self._cleanup_observed = 0
        self._worker = Thread(target=self._entry, name="ssh-terminal", daemon=False)
        self._terminal = PosixTerminal(self._spec.input_fd, self._spec.output_fd, self._worker)
        custody.retain_cleanup(self._owner, self)

    @property
    def settled(self) -> bool:
        return self._done.is_set() and self._terminal.settled

    def run(self) -> ProcessResult:
        """Wait for operation completion, then observe cleanup within a fresh finite bound."""
        expired = False
        try:
            self._worker.start()
            with self._condition:
                if not self._cancelled:
                    self._admitted = True
                self._condition.notify_all()
            while not self._operation_done.is_set():
                remaining = self._deadline.remaining()
                if remaining is not None and remaining <= 0:
                    expired = True
                    break
                self._operation_done.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))
        except BaseException as error:
            if isinstance(error, Exception):
                self._result = _result(None, False, False, Failure.DISPATCH)
            else:
                self._record_control(error)
        finally:
            try:
                self.close(Deadline.after(_CLEANUP_SECONDS))
            except BaseException as error:
                if isinstance(error, Exception):
                    raise
                self._record_control(error)
        interruption = self._record_control(None)
        result = self._result
        if result is None:
            result = self._observe_result(False, False, Failure.DEADLINE if expired else Failure.OBSERVATION)
            self._result = result
        if not self.settled and result.failure is None:
            result = replace(result, failure=Failure.OBSERVATION)
            self._result = result
        if interruption is not None:
            if not self.settled or result.failure is Failure.OBSERVATION:
                interruption.add_note("Terminal local cleanup or observation was uncertain.")
            raise interruption from None
        return result

    def close(self, deadline: Deadline) -> None:
        """Request one fresh cleanup observation without racing admitted borrowers."""
        assert deadline.expires_at is not None
        interruption: BaseException | None = None
        while True:
            try:
                with self._condition:
                    self._cancelled = True
                    self._stop.set()
                    self._cleanup_requested += 1
                    request = self._cleanup_requested
                    self._cleanup_deadline = deadline
                    admitted = self._admitted
                    self._condition.notify_all()
                break
            except BaseException as error:
                if isinstance(error, Exception):
                    raise
                interruption = _retain_control_exception(interruption, error)
                self._record_control(error)
        if not admitted:
            # A late inert worker sees cancellation before terminal or pipe use.
            # No terminal acquisition or native start can have been admitted.
            try:
                self._owner.close_bounded(ProcessDeadline(deadline.expires_at))
            except BaseException as error:
                if isinstance(error, Exception):
                    raise
                interruption = _retain_control_exception(interruption, error)
                self._record_control(error)
            self._operation_done.set()
            self._done.set()
        else:
            while not self._done.is_set():
                remaining = deadline.remaining()
                if remaining is None or remaining <= 0:
                    break
                try:
                    with self._condition:
                        if self._cleanup_observed >= request:
                            break
                    self._done.wait(min(_POLL_SECONDS, remaining))
                except BaseException as error:
                    if isinstance(error, Exception):
                        raise
                    interruption = _retain_control_exception(interruption, error)
                    self._record_control(error)
                    # Repeated signals do not renew this caller's cleanup bound.
        if interruption is not None:
            raise interruption from None

    def _record_control(self, error: BaseException | None) -> BaseException | None:
        """Select the first observed control exception across caller and worker."""
        while True:
            try:
                with self._condition:
                    if error is not None:
                        self._interruption = _retain_control_exception(self._interruption, error)
                    return self._interruption
            except BaseException as later:
                error = _retain_control_exception(error, later)

    def _entry(self) -> None:
        with self._condition:
            while not self._admitted and not self._cancelled:
                self._condition.wait()
            if not self._admitted:
                return
        stdout, stderr = _Stream(self._output.stdout), _Stream(self._output.stderr)
        failure: Failure | None = None
        try:
            failure = self._work(stdout, stderr)
        except Exception as error:
            failure = (
                Failure.OBSERVATION
                if self._native_start_attempted
                or not self._terminal.settled
                or isinstance(error.__cause__, AcquisitionCleanupFailure)
                else Failure.DISPATCH
            )
        except BaseException as error:
            self._record_control(error)
            if not self._terminal.settled or self._native_start_attempted:
                failure = Failure.OBSERVATION
        finally:
            # Pump borrowing has ceased. The caller may now request bounded
            # native cleanup; a pending constructor still borrows the PTY slave.
            self._result = self._observe_result(stdout.eof, stderr.eof, failure)
            self._operation_done.set()
            self._cleanup(stdout, stderr, failure)

    def _work(self, stdout: _Stream, stderr: _Stream) -> Failure | None:
        if self._deadline.expired or self._stop.is_set():
            return Failure.DEADLINE
        self._terminal.acquire()
        env = dict(os.environ)
        env["TERM"] = self._spec.term
        request = LocalProcessRequest(
            self._argv,
            BorrowedProcessStdin(self._terminal.slave_fd),
            tuple(env.items()),
            start_new_session=True,
        )
        with self._condition:
            if self._cancelled or self._deadline.expired:
                return Failure.DEADLINE
            # Possible native effect, even if start's reply or construction is lost.
            self._native_start_attempted = True
        self._owner.start(request, close_deadline=ProcessDeadline(time.monotonic() + _CLEANUP_SECONDS))
        return self._pump(self._owner, self._terminal, stdout, stderr)

    def _cleanup(self, stdout: _Stream, stderr: _Stream, failure: Failure | None) -> None:
        """The original worker retains pending native ownership until safe release."""
        observed = 0
        while True:
            with self._condition:
                request = self._cleanup_requested
                deadline = self._cleanup_deadline
            process = self._owner.snapshot().terminal
            if request > observed and deadline is not None:
                try:
                    process = self._owner.close_bounded(ProcessDeadline(deadline.expires_at))
                except BaseException as error:
                    if not isinstance(error, Exception):
                        self._record_control(error)
                    failure = Failure.OBSERVATION
                    process = self._owner.snapshot().terminal
                observed = request
            if process is not None and (process.cleaned or not process.cleanup_retryable):
                if process.cleaned:
                    errors = self._terminal.release()
                    if errors or process.observation_failed:
                        failure = Failure.OBSERVATION
                    elif process.dispatch_failed:
                        failure = Failure.DISPATCH
                    for release_error in errors:
                        if not isinstance(release_error, Exception):
                            self._record_control(release_error)
                else:
                    # Permanent native loss cannot be repaired by a fresh close.
                    # Finish without restoring or relinquishing terminal custody.
                    failure = Failure.OBSERVATION
                stdout.pending = stderr.pending = None
                self._result = self._observe_result(stdout.eof, stderr.eof, failure)
                self._done.set()
                with self._condition:
                    self._cleanup_observed = observed
                    self._condition.notify_all()
                return
            self._result = self._observe_result(stdout.eof, stderr.eof, failure or Failure.OBSERVATION)
            with self._condition:
                self._cleanup_observed = observed
                self._condition.notify_all()
                if self._cleanup_requested == observed:
                    # Pending native construction can settle autonomously. A
                    # published failure needs an explicit fresh cleanup request.
                    self._condition.wait(_POLL_SECONDS if process is None else None)

    def _observe_result(self, stdout_complete: bool, stderr_complete: bool, failure: Failure | None) -> ProcessResult:
        snapshot = self._owner.snapshot()
        process = snapshot.terminal
        if process is None:
            # Attempted admission is possible effect, never positive dispatch proof.
            process = LocalProcessTerminal(
                self._native_start_attempted,
                self._native_start_attempted,
                None,
                snapshot.exit_status,
                False,
                observation_failed=snapshot.observation_failed,
            )
        return _result(process, stdout_complete, stderr_complete, failure)

    def _pump(
        self,
        owner: LocalProcessOwner,
        terminal: PosixTerminal,
        stdout: _Stream,
        stderr: _Stream,
    ) -> Failure | None:
        pipes = None
        while pipes is None:
            snapshot = owner.snapshot()
            if snapshot.terminal is not None:
                return None
            if snapshot.observation_failed:
                return Failure.OBSERVATION
            if self._deadline.expired or self._stop.is_set():
                return Failure.DEADLINE
            pipes = snapshot.pipes
            if pipes is None:
                self._pause()
        for pipe in (pipes.stdout, pipes.stderr):
            os.set_blocking(pipe.fileno(), False)
        input_state = _Input(self._spec)
        output_first = True
        cutoff: float | None = None
        frozen = False
        try:
            while True:
                snapshot = owner.snapshot()
                if snapshot.observation_failed:
                    return Failure.OBSERVATION
                if self._deadline.expired or self._stop.is_set():
                    return Failure.DEADLINE
                pending = stdout.pending is not None or stderr.pending is not None
                if snapshot.exit_status is not None:
                    if cutoff is None:
                        cutoff = time.monotonic() + _EXIT_DRAIN_SECONDS
                    if not frozen and time.monotonic() >= cutoff:
                        for stream, pipe in ((stdout, pipes.stdout), (stderr, pipes.stderr)):
                            try:
                                stream.freeze(pipe)
                            except (OSError, AttributeError):
                                # Preserve collected pending bytes, but an unknown
                                # queued prefix admits no new collection or EOF probe.
                                stream.observation_failed = True
                                stream.quota = 0
                        frozen = True
                progressed = False
                outputs = (
                    ((stdout, pipes.stdout), (stderr, pipes.stderr))
                    if output_first
                    else ((stderr, pipes.stderr), (stdout, pipes.stdout))
                )
                output_first = not output_first
                for stream, pipe in outputs:
                    if self._deadline.expired or self._stop.is_set():
                        return Failure.DEADLINE
                    if snapshot.exit_status is not None and not frozen and stream.pending is None and pending:
                        continue
                    try:
                        progressed = stream.advance(pipe) or progressed
                    except (OSError, SinkWriteError):
                        return Failure.OUTPUT
                if self._deadline.expired or self._stop.is_set():
                    return Failure.DEADLINE
                if snapshot.exit_status is not None:
                    if frozen and stdout.finished and stderr.finished and not (stdout.eof and stderr.eof):
                        return Failure.OUTPUT
                    if stdout.eof and stderr.eof:
                        try:
                            return None if input_state.complete_after_exit() else Failure.INPUT
                        except OSError:
                            return Failure.INPUT
                else:
                    try:
                        progressed = input_state.advance(terminal.master_fd) or progressed
                    except OSError:
                        return Failure.INPUT
                    try:
                        if terminal.refresh_dimensions():
                            resize_failure = self._notify_resize(owner)
                            if resize_failure is not None:
                                return resize_failure
                    except BaseException as error:
                        if not isinstance(error, Exception):
                            self._record_control(error)
                        return Failure.OBSERVATION
                if not progressed:
                    self._pause()
        finally:
            input_state.pending = None

    def _notify_resize(self, owner: LocalProcessOwner) -> Failure | None:
        """Bound local notification without treating signal acceptance as remote proof."""
        expires_at = time.monotonic() + _RESIZE_SECONDS
        if self._deadline.expires_at is not None:
            expires_at = min(expires_at, self._deadline.expires_at)
        notification = owner.notify_resize(ProcessDeadline(expires_at))
        if notification is ResizeNotification.REQUESTED:
            return None
        if notification is ResizeNotification.UNKNOWN:
            # A claimed native request remains owned until process settlement.
            return Failure.OBSERVATION
        snapshot = owner.snapshot()
        if snapshot.observation_failed:
            return Failure.OBSERVATION
        if self._deadline.expired or self._stop.is_set():
            return Failure.DEADLINE
        # Natural exit needs no new window size. Preserve its completion and
        # continue bounded output draining rather than infer failed execution.
        return None if snapshot.exit_status is not None else Failure.OBSERVATION

    def _pause(self) -> None:
        remaining = self._deadline.remaining()
        self._stop.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))


def _result(
    process: LocalProcessTerminal | None,
    stdout_complete: bool,
    stderr_complete: bool,
    failure: Failure | None,
) -> ProcessResult:
    return ProcessResult(
        process.started if process is not None else False,
        process.local_status if process is not None else None,
        process.exit_status if process is not None else None,
        CapturedOutput(complete=stdout_complete, provenance=Provenance.CARRIER_STDOUT, retention=Retention.DELIVERED),
        CapturedOutput(complete=stderr_complete, provenance=Provenance.MIXED_STDERR, retention=Retention.DELIVERED),
        failure,
    )


def run_terminal_relay_candidate(
    argv: list[str],
    *,
    io: CarrierIO,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
) -> ProcessResult:
    """Exercise private relay custody while the carrier terminal gate remains shut.

    Geometry changes request a bounded notification from the same process owner;
    accepted local SIGWINCH does not prove remote resize. This candidate neither
    constructs a substitute process owner nor interprets
    preparation readiness. Pending cleanup retains endpoint use in custody. Native acquisition
    cleanup uncertainty becomes an observation fact and a safe control note;
    public control propagation suppresses raw native cause chains.
    """
    return _Attempt(argv, io, deadline, custody).run()
