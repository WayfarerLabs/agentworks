"""Shared byte modes refuse safely on the buffered SSH implementation."""

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    CarrierIO,
    Deadline,
    LiveInput,
    PreparedInvocation,
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
    invocation = PreparedInvocation(("/unused",))
    with pytest.raises(ValidationError):
        carrier.validate(invocation, io=io)
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline.after(10))
