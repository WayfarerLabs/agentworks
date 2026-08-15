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


# Two different things call this, and each buys something the other cannot.
#
# TOTALITY comes from ``ResolutionPolicy.__post_init__``: no policy can be constructed
# from an unchecked ``interaction``, whatever route reached the construction. That needs
# no upkeep, and it is why a new construction site cannot silently go unchecked.
#
# POSITION comes from the entry points that call this themselves, before doing work a
# rejection should not have to unwind: a rejected policy must leave nothing behind, so an
# entry point checks before any state change. ``delete_vm`` is why that matters, because
# it reaches its resolver inside a best-effort span that downgrades an ``AgentworksError``
# to a warning, which swallowed the deeper rejection and completed the delete. Confirming
# with the operator first is not a state change, and ``delete_workspace`` and
# ``delete_agent`` do exactly that before their boundary checks.
#
# When judging whether a new construction is covered, an ``interaction`` value qualifies
# in one of two ways: it was checked in the function that constructs the policy, or it is
# read from a field of an object whose constructor checked it (the three constructions
# inside ``Resolver`` read ``self._interaction``, checked in ``Resolver.__init__`` and
# never reassigned, so no check sits on the call path of ``Resolver.resolve()`` itself). A
# new ``Resolver`` method taking ``interaction`` as a parameter has neither and needs its
# own check. `grep -rn "ResolutionPolicy(" cli/agentworks/` is the whole audit.
def require_exact_interaction_policy(interaction: InteractionPolicy) -> None:
    """Reject anything that is not an exact ``InteractionPolicy`` member.

    Boundary: a caller-supplied argument crossing the published service surface. The
    services that resolve secrets accept ``interaction`` from callers our type checker
    does not see, and the value decides whether an interactive source may be attempted,
    so it is checked once on arrival. Interior forwarding needs no check of its own; where
    one value does reach this more than once, the extra calls buy the ordering and totality
    described above rather than distrust of the interior.
    """
    if type(interaction) is not InteractionPolicy:
        raise StateError("interaction must be an exact InteractionPolicy")
