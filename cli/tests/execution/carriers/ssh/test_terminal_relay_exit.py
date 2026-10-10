"""Exit observations preserve a final preparation handoff without new dispatch."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from threading import Event

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import LocalProcessOwner, LocalProcessSnapshot, LocalProcessTerminal
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, SinkOutput, TerminalInput
from agentworks.execution.carriers.ssh._terminal_relay import _Attempt, run_terminal_relay_candidate

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires owned POSIX PTYs")


@pytest.fixture
def endpoint(custody: LocalDeliveryCustody) -> Iterator[tuple[int, int]]:
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        assert custody.close(Deadline.after(3))
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize("final_chunk", [b"", None, b"extra-payload"])
def test_final_handoff_observation_after_exit(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, final_chunk: bytes | None, custody: LocalDeliveryCustody
) -> None:
    ready, exited = Event(), Event()
    original_snapshot = LocalProcessOwner.snapshot
    output = bytearray()
    handoff_reads = 0

    class Source:
        sent = False

        def try_read(self, limit: int) -> bytes | None:
            nonlocal handoff_reads
            if not ready.is_set():
                return None
            if not self.sent:
                self.sent = True
                return b"x"
            if exited.is_set():
                handoff_reads += 1
                return final_chunk
            return None

    class Sink:
        def try_write(self, data: memoryview) -> int:
            output.extend(data)
            if b"READY" in output:
                ready.set()
            return len(data)

    def snapshot(owner: LocalProcessOwner) -> LocalProcessSnapshot:
        observed = original_snapshot(owner)
        if observed.exit_status is not None:
            exited.set()
        return observed

    monkeypatch.setattr(LocalProcessOwner, "snapshot", snapshot)
    io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", Source()), SinkOutput(Sink(), Sink()))
    code = "import os,tty; tty.setraw(0); os.write(1,b'READY'); assert os.read(0,1)==b'x'; os.write(1,b'ACK')"
    result = run_terminal_relay_candidate(
        [sys.executable, "-c", code], io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.started and result.exit_status == 0
    assert bytes(output) == b"READYACK"
    assert handoff_reads == 1
    assert result.failure is (None if final_chunk == b"" else Failure.INPUT)


def test_published_close_error_is_observable_without_raw_diagnostics(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    original_close = LocalProcessOwner.close_bounded

    class Source:
        def try_read(self, limit: int) -> bytes | None:
            return b""

    class Sink:
        def try_write(self, data: memoryview) -> int:
            return len(data)

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> None:
        original_close(owner, deadline)
        raise OSError("sensitive-close-canary")

    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", Source()), SinkOutput(Sink(), Sink()))
    code = "import tty,time; tty.setraw(0); time.sleep(.03)"
    result = run_terminal_relay_candidate(
        [sys.executable, "-c", code], io=io, deadline=Deadline.after(3), custody=custody
    )
    assert result.started and result.failure is Failure.OBSERVATION
    assert "sensitive-close-canary" not in repr(result)


def test_worker_control_precedes_caller_interruption_during_cleanup(
    endpoint: tuple[int, int], monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    import termios

    mode = termios.tcgetattr(endpoint[1])
    primary = KeyboardInterrupt("source-control")
    secondary = SystemExit("caller-control")
    closing, release_cleanup = Event(), Event()
    original_close = LocalProcessOwner.close_bounded

    class Source:
        def try_read(self, limit: int) -> bytes | None:
            raise primary

    class Sink:
        def try_write(self, data: memoryview) -> int:
            return len(data)

    def close(owner: LocalProcessOwner, deadline: ProcessDeadline) -> LocalProcessTerminal:
        closing.set()
        assert release_cleanup.wait(2)
        result = original_close(owner, deadline)
        assert result is not None
        return result

    monkeypatch.setattr(LocalProcessOwner, "close_bounded", close)
    io = CarrierIO(TerminalInput(endpoint[1], endpoint[1], "fixture", Source()), SinkOutput(Sink(), Sink()))
    code = "import tty,time; tty.setraw(0); time.sleep(30)"
    attempt = _Attempt([sys.executable, "-c", code], io, Deadline.after(3), custody=custody)
    original_wait = attempt._done.wait
    calls = 0

    def wait(timeout: float | None = None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert closing.wait(2)
            raise secondary
        release_cleanup.set()
        return original_wait(timeout)

    monkeypatch.setattr(attempt._done, "wait", wait)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            attempt.run()
        assert raised.value is primary and attempt._done.is_set()
        assert termios.tcgetattr(endpoint[1]) == mode
    finally:
        release_cleanup.set()
