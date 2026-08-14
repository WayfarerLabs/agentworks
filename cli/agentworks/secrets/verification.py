"""Proof-oriented secret verification services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError, ValidationError
from agentworks.naming import MAX_SECRET_NAME_LENGTH, validate_name
from agentworks.secrets.outcomes import format_remediation
from agentworks.secrets.policy import InteractionPolicy

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


def render_verification(outcomes: tuple[ResolutionOutcome, ...]) -> None:
    """Render value-free resolution outcomes in request order."""
    from agentworks import output

    headers = ["NAME", "CATEGORY", "SOURCE", "IDENTIFIER", "DETAIL", "REMEDIATION"]
    rows = [
        [
            outcome.name,
            outcome.category.value,
            outcome.source or "-",
            outcome.identifier or "-",
            outcome.detail.value,
            format_remediation(outcome),
        ]
        for outcome in outcomes
    ]
    max_col_width = max(len(cell) for row in [headers, *rows] for cell in row)
    for line in output.render_table(
        headers,
        rows,
        max_col_width=max_col_width,
    ):
        output.info(line)


def verify_secrets(
    config: Config,
    registry: Registry,
    names: Sequence[str],
    *,
    interaction: InteractionPolicy,
) -> tuple[ResolutionOutcome, ...]:
    """Resolve requested declarations once and return value-free outcomes."""
    if not names:
        raise ValidationError("at least one secret name is required")
    invalid_name = False
    for name in names:
        if type(name) is not str:
            invalid_name = True
        else:
            try:
                validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
            except ValidationError:
                invalid_name = True
        if invalid_name:
            del name
            names = ()
            break
    if invalid_name:
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
        OutputInteractionBroker,
        ResolutionPolicy,
        active_sources,
        resolve_batch,
    )

    broker = OutputInteractionBroker(declarations) if interaction is InteractionPolicy.ALLOW else None
    batch = resolve_batch(
        declarations,
        active_sources(config, registry),
        policy=ResolutionPolicy(interaction=interaction, completion=CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    return batch.outcomes
