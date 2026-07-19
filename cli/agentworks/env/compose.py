"""Compose the rendered env for a shell-open site.

Combines AGENTWORKS_* identity vars (per-context subset only; VM-stable and
per-user subsets live in VM-side profile fragments) with the user-defined
env merged across the precedence ladder, secret references filled from the
command's pre-resolved values. The resulting flat ``dict[str, str]`` is
handed to the transport layer (``Transport.run(env=...)``), which coalesces
every pair into one ``-o SetEnv="K1=V1" "K2=V2" ...`` argument (see
``_set_env_args`` in ``agentworks/transports/ssh.py``); the remote sshd
accepts these under the ``AcceptEnv *`` directive deployed by VM init.

``values`` is the dict returned by the command's single
``resolve_for_command`` call. A secret referenced here but absent from it
means the eager-resolve target and this compose site drifted apart -- a
bug in the calling command, surfaced loudly rather than resolved on the
fly (there is no resolver to fall back to, by design).

Identity vars take precedence over user-defined env on key collision: an
operator who sets AGENTWORKS_SESSION_KIND in their own env gets a load-time
warning during config validation and the value has no runtime effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.env.identity import per_context_identity_env
from agentworks.env.merge import effective_env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env.entry import EnvEntry
    from agentworks.env.identity import ResourceContext


def compose_env(
    *,
    values: Mapping[str, str],
    ctx: ResourceContext,
    vm: dict[str, EnvEntry],
    workspace: dict[str, EnvEntry] | None = None,
    admin: dict[str, EnvEntry] | None = None,
    agent: dict[str, EnvEntry] | None = None,
    session: dict[str, EnvEntry] | None = None,
) -> dict[str, str]:
    """Assemble the rendered env dict for a shell-open site.

    Caller picks which scope env dicts apply (admin vs agent is mutually
    exclusive; workspace / session are optional) and passes the secret
    ``values`` its ``resolve_for_command`` call returned. The result is
    a flat ``dict[str, str]`` ready to hand to the SSH layer via
    ``env=``.
    """
    identity = per_context_identity_env(ctx)
    merged = effective_env(
        vm=vm,
        workspace=workspace,
        admin=admin,
        agent=agent,
        session=session,
    )
    user_env: dict[str, str] = {}
    for key, entry in merged.items():
        if entry.secret is not None:
            if entry.secret not in values:
                raise RuntimeError(
                    f"env var {key!r} references secret {entry.secret!r}, "
                    "which the command's eager-resolve pass did not cover. "
                    "The SecretTarget and this compose_env site must be "
                    "built from the same scope dicts (drift bug)."
                )
            user_env[key] = values[entry.secret]
        else:
            # EnvEntry invariant: exactly one of value/secret set.
            assert entry.value is not None
            user_env[key] = entry.value
    # Identity overlays user_env: AGENTWORKS_* names always reflect the
    # platform-set value when sshd injects the SetEnv'd vars into the
    # user's shell. A collision in user env has already produced
    # a load-time warning in config._parse_env_table; here we silently
    # discard it.
    return {**user_env, **identity}
