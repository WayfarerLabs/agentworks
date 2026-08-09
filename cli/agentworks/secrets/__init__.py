"""Secret declarations, backends, and the resolve loop.

The capability contract and registry live in
``agentworks.capabilities.secret_backend``. This package owns declarations,
resolution, inspection, and orchestration.
"""

from __future__ import annotations

from agentworks.capabilities.secret_backend.env_var import env_var_name_for
from agentworks.secrets.base import (
    SecretConfig,
    SecretDecl,
)
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
