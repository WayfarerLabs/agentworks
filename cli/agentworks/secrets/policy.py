"""Explicit interaction authority for secret-consuming operations."""

from __future__ import annotations

from enum import StrEnum

from agentworks.errors import StateError


class InteractionPolicy(StrEnum):
    """Whether an operation permits sources that may require interaction."""

    ALLOW = "allow"
    REFUSE = "refuse"


def validate_interaction_policy(value: object) -> InteractionPolicy:
    """Return an exact policy enum or fail without rendering the input."""
    if type(value) is not InteractionPolicy:
        raise StateError("interaction must be an exact InteractionPolicy") from None
    return value
