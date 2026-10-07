"""Private validation shared by safe operation diagnostics."""

from __future__ import annotations

from agentworks.errors import ValidationError


def validate_logical_entity_value(value: object, label: str, *, subject: str) -> None:
    """Refuse unsafe identity text before it can reach a rendered error."""
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or any(character in value for character in "\\/\x00\r\n")
    ):
        raise ValidationError(f"{subject} requires a safe logical entity {label}")
