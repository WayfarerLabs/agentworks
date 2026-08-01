"""Agent list / describe.

The read-only half of the agents command layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import NotFoundError
from agentworks.name_filters import validate_name_filters

from ._common import MAX_AGENT_NAME_LENGTH, MAX_GRANTS_DISPLAY

if TYPE_CHECKING:
    from agentworks.db import Database

# NAME-column truncation cap for ``agent list``, derived from the agent-name
# cap so the two cannot drift: a valid name (<= 28) never truncates, and the
# column stays aligned even against an over-cap legacy row.
_NAME_CELL_WIDTH = MAX_AGENT_NAME_LENGTH


def _format_grants(db: Database, agent_name: str, grant_all: bool) -> str:
    """Format workspace grants for display in agent list."""
    if grant_all:
        return "--ALL--"

    grants = db.list_granted_workspaces_with_types(agent_name)
    if not grants:
        return "(none)"

    parts: list[str] = []
    for ws_name, has_explicit, has_implicit in grants:
        # Mark with * if implicit-only (no explicit grant)
        suffix = "*" if has_implicit and not has_explicit else ""
        parts.append(f"{ws_name}{suffix}")

    return output.truncate(", ".join(parts), MAX_GRANTS_DISPLAY)


def list_agents(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """List agents.

    An unknown name in the VM filter raises ``NotFoundError`` rather
    than matching nothing (issue #304).

    With ``names_only=True``, emit one agent name per line and skip
    the table render. Used by shell completion (see issue #147).
    """
    validate_name_filters(db, vm_name=vm_name)
    agents = db.list_agents(vm_name=vm_name)

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No agents found" line below is
        # for human readers only.
        for agent in agents:
            output.info(agent.name)
        return

    if not agents:
        output.info("No agents found.")
        return

    names = [output.truncate(agent.name, _NAME_CELL_WIDTH) for agent in agents]
    name_w = max(len("NAME"), *(len(n) for n in names))

    header = f"{'NAME':<{name_w}} {'VM':<15} {'TEMPLATE':<12} {'WORKSPACE GRANTS'}"
    output.info(header)
    output.info("-" * len(header))
    for agent, name in zip(agents, names, strict=True):
        grants = _format_grants(db, agent.name, agent.grant_all)
        output.info(f"{name:<{name_w}} {agent.vm_name:<15} {agent.template or '-':<12} {grants}")


def describe_agent(
    db: Database,
    *,
    name: str,
) -> None:
    """Show detailed information about an agent."""
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    output.info(f"Name:       {agent.name}")
    output.info(f"VM:         {agent.vm_name}")
    output.info(f"Linux user: {agent.linux_user}")
    output.info(f"Template:   {agent.template or '-'}")
    output.info(f"Grant all:  {'yes' if agent.grant_all else 'no'}")
    output.info(f"Created:    {agent.created_at}")

    # Explicit grants
    grants = db.list_granted_workspaces_with_types(name)
    explicit = [ws for ws, has_explicit, _ in grants if has_explicit]
    output.info(f"\nExplicit grants ({len(explicit)}):")
    if explicit:
        for ws in explicit:
            output.detail(ws)
    else:
        output.detail("(none)")

    # Sessions (which also show implicit grants)
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    output.info(f"\nSessions ({len(agent_sessions)}):")
    if agent_sessions:
        for s in agent_sessions:
            output.detail(f"{s.name}  [{s.template}]  workspace: {s.workspace_name}")
    else:
        output.detail("(none)")
