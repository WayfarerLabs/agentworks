"""Secret-backend capability contract, built-ins, and class registry."""

from __future__ import annotations

from agentworks.capabilities.secret_backend.base import (
    SECRET_BACKEND_REGISTRY,
    InteractionChannel,
    SecretBackend,
)
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretClientTimeout,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.capabilities.secret_backend.env_var import (
    EnvVarBackend,
    EnvVarMapping,
    EnvVarSourceConfig,
    env_var_name_for,
)
from agentworks.capabilities.secret_backend.prompt import (
    PromptBackend,
    PromptMapping,
    PromptSourceConfig,
)

__all__ = [
    "SECRET_BACKEND_REGISTRY",
    "EnvVarBackend",
    "EnvVarMapping",
    "EnvVarSourceConfig",
    "InteractionBroker",
    "InteractionChannel",
    "PromptBackend",
    "PromptMapping",
    "PromptSourceConfig",
    "RemainingTime",
    "SecretBackend",
    "SecretBackendEntry",
    "SecretClientFailure",
    "SecretClientFailureKind",
    "SecretClientRemediation",
    "SecretClientTimeout",
    "SecretLookupRequest",
    "SecretSourceClient",
    "env_var_name_for",
]


def __getattr__(name: str) -> object:
    """Load the resource-row export without pulling resources into leaves."""
    if name == "SecretBackendEntry":
        from agentworks.capabilities.secret_backend.kinds import SecretBackendEntry

        return SecretBackendEntry
    raise AttributeError(name)
