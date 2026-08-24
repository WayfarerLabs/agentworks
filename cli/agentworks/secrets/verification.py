"""Proof-oriented, value-free secret verification services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.secret_backend import OperatorImpact, TtyInteractionAccess
from agentworks.errors import NotFoundError, ValidationError
from agentworks.naming import MAX_SECRET_NAME_LENGTH, validate_name
from agentworks.secrets.preview import ResolutionPreview, preview_batch, preview_hint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.resources.registry import Registry


_INVALID_NAME_ERROR = (
    "invalid secret name; expected 1-253 lowercase alphanumeric characters, "
    "hyphens or underscores, with an alphanumeric first and last character "
    "and no consecutive hyphens"
)


def render_verification(previews: tuple[ResolutionPreview, ...]) -> None:
    """Render value-free preview outcomes in request order."""
    from agentworks import output

    headers = ["NAME", "STATUS", "SOURCE", "IDENTIFIER", "REASON", "HINT"]
    rows = [
        [
            preview.name,
            preview.status.value,
            preview.source or "-",
            preview.identifier or "-",
            preview.reason or "-",
            preview_hint(preview, interaction_opt_in=True),
        ]
        for preview in previews
    ]
    max_col_width = max(len(cell) for row in [headers, *rows] for cell in row)
    for line in output.render_table(headers, rows, max_col_width=max_col_width):
        output.info(line)


def verify_secrets(
    config: Config,
    registry: Registry,
    names: Sequence[str],
    *,
    impact: OperatorImpact,
    tty_access: TtyInteractionAccess,
) -> tuple[ResolutionPreview, ...]:
    """Preview requested declarations once without returning any value."""
    if type(impact) is not OperatorImpact:
        raise ValidationError("impact must be an exact OperatorImpact")
    if type(tty_access) is not TtyInteractionAccess:
        raise ValidationError("tty_access must be an exact TtyInteractionAccess")
    if not names:
        raise ValidationError("at least one secret name is required")
    for name in names:
        if type(name) is not str:
            raise ValidationError(_INVALID_NAME_ERROR) from None
        try:
            validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
        except ValidationError:
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

    from agentworks.secrets.resolve import OutputInteractionBroker, active_sources

    broker = (
        OutputInteractionBroker(declarations)
        if impact is OperatorImpact.ALLOW and tty_access is TtyInteractionAccess.AVAILABLE
        else None
    )
    previews = preview_batch(
        declarations,
        active_sources(config, registry),
        impact=impact,
        tty_access=tty_access,
        interaction_broker=broker,
    )
    return tuple(previews[name] for name in unique_names)
