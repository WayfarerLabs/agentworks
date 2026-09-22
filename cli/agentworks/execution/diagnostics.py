"""Safe contextual diagnostics for checked execution results.

Core composes these values from its logical target and operation evidence.
Carrier, account, command, payload, and output facts do not belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ExecutionDiagnostic:
    """Safe core-composed context for checking one execution result.

    ``entity_kind`` and ``entity_name`` are a core-supplied logical target
    identity. They are not carrier, account, command, path, or provider
    values. Callers with stronger private evidence may choose a more precise
    phase than :meth:`from_result`; facts not preserved by ``ExecutionResult``
    use ``UNKNOWN`` rather than being reconstructed from carrier outcomes.
    """

    result: ExecutionResult
    entity_kind: str
    entity_name: str
    phase: ExecutionPhase
    reason: ExecutionFailureReason

    def __post_init__(self) -> None:
        """Validate values crossing the diagnostic composition boundary."""
        if type(self.result) is not ExecutionResult:
            raise ValidationError("Execution diagnostics require an exact execution result")
        validate_logical_entity_value(self.entity_kind, "kind", subject="Execution diagnostics")
        validate_logical_entity_value(self.entity_name, "name", subject="Execution diagnostics")
        if type(self.phase) is not ExecutionPhase:
            raise ValidationError("Execution diagnostics require a supported phase")
        if type(self.reason) is not ExecutionFailureReason:
            raise ValidationError("Execution diagnostics require a supported failure reason")
        expected_phase, expected_reason = _result_diagnostic(self.result)
        if self.result.ok or self.reason is not expected_reason:
            raise ValidationError("Execution diagnostics require an unsuccessful matching result")
        if expected_phase is not ExecutionPhase.UNKNOWN and self.phase is not expected_phase:
            raise ValidationError("Execution diagnostics cannot replace established result phase")

    @classmethod
    def from_result(
        cls,
        result: ExecutionResult,
        *,
        entity_kind: str,
        entity_name: str,
        phase: ExecutionPhase | None = None,
    ) -> ExecutionDiagnostic:
        """Project result facts without recovering discarded private evidence."""
        if type(result) is not ExecutionResult:
            raise ValidationError("Execution diagnostics require an exact execution result")
        default_phase, reason = _result_diagnostic(result)
        return cls(result, entity_kind, entity_name, default_phase if phase is None else phase, reason)

    @property
    def details(self) -> ErrorDetails:
        """Return the standard closed diagnostic facts for this context."""
        return ErrorDetails(self.phase, self.reason)

    def check(self) -> ExecutionResult:
        """Raise the checked error carrying this unsuccessful result."""
        raise CheckedExecutionError(
            self.result,
            entity_kind=self.entity_kind,
            entity_name=self.entity_name,
            details=self.details,
        ) from None


def check_execution_result(
    result: ExecutionResult,
    *,
    entity_kind: str,
    entity_name: str,
    phase: ExecutionPhase | None = None,
) -> ExecutionResult:
    """Check one result with core-composed safe target context."""
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
    return ExecutionDiagnostic.from_result(
        result,
        entity_kind=entity_kind,
        entity_name=entity_name,
        phase=phase,
    ).check()


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
