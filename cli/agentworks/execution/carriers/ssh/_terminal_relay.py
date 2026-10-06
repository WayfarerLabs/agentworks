"""Private POSIX terminal relay using the shared held-process owner.

The SSH carrier does not enable this path yet. Its composition must supply an
owner-mediated resize notification before terminal delivery can be advertised.
Readiness interpretation and emulator sanitation remain preparation policy.
"""

from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass, field
from threading import Condition, Event, Thread
from typing import TYPE_CHECKING

from agentworks.execution._process import (
    BorrowedProcessStdin,
    LocalProcessOwner,
    LocalProcessRequest,
    LocalProcessTerminal,
    SinkWriteError,
    try_write_to_sink,
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

    from agentworks.execution.carrier import ByteSink

_CHUNK = 65_536
_POLL_SECONDS = 0.01
_EXIT_DRAIN_SECONDS = 0.1


@dataclass
class _Stream:
    sink: ByteSink = field(repr=False)
    pending: memoryview | None = field(default=None, repr=False)
    eof: bool = False

    def advance(self, pipe: IO[bytes]) -> bool:
        """Deliver pending bytes before collecting another bounded pipe chunk."""
        if self.eof:
            return False
        if self.pending is None:
            try:
                chunk = os.read(pipe.fileno(), _CHUNK)
            except BlockingIOError:
                return False
            if not chunk:
                self.eof = True
                return True
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
    """Retain the native worker before admitting any terminal or process effects."""

    def __init__(
        self,
        argv: list[str],
        io: CarrierIO,
        deadline: Deadline,
    ) -> None:
        assert isinstance(io.input, TerminalInput) and isinstance(io.output, SinkOutput)
        self._argv = tuple(argv)
        self._spec = io.input
        self._output = io.output
        self._deadline = deadline
        self._condition = Condition()
        self._admitted = False
        self._cancelled = False
        self._stop = Event()
        self._done = Event()
        self._result: ProcessResult | None = None
        self._interruption: BaseException | None = None
        self._worker = Thread(target=self._entry, name="ssh-terminal")

    def run(self) -> ProcessResult:
        """Keep all admitted effects owned through interrupted caller waits."""
        try:
            self._worker.start()
            with self._condition:
                self._admitted = True
                self._condition.notify_all()
            self._done.wait()
        except BaseException as error:
            self._record_control(error)
        finally:
            # A failed or interrupted start can leave an inert native tail. Only
            # admitted work may touch endpoints and requires completion settlement.
            while True:
                try:
                    with self._condition:
                        self._cancelled = True
                        admitted = self._admitted
                        self._condition.notify_all()
                    self._stop.set()
                    break
                except BaseException as error:
                    self._record_control(error)
            if admitted:
                while True:
                    try:
                        if self._done.wait(_POLL_SECONDS):
                            break
                    except BaseException as error:
                        self._record_control(error)
        interruption = self._record_control(None)
        if interruption is not None:
            if self._result is not None and self._result.failure is Failure.OBSERVATION:
                interruption.add_note("Terminal local cleanup or observation was uncertain.")
            raise interruption from None
        assert self._result is not None
        return self._result

    def _record_control(self, error: BaseException | None) -> BaseException | None:
        """Select the first observed control exception across caller and worker."""
        while True:
            try:
                with self._condition:
                    self._interruption = _retain_control(self._interruption, error)
                    return self._interruption
            except BaseException as later:
                error = _retain_control(error, later)

    def _entry(self) -> None:
        """Publish completion only after every descriptor borrower has stopped."""
        try:
            with self._condition:
                while not self._admitted and not self._cancelled:
                    self._condition.wait()
                if not self._admitted:
                    return
            self._work()
        except BaseException as error:
            # Ordinary errors are categories, never raw source or sink diagnostics.
            if not isinstance(error, Exception):
                self._record_control(error)
            self._result = _result(None, False, False, Failure.OBSERVATION)
        finally:
            self._done.set()

    def _work(self) -> None:
        terminal: PosixTerminal | None = None
        owner = LocalProcessOwner()
        process: LocalProcessTerminal | None = None
        stdout = _Stream(self._output.stdout)
        stderr = _Stream(self._output.stderr)
        failure: Failure | None = None
        try:
            if self._deadline.expired or self._stop.is_set():
                failure = Failure.DEADLINE
            else:
                terminal = PosixTerminal.acquire(self._spec.input_fd, self._spec.output_fd)
                if self._deadline.expired or self._stop.is_set():
                    failure = Failure.DEADLINE
                else:
                    # TERM describes the supplied endpoint rather than an ambient
                    # workstation terminal. No other child policy changes here.
                    env = dict(os.environ)
                    env["TERM"] = self._spec.term
                    request = LocalProcessRequest(
                        self._argv,
                        BorrowedProcessStdin(terminal.slave_fd),
                        tuple(env.items()),
                        start_new_session=True,
                    )
                    if self._deadline.expired or self._stop.is_set():
                        failure = Failure.DEADLINE
                    else:
                        owner.start(request)
                        failure = self._pump(owner, terminal, stdout, stderr)
                    del request, env
        except Exception as error:
            failure = (
                Failure.OBSERVATION
                if terminal is not None or isinstance(error.__cause__, AcquisitionCleanupFailure)
                else Failure.DISPATCH
            )
        except BaseException as error:
            if isinstance(error.__cause__, AcquisitionCleanupFailure):
                failure = Failure.OBSERVATION
            self._record_control(error)
        finally:
            # The native worker alone pumps descriptors. It has stopped before
            # close relinquishes the pipes and settles construction/client custody.
            try:
                process = owner.close()
            except BaseException as error:
                if isinstance(error, Exception):
                    failure = Failure.OBSERVATION
                else:
                    self._record_control(error)
                process = owner.snapshot().terminal
            assert process is not None
            if not process.cleaned or process.observation_failed:
                failure = Failure.OBSERVATION
            elif process.dispatch_failed:
                failure = Failure.DISPATCH
            if terminal is not None:
                release_errors = terminal.release()
                if release_errors:
                    failure = Failure.OBSERVATION
                    for release_error in release_errors:
                        if not isinstance(release_error, Exception):
                            self._record_control(release_error)
            stdout.pending = stderr.pending = None
            self._result = _result(process, stdout.eof, stderr.eof, failure)

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
        drain_remaining = _EXIT_DRAIN_SECONDS
        drain_at: float | None = None
        drain_paused = False
        try:
            while True:
                snapshot = owner.snapshot()
                if snapshot.observation_failed:
                    return Failure.OBSERVATION
                if self._deadline.expired or self._stop.is_set():
                    return Failure.DEADLINE
                pending = stdout.pending is not None or stderr.pending is not None
                if snapshot.exit_status is not None:
                    now = time.monotonic()
                    if drain_at is not None and not drain_paused:
                        drain_remaining -= now - drain_at
                    drain_at, drain_paused = now, pending
                    if drain_remaining <= 0:
                        return Failure.OUTPUT
                progressed = False
                outputs = (
                    ((stdout, pipes.stdout), (stderr, pipes.stderr))
                    if output_first
                    else ((stderr, pipes.stderr), (stdout, pipes.stdout))
                )
                output_first = not output_first
                for stream, pipe in outputs:
                    if snapshot.exit_status is not None and stream.pending is None and pending:
                        continue
                    try:
                        progressed = stream.advance(pipe) or progressed
                    except (OSError, SinkWriteError):
                        return Failure.OUTPUT
                if snapshot.exit_status is not None:
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
                            # Changing geometry without exact-client notification
                            # cannot establish remote resize. Refuse this candidate
                            # until the shared owner supplies the real operation.
                            return Failure.OBSERVATION
                    except Exception:
                        return Failure.OBSERVATION
                if drain_at is not None:
                    drain_paused = stdout.pending is not None or stderr.pending is not None
                if not progressed:
                    self._pause()
        finally:
            input_state.pending = None

    def _pause(self) -> None:
        remaining = self._deadline.remaining()
        self._stop.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))


def _retain_control(first: BaseException | None, later: BaseException | None) -> BaseException | None:
    if first is None or (isinstance(first, Exception) and later is not None and not isinstance(later, Exception)):
        return later
    return first


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
) -> ProcessResult:
    """Exercise private relay custody while the carrier terminal gate remains shut.

    Initial geometry is copied. A detected geometry change refuses the attempt
    because the shared process owner cannot yet notify its exact client. This
    candidate neither constructs a substitute process owner nor interprets
    preparation readiness. Endpoint use ends before return. Native acquisition
    cleanup uncertainty becomes an observation fact and a safe control note;
    public control propagation suppresses raw native cause chains.
    """
    return _Attempt(argv, io, deadline).run()
