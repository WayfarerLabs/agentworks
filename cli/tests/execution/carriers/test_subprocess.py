"""Behavioral coverage for the shared owned-process pump."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from subprocess import Popen
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    Deadline,
    Discard,
    Failure,
    FiniteInput,
    LiveInput,
    Provenance,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers import _subprocess
from agentworks.execution.carriers._subprocess import ProcessResult, run_process

pytestmark = pytest.mark.windows


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(argv, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def execute(
    script: str,
    *,
    io: CarrierIO | None = None,
    seconds: float | None = 10,
    env: Mapping[str, str] | None = None,
    live_stdio: bool = False,
) -> ProcessResult:
    return run_process(
        [sys.executable, "-c", script],
        io=io or CarrierIO(),
        deadline=Deadline.after(seconds),
        env=env,
        live_stdio=live_stdio,
    )


def fresh_process_result(script: str) -> object:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def assert_closed(children: list[subprocess.Popen[bytes]]) -> None:
    assert children
    for child in children:
        assert child.poll() is not None
        for pipe in (child.stdin, child.stdout, child.stderr):
            assert pipe is None or pipe.closed


class ChunkSource:
    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = chunks
        self.limits: list[int] = []
        self.closed = False

    def try_read(self, limit: int) -> bytes | None:
        self.limits.append(limit)
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


class ShortSink:
    def __init__(self, limit: int, *, stall_every: int | None = None) -> None:
        self.limit = limit
        self.stall_every = stall_every
        self.calls = 0
        self.offers: list[int] = []
        self.data = bytearray()
        self.closed = False

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        self.offers.append(len(data))
        if self.stall_every is not None and self.calls % self.stall_every == 0:
            return None
        written = min(self.limit, len(data))
        self.data.extend(data[:written])
        return written

    def close(self) -> None:
        self.closed = True


def test_binary_streams_remain_separate_and_unattributed(children: list[subprocess.Popen[bytes]]) -> None:
    result = execute(
        "import sys; sys.stdout.buffer.write(b'\\x00\\xff\\r\\n'); "
        "sys.stderr.buffer.write(b'\\x80err\\n'); sys.exit(23)"
    )
    assert result.started
    assert result.local_status == result.exit_status == 23
    assert result.stdout.data == b"\x00\xff\r\n"
    assert result.stderr.data == b"\x80err\n"
    assert result.stdout.complete and result.stderr.complete
    assert result.stdout.provenance == result.stderr.provenance == Provenance.UNKNOWN
    assert result.failure is None
    assert_closed(children)


@pytest.mark.parametrize("code", [0, 42, 255])
def test_exact_wait_preserves_every_representative_exit(children: list[subprocess.Popen[bytes]], code: int) -> None:
    result = execute(f"import sys; sys.exit({code})")

    assert result.local_status == result.exit_status == code
    assert result.failure is None
    assert_closed(children)


@pytest.mark.skipif(os.name == "nt", reason="SIGCHLD is a POSIX process-global setting")
def test_ignored_sigchld_in_fresh_process_never_becomes_exit_zero() -> None:
    result = fresh_process_result(
        """
import json, signal, sys
from agentworks.execution.carrier import CarrierIO, Deadline
from agentworks.execution.carriers._subprocess import run_process
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
result = run_process(
    [sys.executable, '-c', 'import sys; sys.exit(42)'],
    io=CarrierIO(), deadline=Deadline.after(3),
)
print(json.dumps([result.local_status, result.exit_status, result.failure]))
"""
    )

    assert result == [None, None, "observation"]


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_competing_reaper_in_fresh_process_loses_status_without_guessing() -> None:
    result = fresh_process_result(
        """
import json, os, subprocess, sys, threading, time
from agentworks.execution.carrier import CarrierIO, Deadline
from agentworks.execution.carriers._subprocess import run_process
original_popen = subprocess.Popen
reaped = []
reapers = []
def spawn(*args, **kwargs):
    process = original_popen(*args, **kwargs)
    entered = threading.Event()
    def reap():
        entered.set()
        reaped.append(os.waitpid(process.pid, 0)[1])
    thread = threading.Thread(target=reap)
    thread.start()
    entered.wait()
    time.sleep(.02)
    reapers.append(thread)
    return process
