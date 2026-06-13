"""Env model: EnvEntry, scope merge, AGENTWORKS_* identity vars, export-block helpers.

See ``docs/sdd/2026-06-05-env-and-secrets/`` for design.

Pure data with no Typer dependency. Consumers (sessions, consoles, vms,
agents) assemble effective env via ``effective_env`` and ``agentworks_identity_env``
then hand the result to ``build_export_block`` or ``build_prefixed_command`` to
produce the shell prelude any shell-opening site can prepend.
"""

from agentworks.env.entry import EnvEntry
from agentworks.env.exports import build_export_block, build_prefixed_command
from agentworks.env.identity import ResourceContext, agentworks_identity_env
from agentworks.env.merge import effective_env

__all__ = [
    "EnvEntry",
    "ResourceContext",
    "agentworks_identity_env",
    "build_export_block",
    "build_prefixed_command",
    "effective_env",
]
