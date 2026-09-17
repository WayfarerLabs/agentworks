"""Drive shared vectors through a local process oracle, not an SSH substitute."""

from __future__ import annotations

import subprocess
import sys

import pytest

from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Provenance,
    Retention,
)
from tests.execution.conformance import check_buffered_contract


class _LocalOracle:
    """Finite, small synthetic fixtures only; not a production local carrier."""

    features = ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        result = subprocess.run(
            invocation.argv,
            input=io.input.data if isinstance(io.input, FiniteInput) else b"",
            capture_output=True,
            timeout=deadline.remaining(),
        )
        retention = Retention.SUPPRESSED if io.sensitive else Retention.CAPTURED
        limit = io.output.max_bytes if isinstance(io.output, Capture) else 0
        return CarrierReport(
            Dispatch.SENT,
            completion=ExitStatus(code=result.returncode)
            if result.returncode >= 0
            else ExitStatus(signal=-result.returncode),
            local_status=result.returncode,
            stdout=CapturedOutput(
                b"" if io.sensitive else result.stdout[:limit],
                len(result.stdout) <= limit,
                Provenance.CARRIER_STDOUT,
                retention,
            ),
            stderr=CapturedOutput(
                b"" if io.sensitive else result.stderr[:limit],
                len(result.stderr) <= limit,
                Provenance.MIXED_STDERR,
                retention,
            ),
        )


@pytest.mark.skipif(sys.platform != "linux", reason="The first bootstrap proof targets Linux userspace")
def test_shared_buffered_cases_against_local_processes() -> None:
    observations = check_buffered_contract(_LocalOracle())
    assert len(observations) == 8
    assert all(row.failure is None for row in observations)
    assert observations[-1].suppressed


def test_harness_rejects_success_without_guest_stream_evidence() -> None:
    class EmptySuccess:
        features = ChannelFeatures()

        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            return CarrierReport(Dispatch.SENT, completion=ExitStatus(code=0))

    with pytest.raises(AssertionError):
        check_buffered_contract(EmptySuccess())
