"""Secret declarations, sources, and resolver.

See ``docs/sdd/2026-06-05-env-and-secrets/`` for design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources import Registry


from agentworks.secrets.base import (
    SecretBackendConfig,
    SecretConfig,
    SecretDecl,
    SecretSource,
    SecretSourceBase,
)
from agentworks.secrets.env_var import EnvVarSource, env_var_name_for
from agentworks.secrets.orchestration import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.prompt import PromptSource
from agentworks.secrets.resolver import SecretResolver

# Known backend kind identifiers. The framework's secret_backend kind
# is the registry-side handle; the publisher below adds one row per
# entry as built-in so operator-declared [secret_backends.<kind>]
# blocks land as overrides (same pattern as catalog).
KNOWN_BACKEND_KINDS: tuple[str, ...] = ("env-var", "prompt")


def publish_to(registry: Registry) -> None:
    """Publish the known secret backend kinds into the registry.

    Each entry lands as a ``SecretBackendConfig`` row, built-in
    with source ``"agentworks.secrets"``. Operator-declared
    ``[secret_backends.<kind>]`` blocks land via ``Config.publish_to``
    after this publisher runs and override the built-in rows
    (same name -> registry.add replaces). Phase 2b.2.
    """
    from agentworks.resources import Origin

    code_origin = Origin.built_in(source="agentworks.secrets")
    for kind_name in KNOWN_BACKEND_KINDS:
        registry.add(
            "secret_backend",
            kind_name,
            SecretBackendConfig(kind=kind_name),
            code_origin,
        )


__all__ = [
    "KNOWN_BACKEND_KINDS",
    "EnvVarSource",
    "PromptSource",
    "SecretBackendConfig",
    "SecretConfig",
    "SecretDecl",
    "SecretResolver",
    "SecretSource",
    "SecretSourceBase",
    "SecretTarget",
    "compute_needed_secrets",
    "env_var_name_for",
    "publish_to",
    "resolve_for_command",
]
