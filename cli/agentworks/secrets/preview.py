"""Pure, value-free prediction for secret inspection and preflight."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.secrets.outcomes import _safe_diagnostic_text
from agentworks.secrets.policy import InteractionPolicy, require_exact_interaction_policy
from agentworks.secrets.resolve import ActiveSource, _BackendProtocolError, _lookup_projection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .base import SecretDecl


class PreviewCategory(StrEnum):
    ATTEMPTABLE = "attemptable"
    REFUSED_NON_INTERACTIVE = "refused-non-interactive"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SkippedSource:
    source: str
    reason: str

    def __post_init__(self) -> None:
        # ``reason`` is screened like the names beside it, and registration is
        # not what makes that unnecessary: registration vets a plugin's SHAPE
        # (a non-empty, '/'-free name; types; call shapes), never the text it
        # later produces. This field is a mixture. ``sources.py`` composes most
        # of it from our own prose around a plugin-authored name that
        # ``plugins/enablement.py`` baked in, and returns ``impl.not_ready()``
        # wholesale in the remaining case.
        #
        # Escaping at the render sink, which is how the sibling plugin name on
        # ``ResolutionOutcome.remediation_target`` is handled, cannot work here:
        # that one survives as its own field so the sink can escape exactly it,
        # while this one is already concatenated into first-party prose, so the
        # sink would have to escape our punctuation along with it. Issue #545
        # tracks escaping the name upstream, where it is still a separate token;
        # until then this screen is what keeps a rendered row one row.
        if not self.reason:
            raise ValueError("a skipped source must say why it was skipped")
        if not _safe_diagnostic_text(self.source):
            raise ValueError("invalid skipped source name")
        if not _safe_diagnostic_text(self.reason):
            raise ValueError("invalid skipped source reason")


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    name: str
    category: PreviewCategory
    source: str | None
    identifier: str | None
    skipped_not_ready: tuple[SkippedSource, ...]
    # Terminal-channel sources the walk skipped because no terminal is
    # available to this process; a separate list so the not-ready one keeps
    # meaning exactly what its name says.
    skipped_no_terminal: tuple[SkippedSource, ...]

    def __post_init__(self) -> None:
        if self.category in {PreviewCategory.ATTEMPTABLE, PreviewCategory.REFUSED_NON_INTERACTIVE}:
            if self.source is None:
                raise ValueError("attemptable preview requires a source")
        elif self.source is not None or self.identifier is not None:
            raise ValueError("unavailable preview forbids source and identifier")
        # Boundary: the same operator-authored text a resolution outcome
        # carries. See ``_safe_diagnostic_text``. ``source`` and ``identifier``
        # reach this row's own rendered surfaces (``secret describe``,
        # ``doctor``). ``name`` reaches none of them, and is screened for a
        # different reason: ``describe_secret`` builds a preview before its
        # renderer prints that same operator-chosen name in the header
        # (``inspect.py``), so this is where a forged one is caught. Call
        # order is what puts the catch here.
        if not _safe_diagnostic_text(self.name):
            raise ValueError("invalid preview name")
        if self.source is not None and not _safe_diagnostic_text(self.source):
            raise ValueError("invalid preview source")
        if self.identifier is not None and not _safe_diagnostic_text(self.identifier):
            raise ValueError("invalid preview identifier")


# The fixed, first-party reason a terminal-channel source is skipped in a
# preview; the resolver's equivalent is ResolutionDetail.TERMINAL_UNAVAILABLE.
NO_TERMINAL_REASON = "no terminal available for a prompt"


def _preview(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy | None,
) -> ResolutionPreview:
    from agentworks.capabilities.secret_backend.base import InteractionChannel
    from agentworks.output import terminal_prompt_available

    skipped: list[SkippedSource] = []
    skipped_no_terminal: list[SkippedSource] = []
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
        # Gate order mirrors the resolver's ``_blocked_turn``: consent first,
        # then the terminal fact, so predictions never disagree with the pass.
        channel = source.interaction_channel
        if interaction is InteractionPolicy.REFUSE and channel is not InteractionChannel.NONE:
            if first_refused is None:
                first_refused = (source.name, identifier)
            continue
        # A fact, not a policy, so it applies to the inspection variant
        # (``interaction=None``) too: without a terminal a terminal-channel
        # source cannot take a turn no matter what an operation consents to.
        if channel is InteractionChannel.TERMINAL and not terminal_prompt_available():
            skipped_no_terminal.append(SkippedSource(source=source.name, reason=NO_TERMINAL_REASON))
            continue
        return ResolutionPreview(
            name=secret.name,
            category=PreviewCategory.ATTEMPTABLE,
            source=source.name,
            identifier=identifier,
            skipped_not_ready=tuple(skipped),
            skipped_no_terminal=tuple(skipped_no_terminal),
        )
    if first_refused is not None:
        source_name, identifier = first_refused
        return ResolutionPreview(
            name=secret.name,
            category=PreviewCategory.REFUSED_NON_INTERACTIVE,
            source=source_name,
            identifier=identifier,
            skipped_not_ready=tuple(skipped),
            skipped_no_terminal=tuple(skipped_no_terminal),
        )
    return ResolutionPreview(
        name=secret.name,
        category=PreviewCategory.UNAVAILABLE,
        source=None,
        identifier=None,
        skipped_not_ready=tuple(skipped),
        skipped_no_terminal=tuple(skipped_no_terminal),
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
    """Predict whether an operation has a source under an exact interaction policy.

    Checks its own ``interaction`` because this is a published entry point
    that consumes the value and builds no ``ResolutionPolicy``, so the
    constructor's totality never reaches it. ``_preview`` compares it by
    identity, and a plain ``"refuse"`` predicts attemptable where the
    operation would refuse. The check is first, before any source walk, so a
    rejection costs nothing.
    """
    require_exact_interaction_policy(interaction)
    return _preview(secret, sources, interaction=interaction)
