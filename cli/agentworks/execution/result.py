"""Safe public values for one execution attempt's observed outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from agentworks.errors import ErrorDetails, ExternalError, ValidationError
from agentworks.execution.carrier import Dispatch, Retention


class ApplicationState(StrEnum):
    """How precisely application progress was established."""

    NOT_STARTED = "not_started"
    STARTED = "started"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


def _validate_status_value(value: object, *, positive: bool) -> None:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= 255:
        raise ValidationError("Application status must be an integer in its supported range")


@dataclass(frozen=True)
class WaitCode:
    """A shell-compatible wait code without invented exit or signal precision."""

    value: int

    def __post_init__(self) -> None:
        _validate_status_value(self.value, positive=False)


@dataclass(frozen=True)
class ExitCode:
    """An exact normal application exit code."""

    value: int

    def __post_init__(self) -> None:
        _validate_status_value(self.value, positive=False)


@dataclass(frozen=True)
class Signal:
    """An exact application-terminating signal."""

    value: int

    def __post_init__(self) -> None:
        _validate_status_value(self.value, positive=True)


type ApplicationStatus = WaitCode | ExitCode | Signal


class ExecutionFailure(StrEnum):
    """Closed, transport-neutral failure categories for an attempted execution."""

    PREPARATION = "preparation"
    DELIVERY = "delivery"
    DEADLINE = "deadline"
    OBSERVATION = "observation"
    PROTOCOL = "protocol"
    INPUT = "input"
    OUTPUT = "output"
    OUTPUT_LIMIT = "output_limit"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class ExecutionOutput:
    """One raw application stream and the facts governing its retention."""

    data: bytes = field(default=b"", repr=False)
    complete: bool = False
    retention: Retention = Retention.CAPTURED

    def __post_init__(self) -> None:
        """Validate values accepted from clients outside static typing."""
        if type(self.data) is not bytes:
            raise ValidationError("Execution output data must be bytes")
        if type(self.complete) is not bool:
            raise ValidationError("Execution output completeness must be boolean")
        if not isinstance(self.retention, Retention):
            raise ValidationError("Execution output requires a supported retention value")
        if self.retention is not Retention.CAPTURED and self.data:
            raise ValidationError("Unretained execution output cannot contain bytes")


@dataclass(frozen=True)
class ExecutionResult:
    """Immutable safe facts observed for one execution attempt."""

    dispatch: Dispatch
    application_state: ApplicationState
    status: ApplicationStatus | None = None
    stdout: ExecutionOutput = field(default_factory=ExecutionOutput)
    stderr: ExecutionOutput = field(default_factory=ExecutionOutput)
    failure: ExecutionFailure | None = None
    owned_cleanup_confirmed: bool = False
    deadline_exceeded: bool = False

    def __post_init__(self) -> None:
        """Validate the public value boundary without revalidating typed children."""
        if not isinstance(self.dispatch, Dispatch):
            raise ValidationError("Execution result requires supported dispatch evidence")
        if not isinstance(self.application_state, ApplicationState):
            raise ValidationError("Execution result requires a supported application state")
        if self.status is not None and type(self.status) not in {WaitCode, ExitCode, Signal}:
            raise ValidationError("Execution result requires a supported application status")
        if (self.application_state is ApplicationState.COMPLETED) != (self.status is not None):
            raise ValidationError("Application status is required exactly when execution completed")
        if self.dispatch is Dispatch.NOT_SENT and self.application_state in {
            ApplicationState.STARTED,
            ApplicationState.COMPLETED,
        }:
            raise ValidationError("An unsent execution cannot have started or completed")
        if type(self.stdout) is not ExecutionOutput or type(self.stderr) is not ExecutionOutput:
            raise ValidationError("Execution result requires supported output values")
        if self.failure is not None and not isinstance(self.failure, ExecutionFailure):
            raise ValidationError("Execution result requires a supported failure category")
        if type(self.owned_cleanup_confirmed) is not bool or type(self.deadline_exceeded) is not bool:
            raise ValidationError("Execution result flags must be boolean")

    @property
    def ok(self) -> bool:
        """Whether every fact required for successful requested semantics is present."""
        return (
            self.application_state is ApplicationState.COMPLETED
            and isinstance(self.status, WaitCode | ExitCode)
            and self.status.value == 0
            and self.failure is None
            and not self.deadline_exceeded
            and self.owned_cleanup_confirmed
            and _output_succeeded(self.stdout)
            and _output_succeeded(self.stderr)
        )

    def check(self) -> Self:
        """Return this result when successful, otherwise raise with the same safe facts."""
        if self.ok:
            return self
        raise CheckedExecutionError(self) from None


def _output_succeeded(output: ExecutionOutput) -> bool:
    return output.retention in {Retention.DISCARDED, Retention.SUPPRESSED} or output.complete


class CheckedExecutionError(ExternalError):
    """A checked execution did not meet the result success predicate."""

    def __init__(
        self,
        result: ExecutionResult,
        *,
        entity_kind: str | None = None,
        entity_name: str | None = None,
        details: ErrorDetails | None = None,
    ) -> None:
        super().__init__(
            "execution did not complete successfully",
            entity_kind=entity_kind,
            entity_name=entity_name,
            details=details,
        )
        self.result = result
