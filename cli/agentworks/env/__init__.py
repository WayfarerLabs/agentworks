"""Env model: EnvEntry, scope merge, AGENTWORKS_* identity vars, env composition.

Pure data with no Typer dependency. Consumers (sessions, consoles, vms,
agents) assemble effective env via ``compose_env`` and put the resulting
``dict[str, str]`` in a request's ``env`` (``agentworks.execution``), which
travels in the request envelope on the connection's standard input.
"""

from agentworks.env.compose import compose_env
from agentworks.env.entry import EnvEntry, PlaintextEnvEntry, SecretEnvEntry
from agentworks.env.identity import (
    ResourceContext,
    agentworks_identity_env,
    per_context_identity_env,
    per_user_identity_env,
    vm_stable_identity_env,
)
from agentworks.env.merge import effective_env

__all__ = [
    "EnvEntry",
    "PlaintextEnvEntry",
    "ResourceContext",
    "SecretEnvEntry",
    "agentworks_identity_env",
    "compose_env",
    "effective_env",
    "per_context_identity_env",
    "per_user_identity_env",
    "vm_stable_identity_env",
]
