"""Unsupported shared I/O modes must not become EOF or discarded output."""

from __future__ import annotations

import subprocess

import pytest

from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    Deadline,
    Discard,
    Dispatch,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Provenance,
    Retention,
)
from agentworks.execution.carriers.ssh.client import SSHCarrier


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize(
    "unsupported_field,output,failure,retention",
    [
        ("input", Capture(), Failure.INPUT, Retention.CAPTURED),
        ("input", Discard(), Failure.INPUT, Retention.DISCARDED),
        ("output", Capture(), Failure.OUTPUT, Retention.DISCARDED),
    ],
)
def test_unsupported_mode_refuses_before_connection_or_process(
    monkeypatch: pytest.MonkeyPatch,
    unsupported_field: str,
    output: Capture | Discard,
    failure: Failure,
    retention: Retention,
    sensitive: bool,
) -> None:
    io = CarrierIO(input=FiniteInput(b"secret-canary", sensitive=sensitive), output=output)
    # Model a shared API extension without importing types this carrier cannot use.
    object.__setattr__(io, unsupported_field, object())
    # Refusal must precede even reading the connection for admission or a version probe.
    carrier = object.__new__(SSHCarrier)

    def unexpected_process(*args, **kwargs):
        pytest.fail("Unsupported I/O mode started a process")

    monkeypatch.setattr(subprocess, "Popen", unexpected_process)
    report = carrier.execute(PreparedInvocation(("/unused/program",)), io=io, deadline=Deadline.after(10))

    assert report.failure == failure
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.completion is None
    assert report.local_status is None
    assert report.stdout.data == report.stderr.data == b""
    assert not report.stdout.complete and not report.stderr.complete
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    expected_retention = Retention.SUPPRESSED if sensitive else retention
    assert report.stdout.retention == report.stderr.retention == expected_retention
    assert "secret-canary" not in repr(report)
