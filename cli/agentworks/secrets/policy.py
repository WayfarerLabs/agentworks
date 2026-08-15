"""Explicit interaction authority for secret-consuming operations."""

from __future__ import annotations

from enum import StrEnum

from agentworks.errors import StateError


class InteractionPolicy(StrEnum):
    """Whether an operation permits sources that may require interaction."""

    # Consumers branch on this type by identity (`is`), so an equal-but-not-identical value
    # such as a plain "refuse" silently takes the wrong branch. Never construct a policy from
    # a string, config, or deserialized input: these members are the only legitimate values.
    ALLOW = "allow"
    REFUSE = "refuse"


def require_exact_interaction_policy(interaction: InteractionPolicy) -> None:
    """Reject anything that is not an exact ``InteractionPolicy`` member.

    Boundary: a caller-supplied argument crossing the published service surface. The
    services that resolve secrets accept ``interaction`` from callers our type checker
    does not see, and the value decides whether an interactive source may be attempted,
    so it is checked once on arrival. Forwarding a checked policy onward is interior and
    is not checked again.
    """
    if type(interaction) is not InteractionPolicy:
        raise StateError("interaction must be an exact InteractionPolicy")
