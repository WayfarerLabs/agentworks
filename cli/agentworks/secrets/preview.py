"""Pure, value-free prediction for secret inspection and preflight."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy
from agentworks.secrets.resolve import ActiveSource, _BackendProtocolError, _lookup_projection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .base import SecretDecl


def _safe_text(value: str) -> bool:
    return all(unicodedata.category(char) not in {"Cc", "Cf"} for char in value)


class PreviewCategory(StrEnum):
    ATTEMPTABLE = "attemptable"
    REFUSED_INTERACTION = "refused-interaction"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SkippedSource:
    source: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not _safe_text(self.source) or not _safe_text(self.reason):
            raise ValueError("invalid skipped source")


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    name: str
    category: PreviewCategory
    source: str | None
    identifier: str | None
    skipped_not_ready: tuple[SkippedSource, ...]

    def __post_init__(self) -> None:
        if self.category in {PreviewCategory.ATTEMPTABLE, PreviewCategory.REFUSED_INTERACTION}:
            if self.source is None:
                raise ValueError("attemptable preview requires a source")
        elif self.source is not None or self.identifier is not None:
            raise ValueError("unavailable preview forbids source and identifier")
        if not _safe_text(self.name):
            raise ValueError("invalid preview name")
        if self.source is not None and not _safe_text(self.source):
            raise ValueError("invalid preview source")
        if self.identifier is not None and not _safe_text(self.identifier):
            raise ValueError("invalid preview identifier")


def _preview(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy | None,
) -> ResolutionPreview:
    skipped: list[SkippedSource] = []
    first_refused: tuple[str, str | None] | None = None
    for source in sources:
        try:
            request, identifier = _lookup_projection(secret, source)
        except _BackendProtocolError:
            raise StateError(f"secret source {source.name!r} violated the preview contract") from None
        if request is None:
            continue
        if not source.readiness.is_ready:
            reason = source.readiness.reason
            if not reason:
                raise StateError(f"secret source {source.name!r} has invalid readiness") from None
            skipped.append(SkippedSource(source=source.name, reason=reason))
            continue
        if interaction is InteractionPolicy.REFUSE and source.interactive:
            if first_refused is None:
                first_refused = (source.name, identifier)
            continue
        return ResolutionPreview(
            name=secret.name,
            category=PreviewCategory.ATTEMPTABLE,
            source=source.name,
            identifier=identifier,
            skipped_not_ready=tuple(skipped),
        )
    if first_refused is not None:
        source_name, identifier = first_refused
        return ResolutionPreview(
            name=secret.name,
            category=PreviewCategory.REFUSED_INTERACTION,
            source=source_name,
            identifier=identifier,
            skipped_not_ready=tuple(skipped),
        )
    return ResolutionPreview(
        name=secret.name,
        category=PreviewCategory.UNAVAILABLE,
        source=None,
        identifier=None,
        skipped_not_ready=tuple(skipped),
    )


def preview_resolution(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
) -> ResolutionPreview:
    """Predict whether inspection has a source without applying operation refusal."""
    return _preview(secret, sources, interaction=None)


def preview_operation_resolution(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy,
) -> ResolutionPreview:
    """Predict whether an operation has a source under an exact interaction policy."""
    interaction = validate_interaction_policy(interaction)
    return _preview(secret, sources, interaction=interaction)
