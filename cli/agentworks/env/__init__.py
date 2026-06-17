"""Env model: EnvEntry, scope merge, AGENTWORKS_* identity vars, env composition.

See ``docs/sdd/2026-06-05-env-and-secrets/`` for design.

Pure data with no Typer dependency. Consumers (sessions, consoles, vms,
agents) assemble effective env via ``compose_env`` and hand the resulting
``dict[str, str]`` to the SSH layer (``ExecTarget.run(env=...)`` /
``ssh.interactive(target, command, env=...)``), which materializes one
``-o SetEnv=KEY=VALUE`` argument per entry. The remote sshd accepts these
under the ``AcceptEnv *`` directive deployed by VM init (see
``docs/adrs/0014-sshd-accept-env-wildcard.md``).
"""

from agentworks.env.compose import compose_env
from agentworks.env.entry import EnvEntry
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
    "ResourceContext",
    "agentworks_identity_env",
    "compose_env",
    "effective_env",
    "per_context_identity_env",
    "per_user_identity_env",
    "vm_stable_identity_env",
]
