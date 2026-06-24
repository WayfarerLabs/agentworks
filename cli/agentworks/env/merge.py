"""Scope merge for env entries.

Implements the precedence ladder defined in FRD R2:

    session > (agent | admin) > workspace > vm

Exactly one of admin / agent applies to any given shell because a shell runs
as one Linux user. The caller decides which by passing the appropriate
argument as non-None; passing both is a programmer error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.env.entry import EnvEntry


def effective_env(
    *,
    vm: dict[str, EnvEntry],
    workspace: dict[str, EnvEntry] | None = None,
    admin: dict[str, EnvEntry] | None = None,
    agent: dict[str, EnvEntry] | None = None,
    session: dict[str, EnvEntry] | None = None,
) -> dict[str, EnvEntry]:
    """Merge env dicts low-to-high, returning the effective per-key map.

    A shell runs as exactly one Linux user, so exactly one of ``admin`` /
    ``agent`` must be non-None per call. Passing both is a programmer error
    (the caller should know which mode the shell belongs to).

    Empty / None scopes contribute nothing. Later scopes overwrite earlier
    scopes on key collision; entries appearing only at one scope pass through.
    """
    if admin is not None and agent is not None:
        raise ValueError("effective_env: pass exactly one of admin / agent, not both")

    merged: dict[str, EnvEntry] = dict(vm)
    if workspace:
        merged.update(workspace)
    if agent:
        merged.update(agent)
    elif admin:
        merged.update(admin)
    if session:
        merged.update(session)
    return merged
