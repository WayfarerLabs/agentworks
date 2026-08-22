"""Value-free actual-resolution outcomes and operation error projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.capabilities.secret_backend import BlockReason, FailureReason
from agentworks.capabilities.secret_backend.client import safe_identity
from agentworks.errors import (
    AgentworksError,
    ConnectivityError,
    ExternalError,
    SecretMappingError,
    SecretUnavailableError,
    StateError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class ResolutionStatus(StrEnum):
    """Wire-safe final actual-resolution statuses."""

    RESOLVED = "resolved"
    MISSING = "missing"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResolutionResolved:
    """The private batch holds the corresponding value."""


@dataclass(frozen=True, slots=True)
class ResolutionMissing:
    """Every reachable candidate ordinarily missed."""


@dataclass(frozen=True, slots=True)
class ResolutionBlocked:
    """The source chain exhausted under an execution limitation."""

    reason: BlockReason

    def __post_init__(self) -> None:
        if type(self.reason) is not BlockReason:
            raise ValueError("invalid resolution block reason")


@dataclass(frozen=True, slots=True)
class ResolutionFailed:
    """A configured lookup or provider operation hard-failed."""

    reason: FailureReason

    def __post_init__(self) -> None:
        if type(self.reason) is not FailureReason:
            raise ValueError("invalid resolution failure reason")


type ResolutionResult = ResolutionResolved | ResolutionMissing | ResolutionBlocked | ResolutionFailed


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """One stable, value-free final result with core-owned identity."""

    name: str
    result: ResolutionResult
    source: str | None = None
    identifier: str | None = None
    backend: str | None = None

    def __post_init__(self) -> None:
        safe_identity(self.name)
        if type(self.result) not in {
            ResolutionResolved,
            ResolutionMissing,
            ResolutionBlocked,
            ResolutionFailed,
        }:
            raise ValueError("invalid resolution result")
        if self.source is not None:
            safe_identity(self.source)
        if self.identifier is not None:
            safe_identity(self.identifier)
        if self.backend is not None:
            safe_identity(self.backend)
        if isinstance(self.result, (ResolutionResolved, ResolutionMissing, ResolutionFailed)) and self.source is None:
            raise ValueError("this resolution result requires a source")
        if isinstance(self.result, ResolutionBlocked):
            structural = self.result.reason in {
                BlockReason.NO_ACTIVE_SOURCE,
                BlockReason.NO_ATTEMPTABLE_SOURCE,
                BlockReason.BATCH_DOOMED,
            }
            if structural is (self.source is not None):
                raise ValueError("resolution block reason disagrees with source identity")

    @property
    def status(self) -> ResolutionStatus:
        return {
            ResolutionResolved: ResolutionStatus.RESOLVED,
            ResolutionMissing: ResolutionStatus.MISSING,
            ResolutionBlocked: ResolutionStatus.BLOCKED,
            ResolutionFailed: ResolutionStatus.FAILED,
        }[type(self.result)]

    @property
    def reason(self) -> BlockReason | FailureReason | None:
        if isinstance(self.result, (ResolutionBlocked, ResolutionFailed)):
            return self.result.reason
        return None


def _safe_diagnostic_text(value: str) -> bool:
    """Return whether text is safe as a retained diagnostic identity."""
    try:
        safe_identity(value)
    except ValueError:
        return False
    return True


_FAILURE_HINTS: dict[FailureReason, str] = {
    FailureReason.INVALID_MAPPING: "check the configured secret mapping",
    FailureReason.LOOKUP_REJECTED: "check that the provider reference identifies a valid target",
    FailureReason.AUTHENTICATION: "authenticate the configured secret provider and retry",
    FailureReason.CONNECTIVITY: "check connectivity to the configured secret provider",
    FailureReason.DEADLINE_EXCEEDED: "increase the source timeout or complete pending provider approval",
    FailureReason.EXTERNAL: "retry after checking the configured secret provider",
    FailureReason.MALFORMED_VALUE: "remove NUL characters from the provider value",
    FailureReason.BACKEND_PROTOCOL: "report the secret backend protocol violation",
    FailureReason.UNEXPECTED: "report the unexpected secret backend failure",
}


def format_hint(outcome: ResolutionOutcome) -> str:
    """Render core-owned guidance from a closed final result."""
    result = outcome.result
    if isinstance(result, ResolutionResolved):
        return "resolved"
    if isinstance(result, ResolutionFailed):
        hint = _FAILURE_HINTS[result.reason]
        if result.reason is FailureReason.DEADLINE_EXCEEDED and outcome.backend == "onepassword":
            return (
                f"{hint}; a pending approval in the 1Password desktop app is a common cause. "
                "`op whoami` is not a reliable exclusion test for app integration"
            )
        return hint
    if isinstance(result, ResolutionMissing):
        return "configure the secret in this source or a later source"
    assert isinstance(result, ResolutionBlocked)
    return {
        BlockReason.TTY_UNAVAILABLE: "run with usable terminal input or configure a non-TTY source",
        BlockReason.TTY_INTERACTION_DISABLED: (
            "global `--non-interactive` disabled terminal input; remove it or configure a non-TTY source"
        ),
        BlockReason.SOURCE_NOT_READY: "make the configured source ready and retry",
        BlockReason.BACKEND_PLUGIN_DISABLED: "enable the configured secret-backend plugin",
        BlockReason.NO_ACTIVE_SOURCE: "configure an active secret source",
        BlockReason.NO_ATTEMPTABLE_SOURCE: "configure an applicable secret mapping or source",
        BlockReason.BATCH_DOOMED: "resolve the other blocking secrets before retrying this complete operation",
    }[result.reason]


def format_outcome(outcome: ResolutionOutcome) -> str:
    """Render stable value-free fields for one final outcome."""
    reason = outcome.reason.value if outcome.reason is not None else "none"
    return (
        f"{outcome.status.value}/{reason}; source={outcome.source or 'none'}; "
        f"identifier={outcome.identifier or 'none'}; hint={format_hint(outcome)}"
    )


def complete_resolution_error(outcomes: Sequence[ResolutionOutcome]) -> AgentworksError:
    """Map an incomplete value-free batch to the operation error taxonomy."""
    failed = tuple(outcome for outcome in outcomes if outcome.status is not ResolutionStatus.RESOLVED)
    if not failed:
        raise StateError("complete_resolution_error requires an incomplete batch") from None
    first = failed[0]
    if isinstance(first.result, ResolutionFailed):
        if first.result.reason in {FailureReason.INVALID_MAPPING, FailureReason.LOOKUP_REJECTED}:
            error_type: type[AgentworksError] = SecretMappingError
        elif first.result.reason is FailureReason.CONNECTIVITY:
            error_type = ConnectivityError
        else:
            error_type = ExternalError
    else:
        error_type = SecretUnavailableError
    names = ", ".join(outcome.name for outcome in failed)
    hint = "\n".join(f"{outcome.name}: {format_outcome(outcome)}" for outcome in failed)
    return error_type(f"secret resolution failed for: {names}", hint=hint)
