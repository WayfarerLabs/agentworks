"""Buffered QGA observations can feed trusted collectors without claiming live I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    CarrierIO,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    LiveInput,
    Retention,
    SinkOutput,
)
from tests.execution.test_proxmox import Clock, execute


@dataclass
class Sink:
    data: bytearray = field(default_factory=bytearray)
    limit: int = 3
    stalled: bool = False
    calls: int = 0

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        if self.stalled:
            return None
        count = min(self.limit, len(data))
        self.data.extend(data[:count])
        return count


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    request = MagicMock(
        side_effect=[
            {"pid": 42},
            {"exited": 1, "exitcode": 255, "out-data": "secret-canary\r\n\0", "err-data": "diagnostic\n"},
        ]
    )
    monkeypatch.setattr("agentworks.execution.carriers.proxmox._ProxmoxWire.request", request)
    return request


@pytest.mark.parametrize("sensitive", [False, True])
def test_short_sink_writes_deliver_bytes_without_raw_retention(wire: MagicMock, sensitive: bool) -> None:
    stdout, stderr = Sink(), Sink()
    report = execute(CarrierIO(output=SinkOutput(stdout, stderr), sensitive=sensitive))
    assert stdout.data == b"secret-canary\r\n\0"
    assert stderr.data == b"diagnostic\n"
    assert report.completion == ExitStatus(code=255)
    assert report.failure is None
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.data == report.stderr.data == b""
    assert report.stdout.retention == report.stderr.retention == Retention.DELIVERED
    assert "secret-canary" not in repr(report)
    assert wire.call_count == 2


def test_stalled_sink_does_not_starve_other_stream_or_repoll(wire: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr("agentworks.execution.carriers.proxmox.time.monotonic", clock.monotonic)
    monkeypatch.setattr("agentworks.execution.carriers.proxmox.time.sleep", clock.sleep)
    stdout, stderr = Sink(stalled=True), Sink()
    report = execute(CarrierIO(output=SinkOutput(stdout, stderr)), Deadline(clock.now + 0.05))
    assert report.failure == Failure.DEADLINE
    assert report.completion == ExitStatus(code=255)
    assert not stdout.data and stderr.data == b"diagnostic\n"
    assert not report.stdout.complete and report.stderr.complete
    assert wire.call_count == 2
    calls = stdout.calls, stderr.calls
    assert calls[0] > 0 and calls[1] > 0


@pytest.mark.parametrize("invalid", [0, -1, True, 100_000])
def test_invalid_sink_ack_preserves_completion(wire: MagicMock, invalid: int) -> None:
    class InvalidSink:
        def try_write(self, data: memoryview) -> int:
            return invalid

    report = execute(CarrierIO(output=SinkOutput(InvalidSink(), Sink())))
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=255)
    assert report.failure == Failure.OUTPUT
    assert not report.stdout.complete
    assert report.stdout.data == report.stderr.data == b""
    assert wire.call_count == 2


def test_sink_exception_payload_does_not_enter_report(wire: MagicMock) -> None:
    class BrokenSink:
        def try_write(self, data: memoryview) -> int:
            raise OSError("sink-secret-canary")

    report = execute(CarrierIO(output=SinkOutput(BrokenSink(), Sink()), sensitive=True))
    assert report.failure == Failure.OUTPUT
    assert report.completion == ExitStatus(code=255)
    assert "sink-secret-canary" not in repr(report)


def test_provider_truncation_delivers_available_prefix_honestly(wire: MagicMock) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 0, "out-data": "partial", "out-truncated": 1}]
    stdout, stderr = Sink(), Sink()
    report = execute(CarrierIO(output=SinkOutput(stdout, stderr)))
    assert stdout.data == b"partial"
    assert report.failure == Failure.OUTPUT_LIMIT
    assert report.completion == ExitStatus(code=0)
    assert not report.stdout.complete and report.stderr.complete


@pytest.mark.parametrize("invalid", [{"out-data": "\xff"}, {"out-truncated": "invalid"}])
def test_invalid_provider_stream_never_reaches_sink(wire: MagicMock, invalid: dict[str, str]) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 0, "out-data": "payload", **invalid}]
    stdout, stderr = Sink(), Sink()
    report = execute(CarrierIO(output=SinkOutput(stdout, stderr)))
    assert stdout.calls == 0
    assert report.failure == Failure.INVALID_RESPONSE
    assert report.completion == ExitStatus(code=0)
    assert not report.stdout.complete


@pytest.mark.parametrize("live_input", [False, True])
def test_live_requests_refuse_before_source_or_provider_use(wire: MagicMock, live_input: bool) -> None:
    class Source:
        def try_read(self, limit: int) -> bytes | None:
            raise AssertionError("unsupported source was touched")

    request = (
        CarrierIO(input=LiveInput(Source()))
        if live_input
        else CarrierIO(output=SinkOutput(Sink(), Sink(), require_live=True))
    )
    with pytest.raises(ValidationError):
        execute(request)
    wire.assert_not_called()


def test_sink_interruption_propagates_without_replay(wire: MagicMock) -> None:
    class InterruptedSink:
        def try_write(self, data: memoryview) -> int:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute(CarrierIO(output=SinkOutput(InterruptedSink(), Sink())))
    assert wire.call_count == 2
