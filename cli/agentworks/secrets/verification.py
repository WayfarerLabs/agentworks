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
    from agentworks.errors import ValidationError
    from agentworks.secrets.kinds import SECRET_KIND_NAME
    from agentworks.secrets.resolve import (
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

    # Registry and chain construction are first-party orchestration. Preserve
    # their typed diagnostics; only calls into a selected backend are treated
    # as provider-controlled and sanitized by ``resolve_secrets_quiet``.
    backends = active_backends(config, registry)
    resolved = resolve_secrets_quiet(
        [decl],
        backends,
        registry=registry,
        interactive_available=allow_interactive,
    )
    # The ordered resolver either proves every requested name or raises. Keep
    # this membership check as an internal contract without carrying a
    # permanently true result field into the CLI.
    assert name in resolved, "secret resolver returned without the requested proof"
    return SecretVerification(name=name)
