"""Synthetic children and owned PTYs exercise the private relay candidate."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Event, current_thread
from typing import Any, cast

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import (
    BorrowedProcessStdin,
    LocalProcessOwner,
    LocalProcessRequest,
    LocalProcessTerminal,
)
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, Provenance, Retention, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh import _terminal_relay as relay
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs")


@dataclass
class Source:
    ready: Event
    chunks: list[bytes | None] = field(repr=False)
    calls: int = 0
    handed_off: bool = False

    def try_read(self, limit: int) -> bytes | None:
        self.calls += 1
        if not self.ready.is_set():
            return None
        chunk = self.chunks.pop(0)
        if chunk == b"":
            self.handed_off = True
        return chunk


@dataclass
class Sink:
    ready: Event
    data: bytearray = field(default_factory=bytearray, repr=False)
    limit: int = 7
    stalls: int = 2

    def try_write(self, data: memoryview) -> int | None:
        if self.stalls:
            self.stalls -= 1
            return None
        count = min(len(data), self.limit)
        self.data.extend(data[:count])
        if b"READY" in self.data:
            self.ready.set()
        return count


@pytest.fixture(autouse=True)
def owned_children(monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody) -> Iterator[None]:
    """Independently check each synthetic child was reaped before fixture release."""
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", original(*args, **kwargs))
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        yield
    finally:
        assert custody.close(Deadline.after(3))
        for child in children:
            try:
                assert child.returncode is not None
                with pytest.raises(ChildProcessError):
                    os.waitpid(child.pid, os.WNOHANG)
                assert all(pipe is None or pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
            finally:
                if child.returncode is None:
                    child.kill()
                    child.wait()


@pytest.fixture
def endpoint(custody: LocalDeliveryCustody) -> Iterator[tuple[int, int]]:
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        assert custody.close(Deadline.after(3))
        os.close(slave)
        os.close(master)


def _io(endpoint: tuple[int, int], chunks: list[bytes | None]) -> tuple[CarrierIO, Source, Sink, Sink]:
    ready = Event()
    source = Source(ready, chunks)
    stdout, stderr = Sink(ready), Sink(ready)
    return (
        CarrierIO(
            TerminalInput(endpoint[1], endpoint[1], "fixture-terminal", source, True), SinkOutput(stdout, stderr)
        ),
        source,
        stdout,
        stderr,
    )


def _argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]


_RAW_READY = "import os,tty; tty.setraw(0); os.write(1,b'READY'); "


@pytest.mark.parametrize("nonblocking", [False, True])
def test_binary_payload_then_early_keys_partial_writes_and_streams(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, nonblocking: bool, custody: LocalDeliveryCustody
) -> None:
    import fcntl
    import termios

    mode = termios.tcgetattr(endpoint[1])
    mode[6][termios.VINTR] = b"\x07"
    mode[6][termios.VMIN] = b"\x09"
    termios.tcsetattr(endpoint[1], termios.TCSANOW, mode)
    termios.tcsetwinsize(endpoint[1], (31, 97))
    os.set_blocking(endpoint[1], not nonblocking)
    os.set_inheritable(endpoint[1], nonblocking)
    flags = fcntl.fcntl(endpoint[1], fcntl.F_GETFL), os.get_inheritable(endpoint[1])
    payload = b"payload-canary\x00\xff\r\n"
    keys = b"\x16\x07\r"
    io, source, stdout, stderr = _io(endpoint, [payload, None, b""])
    original_start = LocalProcessOwner.start
    original_write = os.write
    original_read = os.read
    writes = 0
    descriptors: list[int] = []

    def start(
        owner: LocalProcessOwner, request: LocalProcessRequest, *, close_deadline: ProcessDeadline | None = None
    ) -> None:
        assert not termios.tcgetattr(endpoint[1])[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
        assert isinstance(request.input, BorrowedProcessStdin)
        descriptors.append(request.input.descriptor)
        assert_preserved_terminal_mode(termios.tcgetattr(request.input.descriptor), mode)
        assert termios.tcgetwinsize(request.input.descriptor) == (31, 97)
        os.write(endpoint[0], keys)
        original_start(owner, request, close_deadline=close_deadline)

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal writes
        if fd not in endpoint:
            writes += 1
            if writes == 1:
                raise BlockingIOError
            return original_write(fd, data[:2])
        return original_write(fd, data)

    def read(fd: int, limit: int) -> bytes:
        if fd == endpoint[1]:
            assert source.handed_off
        return original_read(fd, limit)

    monkeypatch.setattr(LocalProcessOwner, "start", start)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "read", read)
    code = (
        _RAW_READY
        + f"""
