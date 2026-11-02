"""The pump: one client process driven through pipes.

A single-threaded loop drives the process's non-blocking pipes, runs both
output streams through :class:`StreamFilter` and hands payload bytes to the
caller's sinks. It starts the process through ``ssh_process.spawn`` and
otherwise knows nothing about ssh, which is what lets the conformance suite
run it against a local shell.

A :class:`Deadline` bounds the local wait and never claims the guest command
stopped.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution import ssh_process
from agentworks.execution.envelope import StreamFilter
from agentworks.execution.results import UnknownReason

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import BinaryIO

CHUNK_BYTES = 65_536
CAPTURE_LIMIT_BYTES = 1_048_576

_POLL_SECONDS = 0.01
_NATURAL_EXIT_SECONDS = 1.0
_REAP_SECONDS = 0.5
_EXIT_DRAIN_SECONDS = 0.1
_STDIN_QUEUE_CHUNKS = 4


@dataclass(frozen=True)
class Deadline:
    """One local observation budget, not guest cancellation; None is unbounded."""

    expires_at: float | None

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        return cls(None if seconds is None else time.monotonic() + seconds)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


Sink = Callable[[bytes], None]
"""Receives payload bytes as they are released.

A sink that cannot take them yet raises ``BlockingIOError`` with
``characters_written`` set to what it did take; the pump holds the rest and
offers it again (an empty remainder asks the sink to finish, say a flush). An
error for which :func:`reader_left` holds means the consumer has gone; any
other ``OSError`` propagates.
"""


def reader_left(error: OSError) -> bool:
    """Whether a failed write or flush means the reader at the other end has gone (``| head``).

    That is ``BrokenPipeError`` or ``ConnectionResetError``, and on Windows
    ``EINVAL``, which is how CPython reports a pipe whose reader closed there.
    """
    if isinstance(error, (BrokenPipeError, ConnectionResetError)):
        return True
    return sys.platform == "win32" and error.errno == errno.EINVAL


class OutputLimitExceeded(Exception):
    """A capture sink received more than its limit."""


class Capture:
    """A sink that keeps up to ``limit`` bytes and refuses more."""

    def __init__(self, limit: int = CAPTURE_LIMIT_BYTES) -> None:
        self.data = bytearray()
        self._limit = limit

    def __call__(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self._limit:
            del self.data[self._limit :]
            raise OutputLimitExceeded


class StopReason(enum.Enum):
    """Why :meth:`PipeSession.pump` returned."""

    DONE = enum.auto()
    """Both streams reached their closing line."""
    CONDITION = enum.auto()
    """The caller's stop condition became true."""
    EXITED = enum.auto()
    """The client exited and its pipes stopped producing."""
    EOF = enum.auto()
    """Both pipes reached end of file."""
    DEADLINE = enum.auto()
    OUTPUT_LIMIT = enum.auto()
    STDERR_CONSUMER_LEFT = enum.auto()
    """The stderr consumer left, so the closing line that carries the status can no longer arrive."""

    @property
    def limit(self) -> UnknownReason | None:
        """The unknown outcome a local limit stands for; ``None`` for every other reason."""
        if self is StopReason.DEADLINE:
            return UnknownReason.DEADLINE
        if self is StopReason.OUTPUT_LIMIT:
            return UnknownReason.OUTPUT_LIMIT
        return None


