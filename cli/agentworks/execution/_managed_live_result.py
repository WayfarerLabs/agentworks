"""Combine durable managed results with this call's actual pipe presentation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ._managed_live_exchange import ManagedLiveIssue
from ._runtime_prerequisite import RuntimePrerequisiteState
from .carrier import Dispatch, Failure, Retention
from .result import ExecutionFailure, ExecutionOutput, ExecutionResult

if TYPE_CHECKING:
    from ._execution_operation import OwnedInlineOutcome
    from ._managed_job_request import ManagedLiveIO
    from ._managed_live_exchange import ManagedLiveCandidate


def compose_managed_live_result(
    result: ExecutionResult,
    *,
    live_io: ManagedLiveIO,
    candidate: ManagedLiveCandidate | None,
    custody: OwnedInlineOutcome,
) -> ExecutionResult:
    """Preserve application facts without repairing a failed live attempt.

    The durable result alone can establish application status and workload
    closure. The relay alone can establish presentation. In particular, an
    already acknowledged job cannot become NOT_STARTED because its relay was
    refused. Discarded durable output alone cannot establish delivery.
    """
    settled = not (custody.pending_remote_effects or custody.coordination_uncertain or custody.requires_owner_retention)
    observation = None if candidate is None else candidate.observation
    complete = (
        settled
        and candidate is not None
        and candidate.dispatch is Dispatch.SENT
        and candidate.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        and observation is not None
        and observation.bound
        and observation.trusted_terminal
        and observation.error is None
    )
    stdout, stderr = result.stdout, result.stderr
    if live_io.output:
        stdout = ExecutionOutput(
            retention=Retention.DELIVERED,
            complete=complete and observation is not None and observation.stdout is not None,
        )
        stderr = ExecutionOutput(
            retention=Retention.DELIVERED,
            complete=complete and observation is not None and observation.stderr is not None,
        )
    expired = (
        result.deadline_exceeded
        or custody.deadline_exceeded
        or (candidate is not None and candidate.carrier_failure is Failure.DEADLINE)
    )
    return replace(
        result,
        stdout=stdout,
        stderr=stderr,
        failure=_relay_failure(candidate, custody, settled=settled) or result.failure,
        deadline_exceeded=expired,
        owned_cleanup_confirmed=result.owned_cleanup_confirmed and settled,
    )


def _relay_failure(
    candidate: ManagedLiveCandidate | None,
    custody: OwnedInlineOutcome,
    *,
    settled: bool,
) -> ExecutionFailure | None:
    if candidate is None:
        return ExecutionFailure.DEADLINE if custody.deadline_exceeded else ExecutionFailure.OBSERVATION
    failure = candidate.carrier_failure
    if failure in {Failure.INPUT, Failure.OUTPUT}:
        return ExecutionFailure(failure.value)
    runtime = candidate.runtime_prerequisite.state
    if runtime not in {RuntimePrerequisiteState.READY, RuntimePrerequisiteState.UNKNOWN}:
        return ExecutionFailure.PREPARATION
    if failure is not None:
        return {
            Failure.DEADLINE: ExecutionFailure.DEADLINE,
            Failure.DISPATCH: ExecutionFailure.DELIVERY,
            Failure.OBSERVATION: ExecutionFailure.OBSERVATION,
            Failure.INVALID_RESPONSE: ExecutionFailure.OBSERVATION,
            Failure.OUTPUT_LIMIT: ExecutionFailure.OUTPUT_LIMIT,
        }[failure]
    if custody.deadline_exceeded:
        return ExecutionFailure.DEADLINE
    if candidate.dispatch is Dispatch.NOT_SENT:
        return ExecutionFailure.DELIVERY
    if candidate.dispatch is not Dispatch.SENT:
        return ExecutionFailure.OBSERVATION
    if not settled or runtime is not RuntimePrerequisiteState.READY:
        return ExecutionFailure.OBSERVATION
    observation = candidate.observation
    if observation is None:
        return ExecutionFailure.OBSERVATION
    if observation.error is not None:
        return (
            ExecutionFailure.OBSERVATION
            if observation.error in {ManagedLiveIssue.CARRIER, ManagedLiveIssue.MISSING_TERMINAL}
            else ExecutionFailure.PROTOCOL
        )
    if not observation.bound or not observation.trusted_terminal:
        return ExecutionFailure.OBSERVATION
    return None
