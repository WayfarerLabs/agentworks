"""Invariants at the adapter-author boundary."""

from __future__ import annotations

import math

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._process import SinkWriteError, try_write_to_sink
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
    LiveInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
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


def test_live_endpoint_representations_are_hidden_and_sensitivity_is_promoted() -> None:
    class Endpoint:
        def __repr__(self) -> str:
            return "secret-endpoint-canary"

    source = Endpoint()
    stdout = Endpoint()
    stderr = Endpoint()
    io = CarrierIO(
        input=LiveInput(source, sensitive=True),  # type: ignore[arg-type]
        output=SinkOutput(stdout, stderr),  # type: ignore[arg-type]
    )
    assert io.sensitive
    for value in (io.input, io.output, io):
        assert "secret-endpoint-canary" not in repr(value)


@pytest.mark.parametrize("field", ["input", "output"])
def test_unknown_io_mode_is_refused(field: str) -> None:
    kwargs = {field: object()}
    with pytest.raises(ValidationError):
        CarrierIO(**kwargs)  # type: ignore[arg-type]


def test_delivered_output_cannot_retain_raw_bytes() -> None:
    with pytest.raises(ValidationError):
        CapturedOutput(b"private", retention=Retention.DELIVERED)


def test_sink_adapter_failure_has_no_payload_or_exception_chain() -> None:
    class BrokenSink:
        def try_write(self, data: memoryview) -> int | None:
            raise RuntimeError("secret-sink-canary")

    with pytest.raises(SinkWriteError) as failure:
        try_write_to_sink(BrokenSink(), memoryview(b"private"))
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert "secret" not in repr(failure.value)


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


@pytest.mark.parametrize("field", ["source", "environment", "arguments"])
def test_invalid_sensitive_text_does_not_retain_a_codec_exception(field: str) -> None:
    from agentworks.execution.models import Command, Script, Shell
    from agentworks.execution.preparation import prepare

    value = "synthetic-private-payload\ud800"
    with pytest.raises(ValidationError) as failure:
        if field == "source":
            prepare(Script(value, Shell.SH), sensitive=True)
        elif field == "environment":
            prepare(Command(("/bin/true",)), env={"PRIVATE": value}, sensitive=True)
        else:
            prepare(Command(("/bin/true", value)), sensitive=True)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert "synthetic-private-payload" not in repr(failure.value)
