"""Compose the rendered env for a shell-open site.

Combines AGENTWORKS_* identity vars (per-context subset only; VM-stable and
per-user subsets live in VM-side profile fragments) with the user-defined
env merged across the precedence ladder and then resolved through the
SecretResolver. The resulting flat ``dict[str, str]`` is handed to the SSH
layer (``ExecTarget.run(env=...)``), which coalesces every pair into one
``-o SetEnv="K1=V1" "K2=V2" ...`` argument (see ``ssh._set_env_args``);
the remote sshd accepts these under the ``AcceptEnv *`` directive
deployed by VM init.

Identity vars take precedence over user-defined env on key collision: an
operator who sets AGENTWORKS_SESSION_KIND in their own env gets a load-time
warning during config validation and the value has no runtime effect. See
FRD R1 / HLA "Env transport: SSH SetEnv".
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
    ``dict[str, str]`` ready to hand to the SSH layer via ``env=``.
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
    # Identity overlays user_env: AGENTWORKS_* names always reflect the
    # platform-set value when sshd injects the SetEnv'd vars into the
    # user's shell (FRD R1). A collision in user env has already produced
    # a load-time warning in config._parse_env_table; here we silently
    # discard it.
    return {**user_env, **identity}
