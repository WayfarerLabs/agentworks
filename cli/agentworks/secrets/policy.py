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


# Both ``ResolutionPolicy.__post_init__`` and the service entry points call this, and neither
# makes the other redundant. Three things are being bought:
#
# - TOTALITY: the constructor calls it, so no policy exists that was built from an unchecked
#   ``interaction``, whatever route reached the construction.
# - POSITION: an entry point checks before any state change, so a rejection leaves nothing
#   behind even where the deeper one would surface inside a best-effort span that downgrades
#   it to a warning. Prompting for confirmation is not a state change.
# - REACH: an entry point can accept ``interaction`` and return successfully without building
#   any policy (no declarations to resolve, nothing to reveal, every secret already gate-seeded),
#   and there the check on arrival is the only rejection there will ever be.
#
# A new construction is covered when its ``interaction`` was checked in the function that
# constructs the policy, or read from a field checked when the object was built: the three
# constructions inside ``Resolver`` read ``self._interaction``, checked in ``Resolver.__init__``
# and never reassigned. A new ``Resolver`` method taking ``interaction`` would have neither.
# `grep -rn "ResolutionPolicy(" cli/agentworks/` is the whole audit. Apply the rule mechanically,
# including at a site whose own construction sits a line or two down and whose constructor would
# therefore reject the value anyway. The alternative is a per-site judgment about how close is
# close enough, which is what left a boundary bare before.
def require_exact_interaction_policy(interaction: InteractionPolicy) -> None:
    """Reject anything that is not an exact ``InteractionPolicy`` member.

    Boundary: a caller-supplied argument crossing the published service surface. The
    services that resolve secrets accept ``interaction`` from callers our type checker
    does not see, and the value decides whether an interactive source may be attempted,
    so it is checked on arrival. Interior forwarding needs no check of its own.
    """
    if type(interaction) is not InteractionPolicy:
        raise StateError("interaction must be an exact InteractionPolicy")
