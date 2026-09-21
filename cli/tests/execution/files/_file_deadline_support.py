"""Deterministic admitted-helper deadline support for file compositions."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from agentworks.execution import _process
from agentworks.execution.carrier import CarrierIO, ChannelFeatures, SinkOutput
from tests.execution.files._file_stage_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_ready_record

if TYPE_CHECKING:
    import pytest

    from agentworks.execution.carrier import ByteSink, CarrierReport, Deadline, PreparedInvocation


class _AdmissionDeadlineSink:
    def __init__(
        self,
        downstream: ByteSink,
        expected: bytes,
        admitted: threading.Event,
        deadline: Deadline,
    ) -> None:
        self._downstream = downstream
        self._expected = expected
        self._admitted = admitted
        self._deadline = deadline
        self._prefix = bytearray()

    def try_write(self, data: memoryview) -> int | None:
        written = self._downstream.try_write(data)
        if type(written) is int and written > 0 and not self._admitted.is_set():
            remaining = len(self._expected) - len(self._prefix)
            self._prefix.extend(data[: min(written, remaining)])
            if bytes(self._prefix) == self._expected:
                object.__setattr__(self._deadline, "expires_at", 0.0)
                self._admitted.set()
        return written


class AdmittedTimeoutCarrier:
    """Expire a real local process only after its runtime-ready handshake."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, startup_delay: float = 0.0) -> None:
        self._carrier = LocalCarrier()
        self._admitted = threading.Event()
        self._startup_delay = startup_delay
        original_remaining = _process.Deadline.remaining

        def controlled_remaining(deadline: _process.Deadline) -> float | None:
            if self._admitted.is_set():
                return 0.0
            return original_remaining(deadline)

        def controlled_expired(deadline: _process.Deadline) -> bool:
            remaining = controlled_remaining(deadline)
            return remaining is not None and remaining <= 0

        monkeypatch.setattr(_process.Deadline, "remaining", controlled_remaining)
        monkeypatch.setattr(_process.Deadline, "expired", property(controlled_expired))

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    @property
    def admitted(self) -> bool:
        return self._admitted.is_set()

    @property
    def calls(self) -> int:
        return self._carrier.calls

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport:
        if self._startup_delay:
            time.sleep(self._startup_delay)
        assert isinstance(io.output, SinkOutput)
        wrapped = CarrierIO(
            input=io.input,
            output=SinkOutput(
                _AdmissionDeadlineSink(
                    io.output.stdout,
                    runtime_ready_record(invocation),
                    self._admitted,
                    deadline,
                ),
                io.output.stderr,
                require_live=io.output.require_live,
            ),
            sensitive=io.sensitive,
        )
        return self._carrier.execute(invocation, io=wrapped, deadline=deadline)
