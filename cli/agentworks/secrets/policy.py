"""Explicit interaction authority for secret-consuming operations."""

from __future__ import annotations

from enum import StrEnum

from agentworks.errors import StateError


class TtyInteractionPolicy(StrEnum):
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
#
# The audit has two halves, and `grep -rn "ResolutionPolicy(" cli/agentworks/` is only the
# first. It finds every construction, so it settles TOTALITY. It cannot find a published entry
# point that CONSUMES ``interaction`` and constructs no policy, which is the REACH case above,
# and that gap is not hypothetical: ``preview_operation_resolution`` and ``predict_resolution``
# compare it by identity, build nothing, and went unchecked until someone read them. There is no
# clean grep for the second half (roughly seventy functions take the parameter and ten check
# it), so it is a criterion rather than a command: a function that accepts ``interaction`` from
# a caller and can return without any policy being constructed checks it itself.
#
# That criterion governs new code. It does NOT describe the tree today: `list_sessions`
# (`sessions/_queries.py`) returns under `names_only` above its only forwarding, and
# `delete_workspace` (`workspaces/manager/delete.py`) forwards only when a VM is attached and
# returns successfully otherwise. Both satisfy the criterion and neither checks. Issue #544 is
# the sweep of the remaining entry points; until it lands, read this as the rule to write to
# rather than an invariant to rely on.
#
# Apply both halves mechanically, including at a site whose own construction sits a line or two
# down and whose constructor would therefore reject the value anyway. The alternative is a
# per-site judgment about how close is close enough, which is what left a boundary bare before.
def require_exact_tty_interaction_policy(interaction: TtyInteractionPolicy) -> None:
    """Reject anything that is not an exact ``TtyInteractionPolicy`` member.

    Boundary: a caller-supplied argument crossing the published service surface. The
    services that resolve secrets accept ``interaction`` from callers our type checker
    does not see, and the value decides whether an interactive source may be attempted,
    so it is checked on arrival. Interior forwarding needs no check of its own.
    """
    if type(interaction) is not TtyInteractionPolicy:
        raise StateError("interaction must be an exact TtyInteractionPolicy")
