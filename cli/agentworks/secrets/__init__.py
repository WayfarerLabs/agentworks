"""Secret declarations, sources, and resolver.

See ``docs/sdd/2026-06-05-env-and-secrets/`` for design.
"""

from agentworks.secrets.base import (
    SecretBackendConfig,
    SecretConfig,
    SecretDecl,
    SecretSource,
)
from agentworks.secrets.env_var import EnvVarSource, env_var_name_for
from agentworks.secrets.prompt import PromptSource
from agentworks.secrets.resolver import SecretResolver

__all__ = [
    "EnvVarSource",
    "PromptSource",
    "SecretBackendConfig",
    "SecretConfig",
    "SecretDecl",
    "SecretResolver",
    "SecretSource",
    "env_var_name_for",
]
