"""Secret declarations, backends, and the resolve loop.

See ADR 0016 for the model: the ``[secret_config].backends`` chain
(config) names registered backend capabilities
(``SECRET_BACKEND_REGISTRY``), mirrored into the resource Registry as
read-only ``secret-backend`` descriptor rows; the resolution loop
consumes the ``SecretBackend`` API directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources import Registry


from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY
from agentworks.secrets.base import (
    SecretConfig,
    SecretDecl,
)
from agentworks.secrets.env_var import env_var_name_for
from agentworks.secrets.orchestration import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.resolve import (
    ActiveBackend,
    active_backends,
    resolve_secrets,
    validate_chain,
)


def publish_to(registry: Registry) -> None:
    """Publish the ``secret-backend`` descriptor rows -- the
    resource-registry projection of the capability registry
    (``SECRET_BACKEND_REGISTRY``), which remains the source of truth for
    the implementations themselves.
    """
    from agentworks.secrets.backends import publish_to as publish_backends

    publish_backends(registry)


__all__ = [
    "SECRET_BACKEND_REGISTRY",
    "ActiveBackend",
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
