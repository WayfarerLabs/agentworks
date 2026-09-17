"""Invariants at the adapter-author boundary."""

from __future__ import annotations

import math

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    EndOfInput,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
)


def test_deadline_spends_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    deadline = Deadline.after(5)
    assert deadline.remaining() == 5
    now[0] = 13
    assert deadline.remaining() == 2
    now[0] = 20
    assert deadline.expired
    assert deadline.remaining() == 0
    assert Deadline.after(None).remaining() is None
    assert not Deadline.after(None).expired


@pytest.mark.parametrize("seconds", [-1, math.inf, -math.inf, math.nan])
def test_invalid_deadline_is_refused(seconds: float) -> None:
    with pytest.raises(ValidationError):
        Deadline.after(seconds)


@pytest.mark.parametrize("expiry", [math.inf, -math.inf, math.nan])
def test_absolute_deadline_cannot_disguise_unboundedness(expiry: float) -> None:
    with pytest.raises(ValidationError):
        Deadline(expiry)


@pytest.mark.parametrize("argv", [(), ("",), ("a\0b",), ("tool", "a\0b"), ["tool"], (42,)])
def test_invalid_literal_invocation_is_refused(argv: object) -> None:
    with pytest.raises(ValidationError):
        PreparedInvocation(argv)  # type: ignore[arg-type]


def test_payloads_are_not_diagnostic_representations() -> None:
    secret = "proof-secret-sentinel"
    invocation = PreparedInvocation(("tool", secret))
    io = CarrierIO(input=FiniteInput(secret.encode(), sensitive=True))
    output = CapturedOutput(secret.encode())
    report = CarrierReport(Dispatch.SENT, stdout=output)
    for value in (invocation, io, output, report):
        assert secret not in repr(value)


def test_input_sensitivity_cannot_be_downgraded() -> None:
    io = CarrierIO(input=FiniteInput(b"private", sensitive=True), sensitive=False)
    assert io.sensitive
    assert CarrierIO(sensitive=True).sensitive
    assert isinstance(CarrierIO().input, EndOfInput)


@pytest.mark.parametrize("bound", [-1, 1.5, True])
def test_invalid_capture_bound_is_refused(bound: object) -> None:
    with pytest.raises(ValidationError):
        Capture(bound)  # type: ignore[arg-type]


@pytest.mark.parametrize("kwargs", [{}, {"code": 0, "signal": 9}, {"code": -1}, {"signal": 0}, {"code": True}])
def test_completion_has_one_valid_observation(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ExitStatus(**kwargs)


@pytest.mark.parametrize("dispatch", [Dispatch.UNKNOWN, Dispatch.NOT_SENT])
def test_completion_cannot_coexist_with_unsent_or_uncertain_dispatch(dispatch: Dispatch) -> None:
    with pytest.raises(ValidationError):
        CarrierReport(dispatch, completion=ExitStatus(code=0))


@pytest.mark.parametrize("retention", [Retention.SUPPRESSED, Retention.DISCARDED])
def test_unretained_output_cannot_keep_bytes(retention: Retention) -> None:
    with pytest.raises(ValidationError):
        CapturedOutput(b"private", retention=retention)


def test_local_status_does_not_manufacture_completion() -> None:
    report = CarrierReport(Dispatch.UNKNOWN, local_status=255)
    assert report.completion is None
