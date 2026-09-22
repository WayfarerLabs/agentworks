"""Reduce owned inline evidence into one safe public execution result."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution._inline_control import FailureCode, FailurePhase, WaitKind
from agentworks.execution._inline_observer import ObservationError, StreamObservation
from agentworks.execution._inline_request import OutputMode
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import Dispatch, Failure, Retention
from agentworks.execution.result import (
    ApplicationState,
    ExecutionFailure,
    ExecutionOutput,
    ExecutionResult,
    ExitCode,
)

if TYPE_CHECKING:
    from agentworks.execution._execution_operation import OwnedInlineOutcome
    from agentworks.execution._inline import InlineCandidateResult
    from agentworks.execution._inline_observer import InlineObservation


def reduce_owned_inline_result(outcome: OwnedInlineOutcome) -> ExecutionResult:
    """Return safe public facts from one settled or retained inline attempt.

    The private candidate is the sole source of helper evidence. Carrier
    completion is intentionally not consulted for application progress.
    """
    candidate = outcome.candidate
    if candidate is None:
        return ExecutionResult(
            dispatch=Dispatch.UNKNOWN,
            application_state=ApplicationState.UNKNOWN,
            failure=ExecutionFailure.OBSERVATION,
            deadline_exceeded=outcome.deadline_exceeded,
        )

    observation = candidate.observation
    settled = not (outcome.pending_remote_effects or outcome.coordination_uncertain or outcome.requires_owner_retention)
    completed = _has_retrospective_normal_completion(candidate, settled)
    state = _application_state(candidate, completed)
    stdout, stderr = _outputs(candidate, completed)
    failure = _failure(candidate, completed)
    cleanup_confirmed = _cleanup_confirmed(outcome, candidate, completed)
    return ExecutionResult(
        dispatch=candidate.dispatch,
        application_state=state,
        status=_status(observation, completed),
        stdout=stdout,
        stderr=stderr,
        failure=failure,
        owned_cleanup_confirmed=cleanup_confirmed,
        deadline_exceeded=outcome.deadline_exceeded,
    )


def _status(observation: InlineObservation | None, completed: bool) -> ExitCode | None:
    if not completed or observation is None or observation.wait is None:
        return None
    assert type(observation.wait.value) is int
    return ExitCode(observation.wait.value)


def _has_retrospective_normal_completion(candidate: InlineCandidateResult, settled: bool) -> bool:
    observation = candidate.observation
    return (
        settled
        and candidate.dispatch is not Dispatch.NOT_SENT
        and candidate.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        and observation is not None
        and observation.trusted_terminal
        and observation.wait is not None
        and observation.wait.kind is WaitKind.EXIT
        and type(observation.wait.value) is int
        and 0 <= observation.wait.value <= 255
    )


def _application_state(candidate: InlineCandidateResult, completed: bool) -> ApplicationState:
    if completed:
        return ApplicationState.COMPLETED
    if _runtime_refused(candidate):
        return ApplicationState.NOT_STARTED
    observation = candidate.observation
    if (
        observation is not None
        and observation.trusted_terminal
        and observation.failure is not None
        and observation.failure.phase
        in {FailurePhase.REQUEST, FailurePhase.IDENTITY, FailurePhase.PREPARE, FailurePhase.LAUNCH}
    ):
        return ApplicationState.NOT_STARTED
    return ApplicationState.UNKNOWN


def _outputs(candidate: InlineCandidateResult, completed: bool) -> tuple[ExecutionOutput, ExecutionOutput]:
    if not completed or candidate.observation is None:
        empty = _empty_output(candidate.requested_output)
        return empty, empty
    return _output(candidate.observation.stdout), _output(candidate.observation.stderr)


def _empty_output(mode: OutputMode) -> ExecutionOutput:
    retention = {
        OutputMode.CAPTURE: Retention.CAPTURED,
        OutputMode.DISCARD: Retention.DISCARDED,
        OutputMode.SUPPRESS: Retention.SUPPRESSED,
    }[mode]
    return ExecutionOutput(retention=retention)


def _output(observation: StreamObservation | None) -> ExecutionOutput:
    if observation is None:
        return ExecutionOutput()
    retention = Retention(observation.retention.value)
    return ExecutionOutput(observation.data, complete=observation.complete, retention=retention)


def _failure(candidate: InlineCandidateResult, completed: bool) -> ExecutionFailure | None:
    observation = candidate.observation
    if _runtime_refused(candidate):
        return ExecutionFailure.PREPARATION
    if candidate.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN:
        if candidate.carrier_failure is not None:
            return _carrier_failure(candidate.carrier_failure)
        return ExecutionFailure.OBSERVATION
    if observation is not None and observation.error is not None:
        if observation.error is ObservationError.CARRIER:
            return ExecutionFailure.OBSERVATION
        return ExecutionFailure.PROTOCOL
    if observation is not None and observation.failure is not None:
        return _helper_failure(observation.failure.phase, observation.failure.code)
    if completed and observation is not None:
        streams = (observation.stdout, observation.stderr)
        if any(stream is not None and stream.truncated for stream in streams):
            return ExecutionFailure.OUTPUT_LIMIT
        if any(stream is None or not stream.complete for stream in streams):
            return ExecutionFailure.OUTPUT
    if candidate.carrier_failure is not None:
        return _carrier_failure(candidate.carrier_failure)
    return None


def _runtime_refused(candidate: InlineCandidateResult) -> bool:
    return candidate.runtime_prerequisite.state in {
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    }


def _helper_failure(phase: FailurePhase, code: FailureCode) -> ExecutionFailure:
    if phase in {FailurePhase.REQUEST, FailurePhase.IDENTITY, FailurePhase.PREPARE}:
        return ExecutionFailure.PREPARATION
    if phase is FailurePhase.LAUNCH:
        return ExecutionFailure.DELIVERY
    if phase is FailurePhase.CLEANUP:
        return ExecutionFailure.CLEANUP
    return {
        FailureCode.INPUT: ExecutionFailure.INPUT,
        FailureCode.OUTPUT: ExecutionFailure.OUTPUT,
        FailureCode.OBSERVATION: ExecutionFailure.OBSERVATION,
    }.get(code, ExecutionFailure.OBSERVATION)


def _carrier_failure(failure: Failure) -> ExecutionFailure:
    return {
        Failure.DEADLINE: ExecutionFailure.DEADLINE,
        Failure.DISPATCH: ExecutionFailure.DELIVERY,
        Failure.OBSERVATION: ExecutionFailure.OBSERVATION,
        Failure.INVALID_RESPONSE: ExecutionFailure.OBSERVATION,
        Failure.INPUT: ExecutionFailure.INPUT,
        Failure.OUTPUT: ExecutionFailure.OUTPUT,
        Failure.OUTPUT_LIMIT: ExecutionFailure.OUTPUT_LIMIT,
    }[failure]


def _cleanup_confirmed(
    outcome: OwnedInlineOutcome,
    candidate: InlineCandidateResult,
    completed: bool,
) -> bool:
    observation = candidate.observation
    return (
        completed
        and not outcome.pending_remote_effects
        and not outcome.coordination_uncertain
        and not outcome.requires_owner_retention
        and observation is not None
        and observation.trusted_terminal
        and observation.error is None
        and (observation.failure is None or observation.failure.phase is not FailurePhase.CLEANUP)
    )
