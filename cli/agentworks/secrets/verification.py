"""Proof-oriented secret verification services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry


@dataclass(frozen=True, slots=True)
class SecretVerification:
    """Value-free result of proving one named secret resolves."""

    name: str
    verified: bool


class SecretInteractionPolicy(Enum):
    """Caller policy for backends that can require operator interaction."""

    NON_INTERACTIVE = "non-interactive"
    ALLOW_INTERACTIVE = "allow-interactive"


def verify_named_secret(
    config: Config,
    registry: Registry,
    name: str,
    *,
    interaction_policy: SecretInteractionPolicy = SecretInteractionPolicy.NON_INTERACTIVE,
) -> SecretVerification:
    """Resolve one registered secret without retaining or exposing its value."""
    from agentworks import output
    from agentworks.errors import AgentworksError, ValidationError
    from agentworks.secrets.kinds import SECRET_KIND_NAME
    from agentworks.secrets.resolve import (
        _sanitize_verification_exception,
        active_backends,
        resolve_secrets_quiet,
    )

    if type(interaction_policy) is not SecretInteractionPolicy:
        raise ValidationError("secret verification requires an explicit interaction policy")
    if interaction_policy is SecretInteractionPolicy.ALLOW_INTERACTIVE and output.non_interactive():
        raise ValidationError("interactive secret verification is unavailable in global non-interactive mode")
    allow_interactive = interaction_policy is SecretInteractionPolicy.ALLOW_INTERACTIVE

    try:
        decl = registry.lookup(SECRET_KIND_NAME, name)
    except KeyError:
        raise NotFoundError(
            f"secret '{name}' not found",
            entity_kind="secret",
            entity_name=name,
        ) from None

    sanitized: AgentworksError | None = None
    try:
        backends = active_backends(config, registry)
    except Exception as exc:
        sanitized = _sanitize_verification_exception(exc)
        backends = []
    if sanitized is not None:
        raise sanitized from None
    permitted = backends if allow_interactive else [backend for backend in backends if not backend.interactive]
    resolved = resolve_secrets_quiet(
        [decl],
        permitted,
        registry=registry,
        interactive_available=allow_interactive,
    )
    # Deliberately test membership only. The value dies with this frame.
    return SecretVerification(name=name, verified=name in resolved)
