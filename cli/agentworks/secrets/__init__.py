"""Secret declarations, backends, and the resolve loop.

Backends are the door: see ADR 0016 for the model (config chain ->
backend resources -> provider capabilities).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources import Registry


from agentworks.secrets.base import (
    SecretBackendDecl,
    SecretConfig,
    SecretDecl,
)
from agentworks.secrets.env_var import env_var_name_for
from agentworks.secrets.orchestration import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.providers import PROVIDER_REGISTRY
from agentworks.secrets.resolve import (
    active_backends,
    resolve_secrets,
    validate_chain,
)


def publish_to(registry: Registry) -> None:
    """Publish the ``secret-provider`` descriptor rows.

    The built-in BACKEND rows ship as bundled manifests
    (``manifests/builtin/secret-backends.yaml``); this publisher
    contributes the provider descriptors that backend ``provider``
    references resolve against -- the resource-registry projection of
    the capability registry (``PROVIDER_REGISTRY``), which remains the
    source of truth for the implementations themselves.
    """
    from agentworks.secrets.providers import publish_to as publish_providers

    publish_providers(registry)


__all__ = [
    "PROVIDER_REGISTRY",
    "SecretBackendDecl",
    "SecretConfig",
    "SecretDecl",
    "SecretTarget",
    "active_backends",
    "compute_needed_secrets",
    "env_var_name_for",
    "publish_to",
    "resolve_for_command",
    "resolve_secrets",
    "validate_chain",
]
