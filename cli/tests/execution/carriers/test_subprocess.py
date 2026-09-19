"""Behavioral coverage for the shared owned-process pump."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from subprocess import Popen
from typing import Any

import pytest

from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    Deadline,
    Discard,
    Failure,
    FiniteInput,
    Provenance,
    Retention,
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
) -> ProcessResult:
    return run_process(
        [sys.executable, "-c", script],
        io=io or CarrierIO(),
        deadline=Deadline.after(seconds),
        env=env,
    )


def assert_closed(children: list[subprocess.Popen[bytes]]) -> None:
    assert children
    for child in children:
        assert child.poll() is not None
        for pipe in (child.stdin, child.stdout, child.stderr):
            assert pipe is None or pipe.closed


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
    original_read = _subprocess._Output.read
    spawn = subprocess.Popen

    def read(output: _subprocess._Output, pipe: Any) -> bool:
        progressed = original_read(output, pipe)
        observed.update(marker for marker in markers if marker in output.data)
        return progressed

    def delayed_startup(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        time.sleep(0.3)
        return spawn(argv, **kwargs)

    deadline = Deadline.after(10)
    monkeypatch.setattr(_subprocess._Output, "read", read)
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

    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + startup_offset)
    deadline = Deadline.after(1)
    monkeypatch.setattr(subprocess, "Popen", complete_startup)
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
        original = _subprocess._Output.read

        def read(output: _subprocess._Output, pipe: Any) -> bool:
            progressed = original(output, pipe)
            return progressed or not output.eof

        monkeypatch.setattr(_subprocess._Output, "read", read)
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


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interruption_reaps_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    def read(output: _subprocess._Output, pipe: Any) -> bool:
        raise interruption()

    monkeypatch.setattr(_subprocess._Output, "read", read)
    with pytest.raises(interruption):
        execute("import time; time.sleep(30)")
    assert_closed(children)


def test_failed_reap_is_observation_failure(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Popen.wait

    def wait(process: Popen[bytes], timeout: float | None = None) -> int:
        if process is children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original(process, timeout=timeout)

    with monkeypatch.context() as context:
        context.setattr(Popen, "wait", wait)
        result = execute("import time; time.sleep(30)", seconds=0.1)
    assert result.failure == Failure.OBSERVATION
    assert result.exit_status is None
    assert "secret-canary" not in repr(result)
    children[-1].wait(timeout=2)
    assert_closed(children)


def test_failed_reap_during_interruption_adds_safe_note(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_wait = Popen.wait

    def read(output: _subprocess._Output, pipe: Any) -> bool:
        raise KeyboardInterrupt()

    def wait(process: Popen[bytes], timeout: float | None = None) -> int:
        if process is children[-1]:
            raise subprocess.TimeoutExpired("secret-canary", timeout)
        return original_wait(process, timeout=timeout)

    with monkeypatch.context() as context:
        context.setattr(_subprocess._Output, "read", read)
        context.setattr(Popen, "wait", wait)
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
