"""Safe contextual diagnostics for checked execution results.

Core composes these values from its logical target and operation evidence.
Carrier, account, command, payload, and output facts do not belong here.
"""

from __future__ import annotations

from enum import StrEnum

from agentworks.errors import ErrorDetails, ValidationError
from agentworks.execution._diagnostic_values import validate_logical_entity_value
from agentworks.execution.result import ApplicationState, CheckedExecutionError, ExecutionFailure, ExecutionResult


class ExecutionPhase(StrEnum):
    """Closed execution phases safe to expose with a checked failure."""

    PREPARATION = "preparation"
    DELIVERY = "delivery"
    APPLICATION = "application"
    OBSERVATION = "observation"
    CLEANUP = "cleanup"
    UNKNOWN = "unknown"


class ExecutionFailureReason(StrEnum):
    """Closed value-free reasons safe to expose with a checked failure."""

    APPLICATION_STATUS = "application_status"
    PREPARATION = "preparation"
    DELIVERY = "delivery"
    DEADLINE = "deadline"
    OBSERVATION = "observation"
    PROTOCOL = "protocol"
    INPUT = "input"
    OUTPUT = "output"
    OUTPUT_LIMIT = "output_limit"
    CLEANUP = "cleanup"
    INCOMPLETE = "incomplete"


def check_execution_result(
    result: ExecutionResult,
    *,
    entity_kind: str,
    entity_name: str,
    phase: ExecutionPhase | None = None,
) -> ExecutionResult:
    """Check one result with safe core target context and optional phase refinement.

    A supplied phase is private evidence that may refine only a result whose
    public phase is unknown. This keeps result-derived reasons authoritative.
    """
    if type(result) is not ExecutionResult:
        raise ValidationError("Execution diagnostics require an exact execution result")
    validate_logical_entity_value(entity_kind, "kind", subject="Execution diagnostics")
    validate_logical_entity_value(entity_name, "name", subject="Execution diagnostics")
    if phase is not None and type(phase) is not ExecutionPhase:
        raise ValidationError("Execution diagnostics require a supported phase")
    if result.ok:
        if phase is not None:
            raise ValidationError("Successful execution does not have a diagnostic phase")
        return result
    result_phase, reason = _result_diagnostic(result)
    if phase is not None:
        if result_phase is not ExecutionPhase.UNKNOWN or phase is ExecutionPhase.UNKNOWN:
            raise ValidationError("Execution diagnostics require a stronger unknown-phase refinement")
        result_phase = phase
    raise CheckedExecutionError(
        result,
        entity_kind=entity_kind,
        entity_name=entity_name,
        details=ErrorDetails(result_phase, reason),
    ) from None


def _result_diagnostic(result: ExecutionResult) -> tuple[ExecutionPhase, ExecutionFailureReason]:
    """Select only phase precision established by public result facts."""
    if result.failure is not None:
        return _failure_diagnostic(result.failure)
    if result.deadline_exceeded:
        return ExecutionPhase.UNKNOWN, ExecutionFailureReason.DEADLINE
    if (
        result.application_state is ApplicationState.COMPLETED
        and result.status is not None
        and result.status.value != 0
    ):
        return ExecutionPhase.APPLICATION, ExecutionFailureReason.APPLICATION_STATUS
    return ExecutionPhase.UNKNOWN, ExecutionFailureReason.INCOMPLETE


def _failure_diagnostic(failure: ExecutionFailure) -> tuple[ExecutionPhase, ExecutionFailureReason]:
    """Keep only phase facts intrinsic to the public failure category."""
    return {
        ExecutionFailure.PREPARATION: (ExecutionPhase.PREPARATION, ExecutionFailureReason.PREPARATION),
        ExecutionFailure.DELIVERY: (ExecutionPhase.DELIVERY, ExecutionFailureReason.DELIVERY),
        ExecutionFailure.DEADLINE: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.DEADLINE),
        ExecutionFailure.OBSERVATION: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.OBSERVATION),
        ExecutionFailure.PROTOCOL: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.PROTOCOL),
        ExecutionFailure.INPUT: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.INPUT),
        ExecutionFailure.OUTPUT: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.OUTPUT),
        ExecutionFailure.OUTPUT_LIMIT: (ExecutionPhase.UNKNOWN, ExecutionFailureReason.OUTPUT_LIMIT),
        ExecutionFailure.CLEANUP: (ExecutionPhase.CLEANUP, ExecutionFailureReason.CLEANUP),
    }[failure]