expected={payload + keys!r}
data=b''
while len(data)<len(expected): data+=os.read(0,len(expected)-len(data))
assert data==expected
os.write(2,b'RAW-STDERR')
os.write(1,(':'+os.environ['TERM']+':'+str(__import__('termios').tcgetwinsize(0))).encode())
"""
    )
    result = relay.run_terminal_relay_candidate(_argv(code), io=io, deadline=Deadline.after(5), custody=custody)
    assert result.failure is None and result.exit_status == 0
    assert bytes(stdout.data) == b"READY:fixture-terminal:(31, 97)"
    assert bytes(stderr.data) == b"RAW-STDERR"
    assert result.stdout.provenance is Provenance.CARRIER_STDOUT
    assert result.stderr.provenance is Provenance.MIXED_STDERR
    assert result.stdout.retention is result.stderr.retention is Retention.DELIVERED
    assert result.stdout.complete and result.stderr.complete
    assert result.stdout.data == result.stderr.data == b""
    assert b"payload-canary" not in repr(result).encode()
    assert_preserved_terminal_mode(termios.tcgetattr(endpoint[1]), mode)
    assert (fcntl.fcntl(endpoint[1], fcntl.F_GETFL), os.get_inheritable(endpoint[1])) == flags
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_withholding_source_does_not_consume_keyboard(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    io, source, _, _ = _io(endpoint, [None] * 100)
    original_read = os.read
    borrowed_reads = 0

    def read(fd: int, limit: int) -> bytes:
        nonlocal borrowed_reads
        if fd == endpoint[1]:
            borrowed_reads += 1
        return original_read(fd, limit)

    monkeypatch.setattr(os, "read", read)
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "__import__('time').sleep(30)"), io=io, deadline=Deadline.after(0.2), custody=custody
    )
    assert result.started and result.failure is Failure.DEADLINE
    assert source.calls and not source.handed_off and borrowed_reads == 0
    assert result.exit_status is None


def test_pending_payload_drains_before_source_repoll(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    io, source, _, _ = _io(endpoint, [b"abcdef", b""])
    original_write = os.write
    observed_calls: list[int] = []

    def write(fd: int, data: bytes | memoryview) -> int:
        observed_calls.append(source.calls)
        return original_write(fd, data[:1])

    monkeypatch.setattr(os, "write", write)
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "data=b''\nwhile len(data)<6: data+=os.read(0,6-len(data))\n__import__('time').sleep(.03)"),
        io=io,
        deadline=Deadline.after(3),
        custody=custody,
    )
    assert result.failure is None
    assert len(observed_calls) == 6 and len(set(observed_calls)) == 1


@pytest.mark.parametrize("bad", [b"x" * (relay._CHUNK + 1), 3, "secret-canary"])
def test_invalid_source_is_input_failure_without_raw_diagnostics(
    endpoint: tuple[int, int], bad: object, custody: LocalDeliveryCustody
) -> None:
    io, source, _, _ = _io(endpoint, [])

    class BadSource:
        def try_read(self, limit: int) -> bytes | None:
            return bad  # type: ignore[return-value]

    io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", BadSource()), io.output)
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "__import__('time').sleep(30)"), io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.failure is Failure.INPUT
    assert "secret-canary" not in repr(result)


@pytest.mark.parametrize("bad", [0, -1, 1000000, True, "secret-canary"])
def test_invalid_sink_is_output_failure_without_raw_diagnostics(
    endpoint: tuple[int, int], bad: object, custody: LocalDeliveryCustody
) -> None:
    class BadSink:
        def try_write(self, data: memoryview) -> int | None:
            return bad  # type: ignore[return-value]

    io, _, _, stderr = _io(endpoint, [b""])
    io = CarrierIO(io.input, SinkOutput(BadSink(), stderr))
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "__import__('time').sleep(30)"), io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.failure is Failure.OUTPUT
    assert "secret-canary" not in repr(result)


def test_stalled_sink_still_observes_deadline_and_restores(
    endpoint: tuple[int, int], custody: LocalDeliveryCustody
) -> None:
    import termios

    mode = termios.tcgetattr(endpoint[1])
    io, _, stdout, _ = _io(endpoint, [b""])
    stdout.stalls = 1000
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "os.write(2,b'other-stream'); __import__('time').sleep(30)"),
        io=io,
        deadline=Deadline.after(0.2),
        custody=custody,
    )
    assert result.failure is Failure.DEADLINE
    assert_preserved_terminal_mode(termios.tcgetattr(endpoint[1]), mode)


def test_expired_deadline_has_no_terminal_or_process_effects(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    io, _, _, _ = _io(endpoint, [b""])

    def forbidden(*args: object) -> None:
        pytest.fail("expired operation performed an effect")

    monkeypatch.setattr(PosixTerminal, "acquire", forbidden)
    monkeypatch.setattr(LocalProcessOwner, "start", forbidden)
    result = relay.run_terminal_relay_candidate(_argv("pass"), io=io, deadline=Deadline.after(0), custody=custody)
    assert not result.started and result.failure is Failure.DEADLINE


@pytest.mark.parametrize("boundary", ["source", "stdout", "stderr"])
def test_endpoint_exception_is_categorized(
    endpoint: tuple[int, int], boundary: str, custody: LocalDeliveryCustody
) -> None:
    class BrokenEndpoint:
        def try_read(self, limit: int) -> bytes | None:
            raise RuntimeError("sensitive-source-canary")

        def try_write(self, data: memoryview) -> int | None:
            raise RuntimeError("sensitive-sink-canary")

    io, _, stdout, stderr = _io(endpoint, [b""])
    if boundary == "source":
        io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", BrokenEndpoint()), io.output)
    else:
        io = CarrierIO(
            io.input,
            SinkOutput(
                BrokenEndpoint() if boundary == "stdout" else stdout,
                BrokenEndpoint() if boundary == "stderr" else stderr,
            ),
        )
    result = relay.run_terminal_relay_candidate(
        _argv(_RAW_READY + "os.write(2,b'error'); __import__('time').sleep(30)"),
        io=io,
        deadline=Deadline.after(3),
        custody=custody,
    )
    assert result.failure is (Failure.INPUT if boundary == "source" else Failure.OUTPUT)
    assert "canary" not in repr(result)


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_launch_control_exception_settles_before_terminal_release(
    endpoint: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException],
    custody: LocalDeliveryCustody,
) -> None:
    import termios

    mode = termios.tcgetattr(endpoint[1])
    io, _, _, _ = _io(endpoint, [b""])
    original_start = LocalProcessOwner.start
    original_close = LocalProcessOwner.close_bounded
    original_release = PosixTerminal.release
    settled = False
    primary = control("first-control")

    def start(
        owner: LocalProcessOwner, request: LocalProcessRequest, *, close_deadline: ProcessDeadline | None = None
    ) -> None:
        original_start(owner, request, close_deadline=close_deadline)
        raise primary

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> LocalProcessTerminal:
        nonlocal settled
        result = original_close(owner, deadline)
        settled = True
        assert result is not None
        return result

    def release(terminal: PosixTerminal) -> tuple[BaseException, ...]:
        assert settled
        return original_release(terminal)

    monkeypatch.setattr(LocalProcessOwner, "start", start)
    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    monkeypatch.setattr(PosixTerminal, "release", release)
    with pytest.raises(control) as raised:
        relay.run_terminal_relay_candidate(
            _argv("__import__('time').sleep(30)"), io=io, deadline=Deadline.after(3), custody=custody
        )
    assert raised.value is primary and settled
    assert_preserved_terminal_mode(termios.tcgetattr(endpoint[1]), mode)


def test_interrupted_wait_uses_completion_fact_and_preserves_first_control(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    import termios

    mode = termios.tcgetattr(endpoint[1])
    io, source, _, _ = _io(endpoint, [None] * 100)
    attempt = relay._Attempt(_argv(_RAW_READY + "__import__('time').sleep(30)"), io, Deadline.after(3), custody=custody)
    original_wait = attempt._operation_done.wait
    primary = KeyboardInterrupt("first-control")
    calls = 0
    thread = current_thread()

    def wait(timeout: float | None = None) -> bool:
        nonlocal calls
        assert current_thread() is thread
        calls += 1
        if calls == 1:
            assert source.ready.wait(2)
            raise primary
        if calls == 2:
            raise SystemExit("second-control")
        return original_wait(timeout)

    def forbidden_join(*args: object, **kwargs: object) -> None:
        pytest.fail("Thread status cannot establish native worker completion")

    monkeypatch.setattr(attempt._operation_done, "wait", wait)
    monkeypatch.setattr(attempt._done, "wait", wait)
    monkeypatch.setattr(attempt._worker, "join", forbidden_join)
    monkeypatch.setattr(attempt._worker, "is_alive", forbidden_join)
    with pytest.raises(KeyboardInterrupt) as raised:
        attempt.run()
    assert raised.value is primary and attempt._done.is_set()
    assert calls >= 3
    assert_preserved_terminal_mode(termios.tcgetattr(endpoint[1]), mode)


def test_interrupted_worker_start_cannot_admit_terminal_effects(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    io, _, _, _ = _io(endpoint, [b""])
    attempt = relay._Attempt(_argv("pass"), io, Deadline.after(3), custody=custody)
    original_start = attempt._worker.start
    primary = KeyboardInterrupt("start-control")

    def start() -> None:
        original_start()
        raise primary

    def forbidden(*args: object) -> None:
        pytest.fail("Canceled worker acquired a native terminal")

    monkeypatch.setattr(attempt._worker, "start", start)
    monkeypatch.setattr(PosixTerminal, "acquire", forbidden)
    with pytest.raises(KeyboardInterrupt) as raised:
        attempt.run()
    assert raised.value is primary
    assert attempt._done.wait(2)
    assert not attempt._admitted


@pytest.mark.parametrize("control", [None, KeyboardInterrupt, SystemExit])
def test_restoration_uncertainty_is_observable(
    endpoint: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException] | None,
    custody: LocalDeliveryCustody,
) -> None:
    import termios

    custody = LocalDeliveryCustody()
    io, source, _, _ = _io(endpoint, [b""])
    original_setattr = termios.tcsetattr
    primary = control("restore-control") if control else OSError("restore-canary")

    def setattr(fd: int, when: int, mode: list[object]) -> None:
        original_setattr(fd, when, mode)
        if fd == endpoint[1] and source.handed_off:
            raise primary

    monkeypatch.setattr(termios, "tcsetattr", setattr)
    if control:
        with pytest.raises(control) as raised:
            relay.run_terminal_relay_candidate(
                _argv(_RAW_READY + "__import__('time').sleep(.05)"), io=io, deadline=Deadline.after(3), custody=custody
            )
        assert raised.value is primary
        assert raised.value.__notes__
    else:
        result = relay.run_terminal_relay_candidate(
            _argv(_RAW_READY + "__import__('time').sleep(.05)"), io=io, deadline=Deadline.after(3), custody=custody
        )
        assert result.failure is Failure.OBSERVATION
        assert "restore-canary" not in repr(result)

    assert not custody.settled
    assert not custody.close(Deadline.after(0.1))
    from agentworks.errors import StateError

    with pytest.raises(StateError):
        custody.begin_process()


def test_fair_large_duplex_delivery(endpoint: tuple[int, int], custody: LocalDeliveryCustody) -> None:
    payload = bytes(range(256)) * 256
    io, _, stdout, stderr = _io(endpoint, [payload, payload, b""])
    stdout.limit = stderr.limit = 2048
    code = (
        _RAW_READY
        + """
for fd in (1,2):
    pending=b'o'*131072
    while pending: pending=pending[os.write(fd,pending):]
data=b''
while len(data)<131072: data+=os.read(0,131072-len(data))
assert data==bytes(range(256))*512
__import__('time').sleep(.03)
"""
    )
    result = relay.run_terminal_relay_candidate(_argv(code), io=io, deadline=Deadline.after(5), custody=custody)
    assert result.failure is None and result.exit_status == 0
    assert bytes(stdout.data) == b"READY" + b"o" * 131072
    assert bytes(stderr.data) == b"o" * 131072
