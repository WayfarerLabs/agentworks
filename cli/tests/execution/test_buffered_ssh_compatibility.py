"""Shared byte modes refuse safely on the buffered SSH implementation."""

import pytest

from agentworks.execution.carrier import (
    CarrierIO,
    Deadline,
    Dispatch,
    Failure,
    LiveInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.ssh.client import SSHCarrier


class UnusedEndpoint:
    def try_read(self, limit: int) -> bytes | None:
        pytest.fail("Refused input was consumed")

    def try_write(self, data: memoryview) -> int | None:
        pytest.fail("Refused output was delivered")


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("mode", ["live_input", "sink", "live_sink"])
def test_real_extended_modes_refuse_before_connection_access(mode: str, sensitive: bool) -> None:
    endpoint = UnusedEndpoint()
    io = (
        CarrierIO(input=LiveInput(endpoint, sensitive=sensitive))
        if mode == "live_input"
        else CarrierIO(output=SinkOutput(endpoint, endpoint, require_live=mode == "live_sink"), sensitive=sensitive)
    )
    # No connection is installed: touching admission or dispatch fails this test.
    carrier = object.__new__(SSHCarrier)
    report = carrier.execute(PreparedInvocation(("/unused",)), io=io, deadline=Deadline.after(10))

    assert report.failure == (Failure.INPUT if mode == "live_input" else Failure.OUTPUT)
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.completion is None and report.local_status is None
    assert report.stdout.data == report.stderr.data == b""
    assert not report.stdout.complete and not report.stderr.complete
    retention = (
        Retention.SUPPRESSED if sensitive else Retention.CAPTURED if mode == "live_input" else Retention.DISCARDED
    )
    assert report.stdout.retention == report.stderr.retention == retention
