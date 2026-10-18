"""Consumer closure ends input offers while the owned process and output settle."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution import _process as core
from tests.execution.test_process_core import CORE_PATH
from tests.execution.test_process_core import interpreter as interpreter
from tests.execution.test_process_launch_owner import _assert_exact_cleanup, _wait_snapshot
from tests.execution.test_process_launch_owner import children as children

pytestmark = pytest.mark.windows


class OpenSource:
    def __init__(self, *, stalled: bool = False) -> None:
        self.stalled = stalled
        self.calls = 0
        self.closed = False
        self.forbidden = False

    def try_read(self, limit: int) -> bytes | None:
        assert not self.forbidden, "source borrowed after consumer completion"
        self.calls += 1
        return None if self.stalled else b"x" * limit

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("status", [0, 1, 255])
@pytest.mark.parametrize("kind", ["finite", "stalled", "endless"])
def test_no_read_exit_drains_both_streams_without_source_eof(
    status: int, kind: str, children: list[subprocess.Popen[bytes]]
) -> None:
    source = OpenSource(stalled=kind == "stalled")
    owner = core.LocalProcessOwner()
    stdout = bytes(range(256)) * 512
    stderr = b"diagnostic\x00\xff" * 8192
    result = core.run_owned_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import sys; "
            "sys.stdout.buffer.write(bytes(range(256))*512); "
            "sys.stderr.buffer.write(b'diagnostic\\x00\\xff'*8192); "
            f"sys.exit({status})",
        ],
        input=core.ProcessInput(data=b"x" * 2_000_000) if kind == "finite" else core.ProcessInput(source=source),
        output=core.ProcessOutput(capture_limit=max(len(stdout), len(stderr))),
        deadline=core.Deadline(time.monotonic() + 5),
        owner=owner,
    )
    assert result.failure is None
    assert result.local_status == result.exit_status == status
    assert result.stdout.data == stdout and result.stderr.data == stderr
    assert result.stdout.complete and result.stderr.complete
    calls = source.calls
    source.forbidden = True
    time.sleep(0.02)
    assert source.calls == calls and not source.closed
    assert len(children) == 1
    _assert_exact_cleanup(children)
    assert owner.close().cleaned


@pytest.mark.parametrize("status", [0, 1, 255])
@pytest.mark.parametrize("finite", [False, True])
def test_prefix_consumer_does_not_require_remaining_input(
    status: int, finite: bool, children: list[subprocess.Popen[bytes]]
) -> None:
    source = OpenSource()
    result = core.run_owned_process(
        [
            sys.executable,
            "-c",
            "import os,sys; assert os.read(0,1)==b'x'; "
            "sys.stdout.buffer.write(b'complete\\x00'*16000); "
            f"sys.stderr.buffer.write(b'final'); sys.exit({status})",
        ],
        input=core.ProcessInput(data=b"x" * 2_000_000) if finite else core.ProcessInput(source=source),
        output=core.ProcessOutput(capture_limit=200_000),
        deadline=core.Deadline(time.monotonic() + 5),
        owner=core.LocalProcessOwner(),
    )
    source.forbidden = True
    assert result.failure is None
    assert result.local_status == result.exit_status == status
    assert result.stdout.data == b"complete\x00" * 16000 and result.stderr.data == b"final"
    assert result.stdout.complete and result.stderr.complete
    assert not source.closed and len(children) == 1
    _assert_exact_cleanup(children)


def test_broken_input_pipe_stops_source_before_later_output_and_sink_pressure(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    source = OpenSource()
    native_write = os.write
    close_stdin = core.LocalProcessPipes.close_stdin
    broken = False
    close_attempts = 0

    def observe_write(fd: int, data: Any) -> int:
        nonlocal broken
        try:
            return native_write(fd, data)
        except BrokenPipeError:
            broken = True
            source.forbidden = True
            raise

    def observe_close(pipes: core.LocalProcessPipes) -> None:
        nonlocal close_attempts
        close_attempts += 1
        close_stdin(pipes)

    class SlowSink:
        def __init__(self) -> None:
            self.data = bytearray()
            self.calls = 0
            self.release: float | None = None

        def try_write(self, data: memoryview) -> int | None:
            self.calls += 1
            if self.release is None:
                self.release = time.monotonic() + 0.15
            if time.monotonic() < self.release:
                return None
            written = min(4096, len(data))
            self.data.extend(data[:written])
            return written

    stdout, stderr = SlowSink(), SlowSink()
    monkeypatch.setattr(os, "write", observe_write)
    monkeypatch.setattr(core.LocalProcessPipes, "close_stdin", observe_close)
    owner = core.LocalProcessOwner()
    result = core.run_owned_process(
        [
            sys.executable,
            "-c",
            "import os,sys,time; os.close(0); time.sleep(.05); "
            "sys.stdout.buffer.write(b'after-close'*16000); sys.stdout.buffer.flush(); "
            "sys.stderr.buffer.write(b'final'); sys.exit(1)",
        ],
        input=core.ProcessInput(source=source),
        output=core.ProcessOutput(stdout_sink=stdout, stderr_sink=stderr),
        deadline=core.Deadline(time.monotonic() + 5),
        owner=owner,
    )
    assert broken and source.calls > 0 and close_attempts == 1
    assert result.failure is None and result.local_status == result.exit_status == 1
    assert bytes(stdout.data) == b"after-close" * 16000 and bytes(stderr.data) == b"final"
    assert result.stdout.complete and result.stderr.complete
    calls = (source.calls, stdout.calls, stderr.calls)
    time.sleep(0.02)
    assert (source.calls, stdout.calls, stderr.calls) == calls
    assert not source.closed and len(children) == 1 and owner.close().cleaned
    _assert_exact_cleanup(children)


def test_known_exit_never_borrows_source_and_keeps_final_output(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    source = OpenSource()
    source.forbidden = True
    pump = core._pump_owned_pipes

    def after_exit(owner: core.LocalProcessOwner, *args: Any) -> tuple[core.ProcessFailure | None, int | None]:
        _wait_snapshot(owner, lambda snapshot: snapshot.exit_status == 255)
        return pump(owner, *args)

    monkeypatch.setattr(core, "_pump_owned_pipes", after_exit)
    result = core.run_owned_process(
        [sys.executable, "-c", "import os; os.write(1,b'final'); os.write(2,b'err'); raise SystemExit(255)"],
        input=core.ProcessInput(source=source),
        output=core.ProcessOutput(capture_limit=1024),
        deadline=core.Deadline(time.monotonic() + 5),
        owner=core.LocalProcessOwner(),
    )
    assert result.failure is None and result.local_status == result.exit_status == 255
    assert result.stdout.data == b"final" and result.stderr.data == b"err"
    assert result.stdout.complete and result.stderr.complete
    assert source.calls == 0 and not source.closed and len(children) == 1
    _assert_exact_cleanup(children)


@pytest.mark.parametrize("number", [errno.EIO, errno.EINVAL])
def test_other_input_pipe_errors_remain_failures(
    number: int, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    native_write = os.write

    def fail_input(fd: int, data: Any) -> int:
        if children and children[0].stdin is not None and fd == children[0].stdin.fileno():
            raise OSError(number, "injected input fault")
        return native_write(fd, data)

    monkeypatch.setattr(os, "write", fail_input)
    result = core.run_owned_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        input=core.ProcessInput(data=b"offered"),
        output=core.ProcessOutput(capture_limit=1024),
        deadline=core.Deadline(time.monotonic() + 5),
        owner=core.LocalProcessOwner(),
    )
    assert result.failure == core.ProcessFailure.INPUT
    assert result.exit_status is None and result.local_status is not None
    assert len(children) == 1
    _assert_exact_cleanup(children)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_known_exit_owned_close_failure_keeps_original_uncertainty(
    error_type: type[BaseException], monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    source = OpenSource()
    source.forbidden = True
    pump = core._pump_owned_pipes
    owner = core.LocalProcessOwner()
    control = error_type()
    close_attempts = 0
    native_closes = []

    def fail_close() -> None:
        nonlocal close_attempts
        close_attempts += 1
        raise control

    def after_exit(target: core.LocalProcessOwner, pipes: core.LocalProcessPipes, *args: Any) -> Any:
        _wait_snapshot(target, lambda snapshot: snapshot.exit_status == 0)
        assert pipes.stdin is not None
        native_closes.append(pipes.stdin.close)
        monkeypatch.setattr(pipes.stdin, "close", fail_close)
        return pump(target, pipes, *args)

    monkeypatch.setattr(core, "_pump_owned_pipes", after_exit)

    def execute() -> core.ProcessResult:
        return core.run_owned_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            input=core.ProcessInput(source=source),
            output=core.ProcessOutput(capture_limit=1024),
            deadline=core.Deadline(time.monotonic() + 5),
            owner=owner,
        )

    try:
        if error_type is OSError:
            result = execute()
            assert result.failure == core.ProcessFailure.OBSERVATION
            assert result.local_status == result.exit_status == 0
        else:
            with pytest.raises(error_type) as caught:
                execute()
            assert caught.value is control
        terminal = owner.close()
        assert not terminal.cleaned and not terminal.cleanup_retryable
        assert terminal.exit_status == terminal.local_status == 0
        assert owner.close() is terminal and close_attempts == 1
        assert source.calls == 0 and not source.closed and len(children) == 1
        assert owner._retained_status is not None
        pipes = owner._retained_status.pipes
        assert pipes is not None and pipes.stdin is not None and not pipes.stdin.closed
        record = pipes._record(pipes.stdin)
        assert record.attempted and not record.confirmed
    finally:
        for close in native_closes:
            close()
        for child in children:
            if child.stdin is not None and native_closes:
                monkeypatch.setattr(child.stdin, "close", native_closes[0])
    _assert_exact_cleanup(children)


def test_standalone_consumer_completion_without_agentworks_imports(interpreter: Path) -> None:
    script = r"""
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location('standalone_completion_core', sys.argv[1])
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
assert 'agentworks' not in sys.modules
class Source:
    def try_read(self, limit): return b'x' * limit
for code in (0,1,255):
    for finite in (False,True):
        result = core.run_owned_process(
            [sys.executable, '-I', '-S', '-B', '-c',
             'import os,sys; os.read(0,1); os.close(0); '
             'sys.stdout.buffer.write(b"final"*20000); sys.stderr.buffer.write(b"err"); '
             'sys.exit('+str(code)+')'],
            input=core.ProcessInput(data=b'x'*2000000) if finite else core.ProcessInput(source=Source()),
            output=core.ProcessOutput(capture_limit=200000),
            deadline=core.Deadline(time.monotonic()+5), owner=core.LocalProcessOwner())
        assert result.failure is None and result.local_status == result.exit_status == code
        assert result.stdout.data == b'final'*20000 and result.stderr.data == b'err'
        assert result.stdout.complete and result.stderr.complete
assert 'agentworks' not in sys.modules
print('consumer-completion-reused')
"""
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", script, str(CORE_PATH)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=40,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == b"consumer-completion-reused\n" and completed.stderr == b""
