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
from agentworks.secrets.providers import PROVIDER_REGISTRY, resolver_for
from agentworks.secrets.resolver import SecretResolver

# The RESERVED built-in backend names: operator manifests may not
# redeclare them (enforced at ManifestSet.publish_to), and legacy TOML
# [secret_backends.<kind>] sections accept only these kinds. The rows
# themselves ship as bundled manifests; the publisher below contributes
# the provider descriptors.
KNOWN_BACKEND_KINDS: tuple[str, ...] = ("env-var", "prompt")


def publish_to(registry: Registry) -> None:
    """Publish the ``secret-provider`` descriptor rows.

    Phase 3 of the resource-manifests SDD: the built-in BACKEND rows
    moved to the bundled manifests (``manifests/builtin/
    secret-backends.yaml``); this publisher now contributes the
    provider descriptors that backend ``provider`` references resolve
    against. Operator-declared ``[secret_backends.<kind>]`` TOML blocks
    still land via ``Config.publish_to`` and override the bundled rows
    until the cutover.
    """
    from agentworks.secrets.providers import publish_to as publish_providers

    publish_providers(registry)


__all__ = [
    "KNOWN_BACKEND_KINDS",
    "PROVIDER_REGISTRY",
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
    "resolver_for",
]
