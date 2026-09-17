"""Bounded, single-thread I/O for one owned client subprocess.

Python 3.12 supports nonblocking anonymous pipes on Windows as well as POSIX.
No thread or borrowed stream survives return. Execution uses the caller's
deadline; killing and reaping the local process gets at most 0.5 seconds more.
That allowance never resumes execution or establishes guest cancellation.
"""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    Failure,
    FiniteInput,
    Provenance,
    Retention,
)

if TYPE_CHECKING:
    from typing import IO

    from agentworks.execution.carrier import CarrierIO, Deadline

_CHUNK = 65_536
_POLL_SECONDS = 0.01
_CLEANUP_SECONDS = 0.5
_EXIT_DRAIN_SECONDS = 0.1


@dataclass
class _Output:
    provenance: Provenance
    retention: Retention
    limit: int
    data: bytearray = field(default_factory=bytearray, repr=False)
    eof: bool = False
    limited: bool = False

    def read(self, pipe: IO[bytes]) -> bool:
        """Read at most one chunk, so neither output can starve the other."""
        if self.eof:
            return False
        try:
            chunk = os.read(pipe.fileno(), _CHUNK)
        except BlockingIOError:
            return False
        if not chunk:
            self.eof = True
        elif self.retention == Retention.CAPTURED:
            available = self.limit - len(self.data)
            self.data.extend(chunk[:available])
            self.limited |= len(chunk) > available
        return bool(chunk)

    def report(self) -> CapturedOutput:
        return CapturedOutput(bytes(self.data), self.eof and not self.limited, self.provenance, self.retention)


@dataclass(frozen=True)
class _ProcessResult:
    started: bool
    local_status: int | None
    # Only a status observed before local termination can establish completion.
    exit_status: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    failure: Failure | None


def _cleanup(process: subprocess.Popen[bytes]) -> bool:
    """Close owned pipes and bound local kill/reap, even on interruption."""
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_CLEANUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def run_process(argv: list[str], *, io: CarrierIO, deadline: Deadline) -> _ProcessResult:
    """Send finite input once while fairly draining two independently capped streams."""
    retention = (
        Retention.SUPPRESSED
        if io.sensitive
        else Retention.CAPTURED
        if isinstance(io.output, Capture)
        else Retention.DISCARDED
    )
    limit = io.output.max_bytes if isinstance(io.output, Capture) else 0
    stdout = _Output(Provenance.CARRIER_STDOUT, retention, limit)
    stderr = _Output(Provenance.MIXED_STDERR, retention, limit)
    if deadline.expired:
        return _ProcessResult(False, None, None, stdout.report(), stderr.report(), Failure.DEADLINE)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if isinstance(io.input, FiniteInput) else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except (OSError, ValueError):
        return _ProcessResult(False, None, None, stdout.report(), stderr.report(), Failure.DISPATCH)

    failure: Failure | None = None
    exit_status: int | None = None
    drain_until: float | None = None
    interruption: BaseException | None = None
    try:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
        assert process.stdout is not None and process.stderr is not None
        data = memoryview(io.input.data if isinstance(io.input, FiniteInput) else b"")
        offset = 0
        while True:
            exit_status = process.poll()
            if deadline.expired:
                failure = Failure.DEADLINE
                break
            if exit_status is not None:
                if drain_until is None:
                    drain_until = time.monotonic() + _EXIT_DRAIN_SECONDS
                elif time.monotonic() >= drain_until:
                    # A continuously writing descendant must not extend even
                    # an explicitly unbounded invocation after its client exits.
                    failure = Failure.OUTPUT
                    break
            try:
                progressed = stdout.read(process.stdout)
                progressed = stderr.read(process.stderr) or progressed
            except OSError:
                failure = Failure.OUTPUT
                break
            if process.stdin is not None and not process.stdin.closed:
                try:
                    if offset < len(data):
                        written = os.write(process.stdin.fileno(), data[offset : offset + _CHUNK])
                        offset += written
                        progressed |= written > 0
                    if offset == len(data):
                        process.stdin.close()
                except BlockingIOError:
                    pass
                except OSError:
                    failure = Failure.INPUT
                    break
            if exit_status is not None:
                if offset < len(data):
                    failure = Failure.INPUT
                    break
                if stdout.eof and stderr.eof:
                    break
                if not progressed:
                    # A descendant may retain handles after ssh exits. Never
                    # infer EOF from exit or wait indefinitely for that process.
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
        if exit_status is None:
            exit_status = process.poll()
        cleaned = _cleanup(process)
        if not cleaned and interruption is not None:
            interruption.add_note("Local SSH client cleanup did not complete within its bound.")
    if not cleaned:
        failure = Failure.OBSERVATION
    elif failure is None and (stdout.limited or stderr.limited):
        failure = Failure.OUTPUT_LIMIT
    return _ProcessResult(True, process.returncode, exit_status, stdout.report(), stderr.report(), failure)
