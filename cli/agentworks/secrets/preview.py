"""Provider-aware, value-free secret preview orchestration."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from agentworks.capabilities.secret_backend.client import (
    BackendPreview,
    BlockReason,
    FailureReason,
    InteractionBroker,
    OperatorImpact,
    PreviewAvailable,
    PreviewBlocked,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    PreviewMissing,
    SecretLookupRequest,
    TtyInteractionAccess,
    safe_identity,
)
from agentworks.errors import StateError, UserAbort
from agentworks.secrets.resolve import ActiveSource, _BackendProtocolError, _drive_source, _lookup_projection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.machine_output import JsonObject, JsonValue
    from agentworks.secrets.base import SecretDecl


class PreviewStatus(StrEnum):
    """Wire-safe aggregate preview statuses."""

    AVAILABLE = "available"
    MISSING = "missing"
    INDETERMINATE = "indeterminate"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AggregateNoCandidate:
    """No active source had an applicable runtime lookup."""


type AggregatePreview = BackendPreview | AggregateNoCandidate


@dataclass(frozen=True, slots=True)
class SourcePreviewAttempt:
    """One ordered, core-attributed preview attempt."""

    source: str
    identifier: str | None
    result: BackendPreview

    def __post_init__(self) -> None:
        safe_identity(self.source)
        if self.identifier is not None:
            safe_identity(self.identifier)
        if type(self.result) not in {
            PreviewAvailable,
            PreviewMissing,
            PreviewIndeterminate,
            PreviewBlocked,
            PreviewFailed,
        }:
            raise ValueError("invalid source preview result")
        if isinstance(self.result, PreviewBlocked) and self.result.reason in {
            BlockReason.NO_ACTIVE_SOURCE,
            BlockReason.NO_ATTEMPTABLE_SOURCE,
        }:
            raise ValueError("final-only block reason cannot appear in a preview attempt")


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    """One value-free aggregate with ordered source evidence."""

    name: str
    result: AggregatePreview
    source: str | None
    identifier: str | None
    attempts: tuple[SourcePreviewAttempt, ...]

    def __post_init__(self) -> None:
        safe_identity(self.name)
        if self.source is not None:
            safe_identity(self.source)
        if self.identifier is not None:
            safe_identity(self.identifier)
        if isinstance(self.result, AggregateNoCandidate):
            if self.source is not None or self.identifier is not None or self.attempts:
                raise ValueError("no-candidate preview cannot carry attempt identity")
            return
        if type(self.result) not in {
            PreviewAvailable,
            PreviewMissing,
            PreviewIndeterminate,
            PreviewBlocked,
            PreviewFailed,
        }:
            raise ValueError("invalid aggregate preview result")
        if not self.attempts or self.source is None:
            raise ValueError("runtime preview result requires an attributed attempt")

    @property
    def status(self) -> PreviewStatus:
        if isinstance(self.result, AggregateNoCandidate):
            return PreviewStatus.BLOCKED
        return {
            PreviewAvailable: PreviewStatus.AVAILABLE,
            PreviewMissing: PreviewStatus.MISSING,
            PreviewIndeterminate: PreviewStatus.INDETERMINATE,
            PreviewBlocked: PreviewStatus.BLOCKED,
            PreviewFailed: PreviewStatus.FAILED,
        }[type(self.result)]

    @property
    def reason(self) -> str | None:
        if isinstance(self.result, AggregateNoCandidate):
            return "no-candidate"
        if isinstance(self.result, (PreviewIndeterminate, PreviewBlocked, PreviewFailed)):
            return self.result.reason.value
        return None


def attempt_status(attempt: SourcePreviewAttempt) -> PreviewStatus:
    """Return the wire status for one exact attempt variant."""
    return {
        PreviewAvailable: PreviewStatus.AVAILABLE,
        PreviewMissing: PreviewStatus.MISSING,
        PreviewIndeterminate: PreviewStatus.INDETERMINATE,
        PreviewBlocked: PreviewStatus.BLOCKED,
        PreviewFailed: PreviewStatus.FAILED,
    }[type(attempt.result)]


def attempt_reason(attempt: SourcePreviewAttempt) -> str | None:
    """Return the conditional wire reason for one attempt."""
    if isinstance(attempt.result, (PreviewIndeterminate, PreviewBlocked, PreviewFailed)):
        return attempt.result.reason.value
    return None


def preview_data(preview: ResolutionPreview) -> JsonObject:
    """Project a preview into its closed JSON-compatible tagged shape."""
    data: JsonObject = {
        "status": preview.status.value,
        "source": preview.source,
        "identifier": preview.identifier,
        "attempts": [],
    }
    if preview.reason is not None:
        data["reason"] = preview.reason
    attempts: list[JsonValue] = []
    for attempt in preview.attempts:
        projected: JsonObject = {
            "source": attempt.source,
            "identifier": attempt.identifier,
            "status": attempt_status(attempt).value,
        }
        reason = attempt_reason(attempt)
        if reason is not None:
            projected["reason"] = reason
        attempts.append(projected)
    data["attempts"] = attempts
    return data


def preview_hint(preview: ResolutionPreview, *, interaction_opt_in: bool) -> str:
    """Render caller-aware guidance from a closed preview result."""
    if preview.status is PreviewStatus.AVAILABLE:
        return "available under the requested preview impact"
    if preview.status is PreviewStatus.MISSING:
        return "configure the value in this source or a later source"
    reason = preview.reason
    if reason == "operator-impact-limited":
        return (
            "retry with --allow-interaction for a definitive preview"
            if interaction_opt_in
            else "broader operator impact could produce a definitive preview"
        )
    if reason == BlockReason.TTY_UNAVAILABLE.value:
        return "run with usable terminal input or inspect a non-TTY source"
    if reason == BlockReason.TTY_INTERACTION_DISABLED.value:
        return "global --non-interactive disabled terminal input only"
    if reason == BlockReason.SOURCE_NOT_READY.value:
        return "make the configured source ready and retry"
    if reason == BlockReason.BACKEND_PLUGIN_DISABLED.value:
        return "enable the configured secret-backend plugin"
    if reason == "no-candidate":
        return "configure an applicable active source mapping"
    if isinstance(preview.result, PreviewFailed):
        return {
            FailureReason.INVALID_MAPPING: "check the configured secret mapping",
            FailureReason.LOOKUP_REJECTED: "check the provider reference",
            FailureReason.AUTHENTICATION: "authenticate the configured secret provider",
            FailureReason.CONNECTIVITY: "check provider connectivity",
            FailureReason.DEADLINE_EXCEEDED: "complete provider approval or increase the source timeout",
            FailureReason.EXTERNAL: "check the provider and retry",
            FailureReason.MALFORMED_VALUE: "remove NUL characters from the provider value",
            FailureReason.BACKEND_PROTOCOL: "report the secret backend protocol violation",
            FailureReason.UNEXPECTED: "report the unexpected secret backend failure",
        }[preview.result.reason]
    return "retry after correcting the blocked source"


def _aggregate(name: str, attempts: list[SourcePreviewAttempt]) -> ResolutionPreview:
    """Collapse an exhausted attempt chain using the fixed precedence."""
    indeterminate = next((attempt for attempt in attempts if isinstance(attempt.result, PreviewIndeterminate)), None)
    if indeterminate is not None:
        return ResolutionPreview(
            name=name,
            result=indeterminate.result,
            source=indeterminate.source,
            identifier=indeterminate.identifier,
            attempts=tuple(attempts),
        )
    blocked = next((attempt for attempt in attempts if isinstance(attempt.result, PreviewBlocked)), None)
    if blocked is not None:
        return ResolutionPreview(
            name=name,
            result=blocked.result,
            source=blocked.source,
            identifier=blocked.identifier,
            attempts=tuple(attempts),
        )
    if attempts:
        first = attempts[0]
        return ResolutionPreview(
            name=name,
            result=PreviewMissing(),
            source=first.source,
            identifier=first.identifier,
            attempts=tuple(attempts),
        )
    return ResolutionPreview(
        name=name,
        result=AggregateNoCandidate(),
        source=None,
        identifier=None,
        attempts=(),
    )


def preview_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    impact: OperatorImpact,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> dict[str, ResolutionPreview]:
    """Preview a deduplicated batch in one bounded source-first pass."""
    if type(impact) is not OperatorImpact:
        raise StateError("impact must be an exact OperatorImpact")
    if type(tty_access) is not TtyInteractionAccess:
        raise StateError("tty_access must be an exact TtyInteractionAccess")
    names = list(dict.fromkeys(secret.name for secret in secrets))
    declarations: dict[str, SecretDecl] = {}
    for secret in secrets:
        declarations.setdefault(secret.name, secret)
    attempts: dict[str, list[SourcePreviewAttempt]] = {name: [] for name in names}
    completed: dict[str, ResolutionPreview] = {}

    for source in sources:
        pending = [declarations[name] for name in names if name not in completed]
        if not pending:
            break
        projected: list[tuple[SecretLookupRequest, str | None]] = []
        for secret in pending:
            try:
                request, description = _lookup_projection(secret, source)
            except _BackendProtocolError:
                protocol_result = PreviewFailed(FailureReason.BACKEND_PROTOCOL)
                attempt = SourcePreviewAttempt(source.name, None, protocol_result)
                attempts[secret.name].append(attempt)
                completed[secret.name] = ResolutionPreview(
                    name=secret.name,
                    result=protocol_result,
                    source=source.name,
                    identifier=None,
                    attempts=tuple(attempts[secret.name]),
                )
                continue
            if request is not None:
                projected.append((request, description.identifier))
        if not projected:
            continue
        block_reason: BlockReason | None = None
        if source.disabled_backend_plugin is not None:
            block_reason = BlockReason.BACKEND_PLUGIN_DISABLED
        elif not source.readiness.is_ready:
            block_reason = BlockReason.SOURCE_NOT_READY
        if block_reason is not None:
            for request, identifier in projected:
                attempts[request.name].append(
                    SourcePreviewAttempt(source.name, identifier, PreviewBlocked(block_reason))
                )
            continue

        requests = tuple(request for request, _identifier in projected)
        identifiers = {request.name: identifier for request, identifier in projected}
        returned: object
        try:
            returned = _drive_source(
                source,
                requests,
                intent=PreviewIntent(impact),
                tty_access=tty_access,
                interaction_broker=interaction_broker,
            )
        except (UserAbort, concurrent.futures.CancelledError):
            raise
        except _BackendProtocolError:
            returned = {request.name: PreviewFailed(FailureReason.BACKEND_PROTOCOL) for request in requests}
        except Exception:
            returned = {request.name: PreviewFailed(FailureReason.UNEXPECTED) for request in requests}
        for request in requests:
            result = cast("BackendPreview", returned[request.name])
            attempt = SourcePreviewAttempt(source.name, identifiers[request.name], result)
            attempts[request.name].append(attempt)
            if isinstance(result, (PreviewAvailable, PreviewFailed)):
                completed[request.name] = ResolutionPreview(
                    name=request.name,
                    result=result,
                    source=source.name,
                    identifier=identifiers[request.name],
                    attempts=tuple(attempts[request.name]),
                )

    for name in names:
        if name not in completed:
            completed[name] = _aggregate(name, attempts[name])
    return {name: completed[name] for name in names}
