"""Proof-oriented secret verification services."""

from __future__ import annotations

from dataclasses import dataclass
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


def verify_named_secret(
    config: Config,
    registry: Registry,
    name: str,
    *,
    allow_interactive: bool = False,
) -> SecretVerification:
    """Resolve one registered secret without retaining or exposing its value."""
    from agentworks.errors import AgentworksError
    from agentworks.secrets.kinds import SECRET_KIND_NAME
    from agentworks.secrets.resolve import (
        _sanitize_verification_exception,
        active_backends,
        resolve_secrets_quiet,
    )

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
