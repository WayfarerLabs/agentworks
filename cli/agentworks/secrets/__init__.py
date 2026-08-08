"""Secret declarations, backends, and the resolve loop.

See ADR 0016 for the model: the ``[secret_config].backends`` chain
(config) names registered backend capabilities
(``SECRET_BACKEND_REGISTRY``), mirrored into the resource Registry as
read-only ``secret-backend`` capability resources; the resolution loop
consumes the ``SecretBackend`` API directly.
"""

from __future__ import annotations

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

__all__ = [
    "SECRET_BACKEND_REGISTRY",
    "ActiveBackend",
    "SecretConfig",
    "SecretDecl",
    "SecretTarget",
    "active_backends",
    "compute_needed_secrets",
    "env_var_name_for",
    "resolve_for_command",
    "resolve_secrets",
    "validate_chain",
]
