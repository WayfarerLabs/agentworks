"""Explicit terminal-interaction authority for secret operations."""

from __future__ import annotations

from enum import StrEnum

from agentworks.capabilities.secret_backend import TtyInteractionAccess
from agentworks.errors import StateError


class TtyInteractionPolicy(StrEnum):
    """Whether an operation permits use of terminal input."""

    # Consumers branch on this type by identity (`is`), so an equal-but-not-identical value
    # such as a plain "refuse" silently takes the wrong branch. Never construct a policy from
    # a string, config, or deserialized input: these members are the only legitimate values.
    ALLOW = "allow"
    REFUSE = "refuse"


def require_exact_tty_interaction_policy(interaction: TtyInteractionPolicy) -> None:
    """Reject anything that is not an exact ``TtyInteractionPolicy`` member.

    Boundary: a caller-supplied argument crossing the published service surface. The
    services that resolve secrets accept ``interaction`` from callers our type checker
    does not see, so it is checked on arrival. Interior forwarding needs no check of
    its own.
    """
    if type(interaction) is not TtyInteractionPolicy:
        raise StateError("interaction must be an exact TtyInteractionPolicy")


def require_exact_tty_interaction_access(access: TtyInteractionAccess) -> None:
    """Reject anything that is not an exact ``TtyInteractionAccess`` member.

    Boundary: caller-supplied access crossing a published service surface.
    """
    if type(access) is not TtyInteractionAccess:
        raise StateError("tty_access must be an exact TtyInteractionAccess")


def tty_interaction_access(
    policy: TtyInteractionPolicy,
    *,
    terminal_input_usable: bool,
) -> TtyInteractionAccess:
    """Combine exact TTY policy with the physical terminal fact."""
    require_exact_tty_interaction_policy(policy)
    if type(terminal_input_usable) is not bool:
        raise StateError("terminal_input_usable must be an exact bool")
    if policy is TtyInteractionPolicy.REFUSE:
        return TtyInteractionAccess.DISABLED
    if terminal_input_usable:
        return TtyInteractionAccess.AVAILABLE
    return TtyInteractionAccess.UNAVAILABLE