subprocess.Popen = spawn
result = run_process(
    [sys.executable, '-c', 'import sys,time; time.sleep(.1); sys.exit(42)'],
    io=CarrierIO(), deadline=Deadline.after(3),
)
subprocess.Popen = original_popen
reapers[0].join(2)
print(json.dumps([result.local_status, result.exit_status, result.failure,
                  os.waitstatus_to_exitcode(reaped[0])]))
"""
    )

    assert result == [None, None, "observation", 42]


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_known_wait_loss_never_uses_reused_numeric_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.waitpid(process.pid, 0)
    status = _subprocess._ProcessStatus(process)
    assert status.poll() is None and status.lost

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("known-lost process identity was reused")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "waitpid", forbidden)
    monkeypatch.setattr(Popen, "poll", forbidden)
    monkeypatch.setattr(Popen, "wait", forbidden)
    monkeypatch.setattr(Popen, "kill", forbidden)
    assert not _subprocess._cleanup(status)
    assert status.status is None
    assert process.returncode == 0  # Internal destructor bookkeeping only.
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_unexpected_wait_error_still_kills_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status = _subprocess._ProcessStatus(process)
    original_waitpid = os.waitpid
    failed = False

    def fail_once(pid: int, options: int) -> tuple[int, int]:
        nonlocal failed
        if pid == process.pid and not failed:
            failed = True
            raise OSError("secret-wait-canary")
        return original_waitpid(pid, options)

    monkeypatch.setattr(os, "waitpid", fail_once)
    with pytest.raises(OSError, match="secret-wait-canary"):
        status.poll()
    assert _subprocess._cleanup(status)
    assert status.status == -9
    assert not status.lost
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_interrupted_exact_wait_retries_same_owned_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(42)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status = _subprocess._ProcessStatus(process)
    original_waitpid = os.waitpid
    interrupted = False

    def interrupt_once(pid: int, options: int) -> tuple[int, int]:
        nonlocal interrupted
        if pid == process.pid and not interrupted:
            interrupted = True
            raise InterruptedError
        return original_waitpid(pid, options)

    monkeypatch.setattr(os, "waitpid", interrupt_once)
    until = time.monotonic() + 2
    while status.poll() is None and time.monotonic() < until:
        time.sleep(0.01)
    assert interrupted
    assert status.status == 42
    assert _subprocess._cleanup(status)


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_repeated_wait_interruptions_respect_operation_and_cleanup_bounds(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_waitpid = os.waitpid

    def always_interrupted(pid: int, options: int) -> tuple[int, int]:
        if children and pid == children[-1].pid:
            raise InterruptedError
        return original_waitpid(pid, options)

    with monkeypatch.context() as context:
        context.setattr(os, "waitpid", always_interrupted)
        context.setattr(_subprocess, "_CLEANUP_SECONDS", 0.05)
        started = time.monotonic()
        result = execute("import time; time.sleep(30)", seconds=0.05)

    assert time.monotonic() - started < 0.5
    assert result.local_status is None and result.exit_status is None
    assert result.failure == Failure.OBSERVATION


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_stopped_wait_status_remains_pending_until_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status = _subprocess._ProcessStatus(process)
    original_waitpid = os.waitpid
    stopped = (signal.SIGSTOP << 8) | 0x7F
    reported_stop = False
    assert os.WIFSTOPPED(stopped)

    def stop_once(pid: int, options: int) -> tuple[int, int]:
        nonlocal reported_stop
        if pid == process.pid and not reported_stop:
            reported_stop = True
            return pid, stopped
        return original_waitpid(pid, options)

    monkeypatch.setattr(os, "waitpid", stop_once)
    assert status.poll() is None
    assert reported_stop and status.status is None and not status.lost
    assert _subprocess._cleanup(status)
    assert status.status == -signal.SIGKILL


@pytest.mark.skipif(os.name != "nt", reason="Windows retains handle-backed Popen waiting")
def test_windows_waiting_never_calls_posix_waitpid(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "waitpid", lambda *args: pytest.fail("Windows called waitpid"), raising=False)

    result = execute("import sys; sys.exit(42)")

    assert result.local_status == result.exit_status == 42
    assert result.failure is None
    assert_closed(children)


def test_duplex_pressure_delivers_finite_input_once_then_eof(children: list[subprocess.Popen[bytes]]) -> None:
    data = bytes(range(256)) * 8192
    result = execute(
        "import hashlib,sys; "
        "sys.stdout.buffer.write(b'o'*200000); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'e'*200000); sys.stderr.buffer.flush(); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(hashlib.sha256(data).hexdigest().encode())",
        io=CarrierIO(input=FiniteInput(data)),
    )
    assert result.stdout.data == b"o" * 200_000 + hashlib.sha256(data).hexdigest().encode()
    assert result.stderr.data == b"e" * 200_000
    assert result.exit_status == 0
    assert result.failure is None
    assert result.stdout.complete and result.stderr.complete
    assert_closed(children)


def test_live_duplex_handles_stalls_short_writes_and_binary_bytes(
    children: list[subprocess.Popen[bytes]],
) -> None:
    data = bytes(range(256)) * 512
    chunks: list[bytes | None] = [None]
    chunks.extend(data[offset : offset + 8192] for offset in range(0, len(data), 8192))
    chunks.append(b"")
    source = ChunkSource(chunks)
    stdout = ShortSink(4096, stall_every=4)
    stderr = ShortSink(3072, stall_every=3)
    result = execute(
        "import hashlib,sys; "
        "sys.stdout.buffer.write(bytes(range(256))*256); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(bytes(reversed(range(256)))*256); sys.stderr.buffer.flush(); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(hashlib.sha256(data).digest())",
        io=CarrierIO(input=LiveInput(source), output=SinkOutput(stdout, stderr, require_live=True)),
        live_stdio=True,
    )
    assert bytes(stdout.data) == bytes(range(256)) * 256 + hashlib.sha256(data).digest()
    assert bytes(stderr.data) == bytes(reversed(range(256))) * 256
    assert result.stdout.data == result.stderr.data == b""
    assert result.stdout.retention == result.stderr.retention == Retention.DELIVERED
    assert result.stdout.complete and result.stderr.complete
    assert result.exit_status == 0
    assert result.failure is None
    assert source.limits and set(source.limits) == {_subprocess._CHUNK}
    assert max(stdout.offers + stderr.offers) <= _subprocess._CHUNK
    assert not source.closed and not stdout.closed and not stderr.closed
    assert_closed(children)


def test_live_source_eof_closes_only_owned_stdin(children: list[subprocess.Popen[bytes]]) -> None:
    source = ChunkSource([b""])
    result = execute(
        "import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'eof')",
        io=CarrierIO(input=LiveInput(source)),
        live_stdio=True,
    )
    assert result.stdout.data == b"eof"
    assert result.failure is None
    assert not source.closed
    assert_closed(children)


def test_sensitive_sink_delivery_is_transient_not_retained(children: list[subprocess.Popen[bytes]]) -> None:
    canary = b"sensitive-live-reflection-canary"
    source = ChunkSource([canary, b""])
    stdout = ShortSink(7)
    stderr = ShortSink(5)
    result = execute(
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data); sys.exit(19)",
        io=CarrierIO(
            input=LiveInput(source, sensitive=True),
            output=SinkOutput(stdout, stderr, require_live=True),
        ),
        live_stdio=True,
    )
    assert bytes(stdout.data) == bytes(stderr.data) == canary
    assert result.exit_status == 19
    assert result.failure is None
    assert result.stdout.retention == result.stderr.retention == Retention.DELIVERED
    assert result.stdout.data == result.stderr.data == b""
    assert canary.decode() not in repr(result)
    assert_closed(children)


@pytest.mark.parametrize(
    "response",
    [b"x" * (_subprocess._CHUNK + 1), "not-bytes", 0, True, ValueError("secret-source-canary")],
)
def test_invalid_live_source_response_is_input_failure(
    children: list[subprocess.Popen[bytes]], response: object
) -> None:
    class InvalidSource:
        def try_read(self, limit: int) -> bytes | None:
            if isinstance(response, Exception):
                raise response
            return response  # type: ignore[return-value]

    result = execute(
        "import time; time.sleep(30)",
        io=CarrierIO(input=LiveInput(InvalidSource())),
        live_stdio=True,
    )
    assert result.failure == Failure.INPUT
    assert result.exit_status is None
    assert "secret-source-canary" not in repr(result)
    assert_closed(children)


@pytest.mark.parametrize("response", [0, -1, True, "one", 1_000_000, ValueError("secret-sink-canary")])
def test_invalid_sink_response_is_output_failure(children: list[subprocess.Popen[bytes]], response: object) -> None:
    class InvalidSink:
        def try_write(self, data: memoryview) -> int | None:
            if isinstance(response, Exception):
                raise response
            return response  # type: ignore[return-value]

    result = execute(
        "import sys,time; sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush(); time.sleep(30)",
        io=CarrierIO(output=SinkOutput(InvalidSink(), ShortSink(64))),
    )
    assert result.failure == Failure.OUTPUT
    assert result.exit_status is None
    assert result.stdout.retention == result.stderr.retention == Retention.DELIVERED
    assert not result.stdout.complete
    assert "secret-sink-canary" not in repr(result)
    assert_closed(children)


@pytest.mark.parametrize(
    "io",
    [
        CarrierIO(input=LiveInput(ChunkSource([b""]))),
        CarrierIO(output=SinkOutput(ShortSink(64), ShortSink(64), require_live=True)),
    ],
)
def test_live_feature_mismatch_refuses_before_process_creation(monkeypatch: pytest.MonkeyPatch, io: CarrierIO) -> None:
    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError("unsupported live I/O attempted to spawn")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ValidationError):
        execute("raise AssertionError", io=io)


def test_buffered_sink_delivery_does_not_require_live_feature(children: list[subprocess.Popen[bytes]]) -> None:
    stdout = ShortSink(2)
    stderr = ShortSink(3)
    result = execute(
        "import sys; sys.stdout.buffer.write(b'out'); sys.stderr.buffer.write(b'error')",
        io=CarrierIO(output=SinkOutput(stdout, stderr)),
    )
    assert bytes(stdout.data) == b"out"
    assert bytes(stderr.data) == b"error"
    assert result.stdout.retention == result.stderr.retention == Retention.DELIVERED
    assert result.stdout.complete and result.stderr.complete
    assert result.failure is None
    assert_closed(children)


@pytest.mark.parametrize("finite", [False, True])
def test_empty_input_observes_eof(children: list[subprocess.Popen[bytes]], finite: bool) -> None:
    io = CarrierIO(input=FiniteInput(b"")) if finite else CarrierIO()
    result = execute(
        "import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'eof')",
        io=io,
    )
    assert result.stdout.data == b"eof"
    assert result.failure is None
    assert (children[-1].stdin is not None) is finite
    assert_closed(children)


@pytest.mark.parametrize("count,limit", [(0, 0), (8, 8), (9, 8), (100_000, 0)])
def test_capture_limits_apply_independently(children: list[subprocess.Popen[bytes]], count: int, limit: int) -> None:
    result = execute(
        f"import sys; sys.stdout.buffer.write(b'o'*{count}); sys.stderr.buffer.write(b'e'*{count})",
        io=CarrierIO(output=Capture(limit)),
    )
    assert result.stdout.data == b"o" * min(count, limit)
    assert result.stderr.data == b"e" * min(count, limit)
    assert result.stdout.complete is (count <= limit)
    assert result.stderr.complete is (count <= limit)
    assert result.failure == (Failure.OUTPUT_LIMIT if count > limit else None)
    assert result.exit_status == 0
    assert_closed(children)


@pytest.mark.parametrize(
    "io,retention",
    [
        (CarrierIO(input=FiniteInput(b"secret-canary", sensitive=True)), Retention.SUPPRESSED),
        (CarrierIO(input=FiniteInput(b"secret-canary"), sensitive=True), Retention.SUPPRESSED),
        (CarrierIO(input=FiniteInput(b"secret-canary"), output=Discard()), Retention.DISCARDED),
    ],
)
def test_unretained_output_is_drained_without_capture(
    children: list[subprocess.Popen[bytes]], io: CarrierIO, retention: Retention
) -> None:
    result = execute(
        "import sys; data=sys.stdin.buffer.read()*10000; sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)",
        io=io,
    )
    assert result.stdout.data == result.stderr.data == b""
    assert result.stdout.retention == result.stderr.retention == retention
    assert result.stdout.complete and result.stderr.complete
    assert result.failure is None
    assert result.exit_status == 0
    assert "secret-canary" not in repr(result)
    assert_closed(children)


def test_expired_deadline_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError("expired execution attempted to spawn")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = execute("raise AssertionError", seconds=0)
    assert result == ProcessResult(
        started=False,
        local_status=None,
        exit_status=None,
        stdout=result.stdout,
        stderr=result.stderr,
        failure=Failure.DEADLINE,
    )
    assert not result.stdout.complete and not result.stderr.complete


def test_deadline_preserves_partial_evidence_and_reaps(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    markers = {b"partial", b"diagnostic"}
    observed: set[bytes] = set()
    original_advance = _subprocess._Output.advance
    spawn = subprocess.Popen

    def advance(output: _subprocess._Output, pipe: Any) -> tuple[bool, bool]:
        progressed = original_advance(output, pipe)
        observed.update(marker for marker in markers if marker in output.data)
        return progressed

    def delayed_startup(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        time.sleep(0.3)
        return spawn(argv, **kwargs)

    deadline = Deadline.after(10)
    monkeypatch.setattr(_subprocess._Output, "advance", advance)
    monkeypatch.setattr(subprocess, "Popen", delayed_startup)
    monkeypatch.setattr(Deadline, "expired", property(lambda value: observed == markers or value.remaining() == 0))
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); "
            "sys.stderr.write('diagnostic'); sys.stderr.flush(); time.sleep(30)",
        ],
        io=CarrierIO(),
        deadline=deadline,
    )
    assert observed == markers
    assert result.failure == Failure.DEADLINE
    assert result.stdout.data == b"partial"
    assert result.stderr.data == b"diagnostic"
    assert not result.stdout.complete and not result.stderr.complete
    assert result.local_status is not None
    assert result.exit_status is None
    assert_closed(children)


def test_deadline_budget_includes_process_startup(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup_offset = 0.0
    monotonic = time.monotonic
    spawn = subprocess.Popen

    def complete_startup(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        nonlocal startup_offset
        child = spawn(argv, **kwargs)
        startup_offset = 2.0
        return child

    def reject_output_read(output: _subprocess._Output, pipe: Any) -> tuple[bool, bool]:
        pytest.fail("Expired startup budget allowed an output observation cycle")

    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + startup_offset)
    deadline = Deadline.after(1)
    monkeypatch.setattr(subprocess, "Popen", complete_startup)
    monkeypatch.setattr(_subprocess._Output, "advance", reject_output_read)
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        io=CarrierIO(),
        deadline=deadline,
    )
    assert result.started
    assert result.failure == Failure.DEADLINE
    assert result.stdout.data == result.stderr.data == b""
    assert result.local_status is not None
    assert result.exit_status is None
    assert_closed(children)


def test_natural_exit_is_observed_despite_unsent_input(children: list[subprocess.Popen[bytes]], tmp_path: Path) -> None:
    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "import pathlib,time; "
        f"release=pathlib.Path({str(release)!r}); done=pathlib.Path({str(done)!r}); "
        "end=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<end: time.sleep(.01)\n"
        "done.touch()"
    )
    script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}], "
        "stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "sys.stdout.write('parent'); sys.exit(23)"
    )
    try:
        result = execute(script, io=CarrierIO(input=FiniteInput(b"x" * 2_000_000)), seconds=None)
        assert result.failure == Failure.INPUT
        assert result.local_status == result.exit_status == 23
        assert result.stdout.data == b"parent"
        assert_closed(children)
    finally:
        release.touch()
        until = time.monotonic() + 5
        while not done.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert done.exists()


def test_live_input_early_close_preserves_observed_exit(children: list[subprocess.Popen[bytes]]) -> None:
    class EndlessSource:
        def __init__(self) -> None:
            self.calls = 0

        def try_read(self, limit: int) -> bytes | None:
            self.calls += 1
            return b"x" * limit

    source = EndlessSource()
    result = execute(
        "import sys; sys.stdout.buffer.write(b'parent'); sys.exit(23)",
        io=CarrierIO(input=LiveInput(source)),
        seconds=None,
        live_stdio=True,
    )
    assert source.calls > 0
    assert result.failure == Failure.INPUT
    assert result.local_status == result.exit_status == 23
    assert result.stdout.data == b"parent"
    assert_closed(children)


def test_stalled_source_keeps_draining_until_deadline_and_stops_after_return(
    children: list[subprocess.Popen[bytes]],
) -> None:
    class StalledSource:
        def __init__(self) -> None:
            self.calls = 0

        def try_read(self, limit: int) -> bytes | None:
            self.calls += 1
            return None

    source = StalledSource()
    stdout = ShortSink(64)
    stderr = ShortSink(64)
    result = execute(
        "import sys,time; sys.stdout.buffer.write(b'ready'); sys.stdout.buffer.flush(); time.sleep(30)",
        io=CarrierIO(
            input=LiveInput(source),
            output=SinkOutput(stdout, stderr, require_live=True),
        ),
        seconds=0.1,
        live_stdio=True,
    )
    calls_after_return = source.calls
    time.sleep(0.05)
    assert source.calls == calls_after_return
    assert source.calls > 1
    assert bytes(stdout.data) == b"ready"
    assert result.failure == Failure.DEADLINE
    assert result.exit_status is None
    assert not result.stdout.complete and not result.stderr.complete
    assert_closed(children)


def test_sink_failure_preserves_independently_observed_exit(children: list[subprocess.Popen[bytes]]) -> None:
    class FailAfterExit:
        def try_write(self, data: memoryview) -> int | None:
            if children[-1].poll() is None:
                return None
            raise RuntimeError("secret-sink-canary")

    result = execute(
        "import sys; sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush(); sys.exit(23)",
        io=CarrierIO(output=SinkOutput(FailAfterExit(), ShortSink(64))),
        seconds=None,
    )
    assert result.local_status == result.exit_status == 23
    assert result.failure == Failure.OUTPUT
    assert not result.stdout.complete
    assert result.stdout.data == b""
    assert "secret-sink-canary" not in repr(result)
    assert_closed(children)


def test_post_exit_budget_pauses_for_healthy_sink_backpressure(
    children: list[subprocess.Popen[bytes]],
) -> None:
    class DelayedSink:
        def __init__(self) -> None:
            self.blocked_until: float | None = None
            self.data = bytearray()

        def try_write(self, data: memoryview) -> int | None:
            if self.blocked_until is None:
                self.blocked_until = time.monotonic() + 0.2
            if time.monotonic() < self.blocked_until:
                return None
            self.data.extend(data)
            return len(data)

    stdout = DelayedSink()
    started = time.monotonic()
    result = execute(
        "import sys; sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush()",
        io=CarrierIO(output=SinkOutput(stdout, ShortSink(64))),
        seconds=3,
    )
    assert time.monotonic() - started >= 0.18
    assert bytes(stdout.data) == b"payload"
    assert result.local_status == result.exit_status == 0
    assert result.failure is None
    assert result.stdout.complete and result.stderr.complete
    assert_closed(children)


def test_permanently_stalled_post_exit_sink_uses_operation_deadline(
    children: list[subprocess.Popen[bytes]],
) -> None:
    class StalledSink:
        def try_write(self, data: memoryview) -> int | None:
            return None

    started = time.monotonic()
    result = execute(
        "import sys; sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush()",
        io=CarrierIO(output=SinkOutput(StalledSink(), ShortSink(64))),
        seconds=0.25,
    )
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 1
    assert result.local_status == result.exit_status == 0
    assert result.failure == Failure.DEADLINE
    assert not result.stdout.complete
    assert_closed(children)


def test_post_exit_short_writes_are_delivered_fairly(children: list[subprocess.Popen[bytes]]) -> None:
    class DelayedShortSink:
        def __init__(self) -> None:
            self.ready_at = 0.0
            self.data = bytearray()

        def try_write(self, data: memoryview) -> int | None:
            now = time.monotonic()
            if now < self.ready_at:
                return None
            written = min(2, len(data))
            self.data.extend(data[:written])
            self.ready_at = now + 0.02
            return written

    stdout = DelayedShortSink()
    payload = b"short-write-payload" * 3
    result = execute(
        f"import sys; sys.stdout.buffer.write({payload!r}); sys.stdout.buffer.flush()",
        io=CarrierIO(output=SinkOutput(stdout, ShortSink(64))),
        seconds=3,
    )
    assert bytes(stdout.data) == payload
    assert result.local_status == result.exit_status == 0
    assert result.failure is None
    assert result.stdout.complete and result.stderr.complete
    assert_closed(children)


@pytest.mark.parametrize(
    ("stall_seconds", "deadline_seconds", "expected_failure"),
    [(0.3, 2.0, Failure.OUTPUT), (None, 0.5, Failure.DEADLINE)],
)
def test_post_exit_pending_delivery_stops_fresh_reads_from_other_stream(
    children: list[subprocess.Popen[bytes]],
    tmp_path: Path,
    stall_seconds: float | None,
    deadline_seconds: float,
    expected_failure: Failure,
) -> None:
    class CountingSink:
        def __init__(self) -> None:
            self.bytes_written = 0

        def try_write(self, data: memoryview) -> int | None:
            self.bytes_written += len(data)
            return len(data)

    stderr = CountingSink()

    class StalledSink:
        def __init__(self) -> None:
            self.started_at: float | None = None
            self.data = bytearray()
            self.stderr_after_exit: list[int] = []

        def try_write(self, data: memoryview) -> int | None:
            now = time.monotonic()
            if self.started_at is None:
                self.started_at = now
            if children[-1].poll() is not None:
                self.stderr_after_exit.append(stderr.bytes_written)
            if stall_seconds is None or now - self.started_at < stall_seconds:
                return None
            self.data.extend(data)
            return len(data)

    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "import os,pathlib,time; "
        f"release=pathlib.Path({str(release)!r}); done=pathlib.Path({str(done)!r}); "
        "end=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<end:\n"
        " try: os.write(2,b'e'*65536)\n"
        " except OSError: break\n"
        "done.touch()"
    )
    script = (
        "import subprocess,sys; "
        "sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush(); "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)"
    )
    stdout = StalledSink()
    try:
        result = execute(
            script,
            io=CarrierIO(output=SinkOutput(stdout, stderr)),
            seconds=deadline_seconds,
        )
        assert len(stdout.stderr_after_exit) > 5
        assert len(set(stdout.stderr_after_exit)) == 1
        stderr_before_release = stdout.stderr_after_exit[0]
        assert result.local_status == result.exit_status == 0
        assert result.failure == expected_failure
        assert not result.stderr.complete
        if stall_seconds is None:
            assert stdout.data == b""
            assert stderr.bytes_written == stderr_before_release
        else:
            assert stdout.data == b"payload"
            assert stderr.bytes_written > stderr_before_release
        assert_closed(children)
    finally:
        release.touch()
        until = time.monotonic() + 5
        while not done.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert done.exists()


def test_input_pipe_failure_is_safe(children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch) -> None:
    def write(fd: int, data: bytes) -> int:
        raise OSError("secret-canary")

    monkeypatch.setattr(os, "write", write)
    result = execute("import time; time.sleep(30)", io=CarrierIO(input=FiniteInput(b"secret-canary")))
    assert result.failure == Failure.INPUT
    assert result.exit_status is None
    assert "secret-canary" not in repr(result)
    assert_closed(children)


def test_output_pipe_failure_is_safe(children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.read

    def read(fd: int, size: int) -> bytes:
        if children:
            pipe = children[-1].stderr
            assert pipe is not None
            if fd == pipe.fileno():
                raise OSError("secret-canary")
        return original(fd, size)

    monkeypatch.setattr(os, "read", read)
    result = execute("import time; time.sleep(30)")
    assert result.failure == Failure.OUTPUT
    assert result.exit_status is None
    assert not result.stdout.complete and not result.stderr.complete
    assert "secret-canary" not in repr(result)
    assert_closed(children)


def test_nonblocking_setup_failure_reaps_without_completion_claim(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def set_blocking(fd: int, blocking: bool) -> None:
        raise OSError("secret-canary")

    monkeypatch.setattr(os, "set_blocking", set_blocking)
    result = execute("import time; time.sleep(30)")
    assert result.failure == Failure.OBSERVATION
    assert result.local_status is not None
    assert result.exit_status is None
    assert "secret-canary" not in repr(result)
    assert_closed(children)


@pytest.mark.parametrize("flood", [False, True])
def test_descendant_output_handles_have_a_bounded_post_exit_drain(
    children: list[subprocess.Popen[bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flood: bool,
) -> None:
    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "import os,pathlib,time; "
        f"release=pathlib.Path({str(release)!r}); done=pathlib.Path({str(done)!r}); "
        "end=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<end:\n"
        + (" try: os.write(2,b'e'*65536)\n except OSError: break\n" if flood else " time.sleep(.01)\n")
        + "done.touch()"
    )
    script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}], stdin=subprocess.DEVNULL); "
        "sys.stdout.write('parent')"
    )
    if flood:
        original = _subprocess._Output.advance

        def advance(output: _subprocess._Output, pipe: Any) -> tuple[bool, bool]:
            progressed, failed = original(output, pipe)
            return progressed or not output.eof, failed

        monkeypatch.setattr(_subprocess._Output, "advance", advance)
    try:
        started = time.monotonic()
        result = execute(script, io=CarrierIO(output=Capture(32)), seconds=None)
        assert time.monotonic() - started < 2
        assert result.stdout.data == b"parent"
        assert len(result.stderr.data) <= 32
        assert result.local_status == result.exit_status == 0
        assert result.failure == Failure.OUTPUT
        assert not result.stdout.complete and not result.stderr.complete
        assert_closed(children)
    finally:
        release.touch()
        until = time.monotonic() + 5
        while not done.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert done.exists()


def test_ready_sink_does_not_reset_bounded_descendant_collection(
    children: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    class CountingSink:
        def __init__(self) -> None:
            self.bytes_written = 0

        def try_write(self, data: memoryview) -> int | None:
            self.bytes_written += len(data)
            return len(data)

    release = tmp_path / "release"
    done = tmp_path / "done"
    descendant = (
        "import os,pathlib,time; "
        f"release=pathlib.Path({str(release)!r}); done=pathlib.Path({str(done)!r}); "
        "end=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<end:\n"
        " try: os.write(1,b'x'*65536)\n"
        " except OSError: break\n"
        "done.touch()"
    )
    script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}], "
        "stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "sys.stdout.buffer.write(b'parent'); sys.stdout.buffer.flush()"
    )
    stdout = CountingSink()
    try:
        started = time.monotonic()
        result = execute(
            script,
            io=CarrierIO(output=SinkOutput(stdout, ShortSink(64))),
            seconds=None,
        )
        assert time.monotonic() - started < 2
        assert stdout.bytes_written > len(b"parent")
        assert result.local_status == result.exit_status == 0
        assert result.failure == Failure.OUTPUT
        assert not result.stdout.complete
        assert_closed(children)
    finally:
        release.touch()
        until = time.monotonic() + 5
        while not done.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert done.exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interruption_reaps_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    def advance(output: _subprocess._Output, pipe: Any) -> tuple[bool, bool]:
        raise interruption()

    monkeypatch.setattr(_subprocess._Output, "advance", advance)
    with pytest.raises(interruption):
        execute("import time; time.sleep(30)")
    assert_closed(children)


def test_failed_reap_is_observation_failure(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Popen.wait
    original_waitpid = os.waitpid if os.name != "nt" else None

    def wait(process: Popen[bytes], timeout: float | None = None) -> int:
        if process is children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original(process, timeout=timeout)

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        if children and pid == children[-1].pid:
            raise OSError("secret-canary")
        assert original_waitpid is not None
        return original_waitpid(pid, options)

    with monkeypatch.context() as context:
        if os.name == "nt":
            context.setattr(Popen, "wait", wait)
        else:
            context.setattr(os, "waitpid", waitpid)
        result = execute("import time; time.sleep(30)", seconds=0.1)
    assert result.failure == Failure.OBSERVATION
    assert result.local_status is None
    assert result.exit_status is None
    assert "secret-canary" not in repr(result)
    children[-1].wait(timeout=2)
    assert_closed(children)


def test_failed_reap_during_interruption_adds_safe_note(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_wait = Popen.wait
    original_waitpid = os.waitpid if os.name != "nt" else None
    waitpid_calls = 0

    def advance(output: _subprocess._Output, pipe: Any) -> tuple[bool, bool]:
        raise KeyboardInterrupt()

    def wait(process: Popen[bytes], timeout: float | None = None) -> int:
        if process is children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original_wait(process, timeout=timeout)

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        nonlocal waitpid_calls
        if children and pid == children[-1].pid:
            waitpid_calls += 1
            if waitpid_calls > 2:
                raise OSError("secret-canary")
        assert original_waitpid is not None
        return original_waitpid(pid, options)

    with monkeypatch.context() as context:
        context.setattr(_subprocess._Output, "advance", advance)
        if os.name == "nt":
            context.setattr(Popen, "wait", wait)
        else:
            context.setattr(os, "waitpid", waitpid)
        with pytest.raises(KeyboardInterrupt) as raised:
            execute("import time; time.sleep(30)")
    assert raised.value.__notes__
    assert "secret-canary" not in repr(raised.value.__notes__)
    children[-1].wait(timeout=2)
    assert_closed(children)


def test_explicit_environment_is_passed_through(children: list[subprocess.Popen[bytes]]) -> None:
    env = dict(os.environ)
    env["AGENTWORKS_PUMP_FIXTURE"] = "caller-owned-value"
    result = execute(
        "import os,sys; sys.stdout.write(os.environ['AGENTWORKS_PUMP_FIXTURE'])",
        env=env,
    )
    assert result.stdout.data == b"caller-owned-value"
    assert result.failure is None
    assert result.exit_status == 0
    assert_closed(children)


def test_spawn_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("secret-canary")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = execute("pass")
    assert not result.started
    assert result.local_status is None and result.exit_status is None
    assert result.failure == Failure.DISPATCH
    assert "secret-canary" not in repr(result)
