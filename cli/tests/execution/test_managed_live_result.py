"""Durable job completion cannot substitute for foreground pipe delivery."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from agentworks.execution._execution_operation import OwnedInlineOutcome
from agentworks.execution._inline_control import StreamEnd, StreamName, StreamRetention
from agentworks.execution._managed_job_request import ManagedLiveIO
from agentworks.execution._managed_live_exchange import ManagedLiveCandidate, ManagedLiveIssue, ManagedLiveObservation
from agentworks.execution._managed_live_result import compose_managed_live_result
from agentworks.execution._runtime_prerequisite import (
    HelperClosureExpectation,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
)
from agentworks.execution.carrier import Dispatch, ExitStatus, Failure, Retention
from agentworks.execution.models import JobRef
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExecutionOutput, ExecutionResult, ExitCode

LIVE = ManagedLiveIO("a" * 32, True, True)
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
SETTLED = OwnedInlineOutcome()


def _candidate() -> ManagedLiveCandidate:
    def end(stream: StreamName) -> StreamEnd:
        return StreamEnd(stream, 0, EMPTY_DIGEST, complete=True, truncated=False, retention=StreamRetention.DELIVERED)

    return ManagedLiveCandidate(
        Dispatch.SENT,
        ExitStatus(0),
        0,
        None,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3"),
        ManagedLiveObservation(True, end(StreamName.STDOUT), end(StreamName.STDERR), True, None),
        HelperClosureExpectation("b" * 32, ("/usr/bin/python3",), None, None),
    )


def _result(*, code: int = 0) -> ExecutionResult:
    discarded = ExecutionOutput(complete=True, retention=Retention.DISCARDED)
    return ExecutionResult(
        Dispatch.UNKNOWN,
        ApplicationState.COMPLETED,
        ExitCode(code),
        discarded,
        discarded,
        owned_cleanup_confirmed=True,
        job=JobRef("c" * 32),
    )


def _compose(
    *,
    candidate: ManagedLiveCandidate | None,
    custody: OwnedInlineOutcome = SETTLED,
    result: ExecutionResult | None = None,
    live_io: ManagedLiveIO = LIVE,
) -> ExecutionResult:
    return compose_managed_live_result(
        _result() if result is None else result,
        live_io=live_io,
        candidate=candidate,
        custody=custody,
    )


def test_success_requires_both_durable_completion_and_actual_presentation() -> None:
    durable = _result()
    result = _compose(candidate=_candidate(), result=durable)
    assert result.ok and result.status == durable.status and result.job == durable.job
    assert result.dispatch is durable.dispatch
    assert result.stdout.retention is result.stderr.retention is Retention.DELIVERED
    assert result.stdout.complete and result.stderr.complete
    assert not result.stdout.data and not result.stderr.data
    assert durable.stdout.retention is Retention.DISCARDED


@pytest.mark.parametrize("failure", [Failure.INPUT, Failure.OUTPUT])
def test_pipe_failure_survives_later_successful_wait(failure: Failure) -> None:
    result = _compose(candidate=replace(_candidate(), carrier_failure=failure))
    assert result.application_state is ApplicationState.COMPLETED and result.status == ExitCode(0)
    assert result.failure is ExecutionFailure(failure.value) and not result.ok


@pytest.mark.parametrize(
    "observation",
    [
        None,
        ManagedLiveObservation(False, None, None, False, ManagedLiveIssue.BINDING),
        ManagedLiveObservation(True, None, None, False, ManagedLiveIssue.STREAM),
        ManagedLiveObservation(True, None, None, False, ManagedLiveIssue.MISSING_TERMINAL),
    ],
)
def test_discarded_wait_cannot_repair_missing_presentation(observation: ManagedLiveObservation | None) -> None:
    result = _compose(candidate=replace(_candidate(), observation=observation))
    assert result.status == ExitCode(0) and result.owned_cleanup_confirmed
    assert result.failure is not None and not result.ok
    assert result.stdout.retention is result.stderr.retention is Retention.DELIVERED
    assert not result.stdout.complete and not result.stderr.complete


@pytest.mark.parametrize(
    "custody",
    [
        OwnedInlineOutcome(pending_remote_effects=True),
        OwnedInlineOutcome(coordination_uncertain=True),
        OwnedInlineOutcome(helper_requires_owner_retention=True),
    ],
)
def test_uncertain_relay_custody_prevents_aggregate_cleanup(custody: OwnedInlineOutcome) -> None:
    result = _compose(candidate=_candidate(), custody=custody)
    assert result.status == ExitCode(0) and result.failure is ExecutionFailure.OBSERVATION
    assert not result.owned_cleanup_confirmed and not result.stdout.complete and not result.ok


@pytest.mark.parametrize("state", [RuntimePrerequisiteState.MISSING, RuntimePrerequisiteState.SHIM])
def test_refused_relay_does_not_reclassify_the_acknowledged_application(state: RuntimePrerequisiteState) -> None:
    candidate = replace(
        _candidate(),
        dispatch=Dispatch.NOT_SENT,
        runtime_prerequisite=RuntimePrerequisiteObservation(state, None),
        observation=None,
    )
    result = _compose(candidate=candidate)
    assert result.application_state is ApplicationState.COMPLETED and result.status == ExitCode(0)
    assert result.failure is ExecutionFailure.PREPARATION and result.job == _result().job
    assert not result.stdout.complete and not result.ok


def test_no_relay_after_budget_expiry_preserves_job_and_requested_retention() -> None:
    result = _compose(candidate=None, custody=OwnedInlineOutcome(deadline_exceeded=True))
    assert result.deadline_exceeded and result.failure is ExecutionFailure.DEADLINE
    assert result.job == _result().job and result.status == ExitCode(0)
    assert result.stdout.retention is Retention.DELIVERED and not result.stdout.complete and not result.ok


@pytest.mark.parametrize("mode", [Retention.CAPTURED, Retention.SUPPRESSED, Retention.DISCARDED])
def test_input_only_relay_leaves_durable_output_unchanged(mode: Retention) -> None:
    output = ExecutionOutput(b"captured" if mode is Retention.CAPTURED else b"", complete=True, retention=mode)
    durable = replace(_result(), stdout=output, stderr=output)
    result = _compose(candidate=_candidate(), result=durable, live_io=ManagedLiveIO("a" * 32, True, False))
    assert result.ok and result.stdout is output and result.stderr is output


def test_relay_success_does_not_invent_application_completion_or_cleanup() -> None:
    durable = ExecutionResult(Dispatch.UNKNOWN, ApplicationState.UNKNOWN, failure=ExecutionFailure.OBSERVATION)
    result = _compose(candidate=_candidate(), result=durable)
    assert result.application_state is ApplicationState.UNKNOWN and result.status is None
    assert result.stdout.complete and result.stderr.complete
    assert result.failure is ExecutionFailure.OBSERVATION and not result.owned_cleanup_confirmed and not result.ok


def test_nonzero_application_status_is_preserved_after_successful_relay() -> None:
    result = _compose(candidate=_candidate(), result=_result(code=255))
    assert result.status == ExitCode(255) and result.failure is None and not result.ok