class _InputFeeder:
    """Copies the operator's stdin into a queue from a daemon thread.

    A thread is needed because Windows cannot poll a console or pipe handle,
    and one mechanism serves every platform. It reads the raw stream because a
    daemon thread blocked in ``BufferedReader.read1`` aborts the interpreter at
    exit. The queue is bounded so a slow consumer does not buffer the stream.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_STDIN_QUEUE_CHUNKS)
        self.exhausted = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="agw-stdin").start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(CHUNK_BYTES)
                if chunk is None:
                    time.sleep(_POLL_SECONDS)
                    continue
                self._queue.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):
            self._queue.put(b"")

    def poll(self) -> bytes | None:
        """The next chunk, ``b""`` at end of input, or ``None`` when nothing is ready."""
        try:
            chunk = self._queue.get_nowait()
        except queue.Empty:
            return None
        if not chunk:
            self.exhausted = True
        return chunk


class PipeSession:
    """One client process driven through pipes, until the caller has what it needs.

    The session writes ``request`` and then the caller's ``stdin`` to the
    client, runs both output streams through :class:`StreamFilter` and passes
    payload bytes to the sinks. :meth:`pump` can be called more than once, which
    is how a terminal handover keeps its parking connection while the terminal
    half runs.

    The client's stdin closes once the request and the caller's input are sent,
    unless ``hold_stdin_open`` keeps it open for :meth:`close_stdin` (a parked
    launcher treats its closing as the client leaving).

    A write that fails to the client's stdin is the consumer ending early, not a
    failure: the session stops sending and keeps reading. A sink that raises
    an error for which :func:`reader_left` holds is the consumer of that
    stream leaving. The session closes its read end of that stream of the
    client's, so ssh sees ``EPIPE`` as it did when it held the operator's
    streams. A departed stdout makes ssh close the channel when the guest next
    writes, and the session keeps pumping stderr until then, so a quiet payload
    still reports its status. ssh does not end the channel for a departed
    stderr, and the status travels on stderr, so :meth:`pump` returns
    :attr:`StopReason.STDERR_CONSUMER_LEFT` once stdout has nothing held for a
    slow consumer and the trailer has not already arrived (with it in hand,
    stdout is pumped to its end and the status stands), and ending the session
    kills the client.
    :attr:`stdout_abandoned` and :attr:`stderr_abandoned` say which left. A sink
    that raises ``BlockingIOError`` is waited on: the session stops reading that
    stream until the sink has taken what it was offered, so output is never
    dropped. Any other ``OSError`` from a sink propagates.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        request: bytes,
        nonce: str,
        stdin: BinaryIO | None = None,
        hold_stdin_open: bool = False,
        on_stdout: Sink,
        on_stderr: Sink,
        deadline: Deadline | None = None,
    ) -> None:
        self._argv = list(argv)
        self._outbox = bytearray(request)
        self._feeder = None if stdin is None else _InputFeeder(stdin)
        self._sinks = {"out": on_stdout, "err": on_stderr}
        self._pending: dict[str, bytes | None] = {"out": None, "err": None}
        self._deadline = deadline or Deadline(None)
        self._hold_stdin_open = hold_stdin_open
        self._eof = {"out": False, "err": False}
        self._proc: subprocess.Popen[bytes] | None = None
        self._killed = False
        self._abandoned: set[str] = set()
        self.stdout = StreamFilter(nonce)
        self.stderr = StreamFilter(nonce)
        self.exit_status: int | None = None
        """The client's own exit status, when it exited by itself; ``None`` while it runs or if it was killed."""

    def __enter__(self) -> PipeSession:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._cleanup()

    def start(self) -> None:
        self._proc = ssh_process.spawn(
            self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
        if self._feeder is not None:
            self._feeder.start()

    def pump(self, stop: Callable[[], bool] | None = None, *, budget: Deadline | None = None) -> StopReason:
        """Drive the client until a stop reason applies.

        ``budget`` bounds this call on top of the session's own deadline. A
        deadline or an output limit kills the client before returning.
        """
        proc = self._proc
        assert proc is not None
        budget = budget or Deadline(None)
        last_activity = time.monotonic()
        while True:
            if self.exit_status is None and not self._killed:
                self.exit_status = proc.poll()
                if self.exit_status is not None:
                    last_activity = time.monotonic()
            try:
                progressed = self._read("out") | self._read("err")
            except OutputLimitExceeded:
                return self._stopped(StopReason.OUTPUT_LIMIT)
            progressed |= self._write()
            waiting = any(held is not None for held in self._pending.values())
            if self.stdout.done and self.stderr.done and not waiting:
                return StopReason.DONE
            if self.stdout_abandoned and self.stderr.done and not waiting:
                return StopReason.DONE
            if self.stderr_abandoned and not waiting and self.stderr.closing is None:
                return StopReason.STDERR_CONSUMER_LEFT
            if stop is not None and stop():
                return StopReason.CONDITION
            if self._deadline.expired or budget.expired:
                return self._stopped(StopReason.DEADLINE)
            if self._eof["out"] and self._eof["err"] and not waiting:
                return StopReason.EOF
            now = time.monotonic()
            if progressed:
                last_activity = now
                continue
            if self.exit_status is not None and not waiting and now - last_activity >= _EXIT_DRAIN_SECONDS:
                return StopReason.EXITED
            waits = [_POLL_SECONDS, self._deadline.remaining(), budget.remaining()]
            time.sleep(min(wait for wait in waits if wait is not None))

    def close_stdin(self) -> None:
        """Close the client's stdin now: the guest then sees end of input."""
        self._outbox.clear()
        self._finish_input()

    def end(self) -> None:
        """End the client: close its stdin, wait up to a second for it to exit, then kill it."""
        self.close_stdin()
        proc = self._proc
        if proc is not None and self.exit_status is None and not self._killed:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_NATURAL_EXIT_SECONDS)
            self.exit_status = proc.poll()
        self._cleanup()

    def _stopped(self, reason: StopReason) -> StopReason:
        """Kill the client at once and reap it briefly: a local limit has no natural end to wait for."""
        self._killed = True
        self._cleanup()
        return reason

    def _read(self, which: str) -> bool:
        if self._pending[which] is not None:
            return self._offer_again(which)
        if self._eof[which]:
            return False
        proc = self._proc
        assert proc is not None
        pipe = proc.stdout if which == "out" else proc.stderr
        assert pipe is not None
        stream_filter = self.stdout if which == "out" else self.stderr
        try:
            chunk = os.read(pipe.fileno(), CHUNK_BYTES)
        except BlockingIOError:
            return False
        except OSError:
            chunk = b""
        payload = stream_filter.feed(chunk) if chunk else stream_filter.finish()
        if not chunk:
            self._eof[which] = True
        if payload:
            self._deliver(which, payload)
        return True

    def _deliver(self, which: str, payload: bytes) -> None:
        try:
            self._sinks[which](payload)
        except BlockingIOError as error:
            self._pending[which] = payload[getattr(error, "characters_written", 0) :]
        except OSError as error:
            if not reader_left(error):
                raise
            self._abandon(which)

    def _abandon(self, which: str) -> None:
        """Stop reading ``which`` and close the client's read end of it, so ssh sees ``EPIPE``."""
        self._sinks[which] = _discard
        self._abandoned.add(which)
        self._eof[which] = True
        self._pending[which] = None
        proc = self._proc
        assert proc is not None
        pipe = proc.stdout if which == "out" else proc.stderr
        assert pipe is not None
        with contextlib.suppress(OSError):
            pipe.close()

    @property
    def stdout_abandoned(self) -> bool:
        """Whether the stdout consumer left, so the client's stdout was closed unread."""
        return "out" in self._abandoned

    @property
    def stderr_abandoned(self) -> bool:
        """Whether the stderr consumer left, so the client's stderr was closed unread."""
        return "err" in self._abandoned

    def _offer_again(self, which: str) -> bool:
        held = self._pending[which]
        assert held is not None
        self._pending[which] = None
        self._deliver(which, held)
        return self._pending[which] is None

    def _write(self) -> bool:
        proc = self._proc
        assert proc is not None
        stdin = proc.stdin
        if stdin is None or stdin.closed:
            return False
        progressed = False
        if not self._outbox and self._feeder is not None and not self._feeder.exhausted:
            chunk = self._feeder.poll()
            if chunk:
                self._outbox += chunk
                progressed = True
            elif chunk is not None:
                progressed = True
        if self._outbox:
            try:
                written = os.write(stdin.fileno(), bytes(self._outbox[:CHUNK_BYTES]))
            except BlockingIOError:
                return progressed
            except OSError:
                self._outbox.clear()
                self._finish_input()
                return True
            del self._outbox[:written]
            progressed |= written > 0
        if not self._hold_stdin_open and not self._outbox and (self._feeder is None or self._feeder.exhausted):
            self._finish_input()
            progressed = True
        return progressed

    def _finish_input(self) -> None:
        proc = self._proc
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        if self._feeder is not None:
            self._feeder.exhausted = True

    def _cleanup(self) -> None:
        """Close owned pipes and bound local kill and reap, even on interruption."""
        proc = self._proc
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=_REAP_SECONDS)


def _discard(_payload: bytes) -> None:
    return None
