"""Compose the rendered env for a shell-open site.

Combines AGENTWORKS_* identity vars (per-context subset only; VM-stable and
per-user subsets live in VM-side profile fragments) with the user-defined
env merged across the precedence ladder and then resolved through the
SecretResolver.

Identity vars take precedence over user-defined env on key collision: an
operator who sets AGENTWORKS_SESSION_KIND in their own env gets a load-time
warning during config validation and the value has no runtime effect. See
FRD R1 / HLA "Sites compose like this".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.env.identity import per_context_identity_env
from agentworks.env.merge import effective_env

if TYPE_CHECKING:
    from agentworks.env.entry import EnvEntry
    from agentworks.env.identity import ResourceContext
    from agentworks.secrets import SecretResolver


def compose_env(
    *,
    resolver: SecretResolver,
    ctx: ResourceContext,
    vm: dict[str, EnvEntry],
    workspace: dict[str, EnvEntry] | None = None,
    admin: dict[str, EnvEntry] | None = None,
    agent: dict[str, EnvEntry] | None = None,
    session: dict[str, EnvEntry] | None = None,
) -> dict[str, str]:
    """Assemble the rendered env dict for a shell-open site.

    Caller picks which scope env dicts apply (admin vs agent is mutually
    exclusive; workspace / session are optional). The result is a flat
    ``dict[str, str]`` ready for ``build_export_block`` or
    ``build_prefixed_command``.
    """
    identity = per_context_identity_env(ctx)
    user_env = resolver.render(
        effective_env(
            vm=vm,
            workspace=workspace,
            admin=admin,
            agent=agent,
            session=session,
        )
    )
    return {**user_env, **identity}
