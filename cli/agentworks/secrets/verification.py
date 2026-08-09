"""Proof-oriented secret verification services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError, ValidationError
from agentworks.naming import MAX_SECRET_NAME_LENGTH, validate_name
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.outcomes import ResolutionOutcome


_INVALID_NAME_ERROR = (
    "invalid secret name; expected 1-253 lowercase alphanumeric characters, "
    "hyphens or underscores, with an alphanumeric first and last character "
    "and no consecutive hyphens"
)


def verify_secrets(
    config: Config,
    registry: Registry,
    names: Sequence[str],
    *,
    interaction: InteractionPolicy,
) -> tuple[ResolutionOutcome, ...]:
    """Resolve requested declarations once, discard values, and return outcomes."""
    interaction = validate_interaction_policy(interaction)
    if not names:
        raise ValidationError("at least one secret name is required")
    for name in names:
        try:
            if type(name) is not str:
                raise ValueError
            validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
        except Exception:
            raise ValidationError(_INVALID_NAME_ERROR) from None

    unique_names = tuple(dict.fromkeys(names))
    from agentworks.secrets.kinds import SECRET_KIND_NAME

    declarations = []
    for name in unique_names:
        try:
            declarations.append(registry.lookup(SECRET_KIND_NAME, name))
        except KeyError:
            raise NotFoundError(
                f"secret '{name}' not found",
                entity_kind="secret",
                entity_name=name,
            ) from None

    from agentworks.secrets.resolve import (
        CompletionPolicy,
        ResolutionPolicy,
        _OutputInteractionBroker,
        active_sources,
        resolve_batch,
    )

    broker = _OutputInteractionBroker(declarations) if interaction is InteractionPolicy.ALLOW else None
    batch = resolve_batch(
        declarations,
        active_sources(config, registry),
        policy=ResolutionPolicy(interaction=interaction, completion=CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    try:
        outcomes = batch.discard_values()
        batch.scrub_values()
        return outcomes
    except BaseException:
        batch.scrub_values()
        raise
