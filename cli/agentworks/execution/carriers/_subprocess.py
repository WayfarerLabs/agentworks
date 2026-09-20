"""Carrier adapter for the stdlib-only owned-process core.

Process results establish neither guest dispatch nor target termination. The
core's private owner closes the proved CPython/Linux process-construction
interruption path. Native Windows and macOS remain unproved, and a concurrent
external reaper can still make exact local status and cleanup uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._process import Deadline as _Deadline
from agentworks.execution._process import (
    ProcessInput,
    ProcessOutput,
    StreamResult,
    run_owned_process,
)
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    Failure,
    FiniteInput,
    LiveInput,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.carrier import CarrierIO, Deadline


@dataclass(frozen=True)
class ProcessResult:
    started: bool
    local_status: int | None
    # Only a status observed before local termination can establish completion.
    exit_status: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    failure: Failure | None


def output_retention(io: CarrierIO) -> Retention:
    """Use one retention policy for pre-dispatch and process evidence."""
    return (
        Retention.DELIVERED
        if isinstance(io.output, SinkOutput)
        else Retention.SUPPRESSED
        if io.sensitive
        else Retention.CAPTURED
        if isinstance(io.output, Capture)
        else Retention.DISCARDED
    )


def _carrier_input(io: CarrierIO) -> ProcessInput:
    if isinstance(io.input, FiniteInput):
        return ProcessInput(data=io.input.data)
    if isinstance(io.input, LiveInput):
        return ProcessInput(source=io.input.source)
    return ProcessInput()


def _carrier_output(io: CarrierIO, retention: Retention) -> ProcessOutput:
    if retention == Retention.CAPTURED:
        assert isinstance(io.output, Capture)
        return ProcessOutput(capture_limit=io.output.max_bytes)
    if isinstance(io.output, SinkOutput):
        return ProcessOutput(stdout_sink=io.output.stdout, stderr_sink=io.output.stderr)
    return ProcessOutput()


def _captured_output(result: StreamResult, retention: Retention) -> CapturedOutput:
    return CapturedOutput(result.data, result.complete, retention=retention)


def run_process(
    argv: list[str],
    *,
    io: CarrierIO,
    deadline: Deadline,
    env: Mapping[str, str] | None = None,
    live_stdio: bool = False,
) -> ProcessResult:
    """Map carrier policy around one stdlib-only owned-process attempt."""
    requires_live = isinstance(io.input, LiveInput) or (isinstance(io.output, SinkOutput) and io.output.require_live)
    if requires_live and not live_stdio:
        raise ValidationError("Live carrier I/O is unavailable on this channel")

    retention = output_retention(io)
    result = run_owned_process(
        argv,
        input=_carrier_input(io),
        output=_carrier_output(io, retention),
        deadline=_Deadline(deadline.expires_at),
        env=env,
    )
    return ProcessResult(
        started=result.started,
        local_status=result.local_status,
        exit_status=result.exit_status,
        stdout=_captured_output(result.stdout, retention),
        stderr=_captured_output(result.stderr, retention),
        failure=Failure(result.failure.value) if result.failure is not None else None,
    )
