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


# Where this is called is a mechanical rule, not a judgment call about which functions
# happen to be published today: every construction of a ``ResolutionPolicy`` must be
# preceded on its own call path by a call to this function. Adding a new one means adding
# the check at whichever published entry point supplies that policy's ``interaction``.
# `grep -rn "ResolutionPolicy(" cli/agentworks/` is the whole audit.
#
# Entry points that reach a construction call it first, before any prompt, any DB write,
# and any transport: a rejected policy must leave nothing behind. Where the entry point
# is not itself the constructing function, that ordering is what the check buys, because
# a deeper check can sit inside a best-effort span that downgrades its error to a warning.
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
