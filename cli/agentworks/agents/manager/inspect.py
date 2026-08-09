"""Agent list / describe.

The read-only half of the agents command layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import NotFoundError
from agentworks.name_filters import validate_name_filters

from ._common import MAX_AGENT_NAME_LENGTH, MAX_GRANTS_DISPLAY

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.machine_output import JsonObject

# NAME-column truncation cap for ``agent list``, derived from the agent-name
# cap so the two cannot drift: a valid name (<= 28) never truncates, and the
# column stays aligned even against an over-cap legacy row.
_NAME_CELL_WIDTH = MAX_AGENT_NAME_LENGTH


@dataclass(frozen=True)
class AgentGrant:
    workspace_name: str
    grant_type: str


@dataclass(frozen=True)
class AgentListRow:
    name: str
    vm_name: str
    template: str | None
    grant_all: bool
    grants: tuple[AgentGrant, ...]


@dataclass(frozen=True)
class AgentListing:
    agents: tuple[AgentListRow, ...]


@dataclass(frozen=True)
class AgentSession:
    name: str
    template: str
    workspace_name: str


@dataclass(frozen=True)
class AgentDescription:
    name: str
    vm_name: str
    linux_user: str
    template: str | None
    grant_all: bool
    created_at: str
    explicit_grants: tuple[str, ...]
    sessions: tuple[AgentSession, ...]


def agent_listing_data(listing: AgentListing) -> JsonObject:
    """Project agent list facts into the closed JSON v1 shape."""
    return {
        "agents": [
            {
                "name": agent.name,
                "vm_name": agent.vm_name,
                "template": agent.template,
                "grant_all": agent.grant_all,
                "grants": [
                    {"workspace_name": grant.workspace_name, "grant_type": grant.grant_type} for grant in agent.grants
                ],
            }
            for agent in listing.agents
        ],
    }


def agent_description_data(description: AgentDescription) -> JsonObject:
    """Project agent detail facts into the closed JSON v1 shape."""
    return {
        "agent": {
            "name": description.name,
            "vm_name": description.vm_name,
            "linux_user": description.linux_user,
            "template": description.template,
            "grant_all": description.grant_all,
            "created_at": description.created_at,
            "explicit_grants": list(description.explicit_grants),
            "sessions": [
                {
                    "name": session.name,
                    "template": session.template,
                    "workspace_name": session.workspace_name,
                }
                for session in description.sessions
            ],
        },
    }


def _grant_facts(db: Database, agent_name: str) -> tuple[AgentGrant, ...]:
    """Return one ordered fact for every workspace grant relationship."""
    grants: list[AgentGrant] = []
    for workspace_name, has_explicit, has_implicit in db.list_granted_workspaces_with_types(agent_name):
        if has_explicit and has_implicit:
            grant_type = "both"
        elif has_explicit:
            grant_type = "explicit"
        else:
            assert has_implicit
            grant_type = "implicit"
        grants.append(AgentGrant(workspace_name=workspace_name, grant_type=grant_type))
    return tuple(grants)


def agent_listing(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
) -> AgentListing:
    """Collect ordered agent list facts without presentation."""
    validate_name_filters(db, vm_name=vm_name)
    return AgentListing(
        agents=tuple(
            AgentListRow(
                name=agent.name,
                vm_name=agent.vm_name,
                template=agent.template,
                grant_all=agent.grant_all,
                grants=_grant_facts(db, agent.name),
            )
            for agent in db.list_agents(vm_name=vm_name)
        )
    )


def render_agent_listing(listing: AgentListing, *, names_only: bool = False) -> None:
    """Render agent list facts with the legacy human layout."""
    agents = listing.agents

    if names_only:
        for agent in agents:
            output.info(agent.name)
        return

    if not agents:
        output.info("No agents found.")
        return

    names = [output.truncate(agent.name, _NAME_CELL_WIDTH) for agent in agents]
    name_w = max(len("NAME"), *(len(name) for name in names))

    header = f"{'NAME':<{name_w}} {'VM':<15} {'TEMPLATE':<12} {'WORKSPACE GRANTS'}"
    output.info(header)
    output.info("-" * len(header))
    for agent, name in zip(agents, names, strict=True):
        if agent.grant_all:
            grants = "--ALL--"
        elif not agent.grants:
            grants = "(none)"
        else:
            parts = [f"{grant.workspace_name}{'*' if grant.grant_type == 'implicit' else ''}" for grant in agent.grants]
            grants = output.truncate(", ".join(parts), MAX_GRANTS_DISPLAY)
        output.info(f"{name:<{name_w}} {agent.vm_name:<15} {agent.template or '-':<12} {grants}")


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
    if names_only:
        validate_name_filters(db, vm_name=vm_name)
        for agent in db.list_agents(vm_name=vm_name):
            output.info(agent.name)
        return
    render_agent_listing(agent_listing(db, vm_name=vm_name))


def agent_description(
    db: Database,
    *,
    name: str,
) -> AgentDescription:
    """Collect ordered agent detail facts without presentation."""
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    grants = _grant_facts(db, name)
    sessions = tuple(
        AgentSession(name=session.name, template=session.template, workspace_name=session.workspace_name)
        for session in db.list_sessions()
        if session.agent_name == name
    )
    return AgentDescription(
        name=agent.name,
        vm_name=agent.vm_name,
        linux_user=agent.linux_user,
        template=agent.template,
        grant_all=agent.grant_all,
        created_at=agent.created_at,
        explicit_grants=tuple(grant.workspace_name for grant in grants if grant.grant_type in {"explicit", "both"}),
        sessions=sessions,
    )


def render_agent_description(description: AgentDescription) -> None:
    """Render agent detail facts with the legacy human layout."""
    output.info(f"Name:       {description.name}")
    output.info(f"VM:         {description.vm_name}")
    output.info(f"Linux user: {description.linux_user}")
    output.info(f"Template:   {description.template or '-'}")
    output.info(f"Grant all:  {'yes' if description.grant_all else 'no'}")
    output.info(f"Created:    {description.created_at}")

    # Explicit grants
    explicit = description.explicit_grants
    output.info(f"\nExplicit grants ({len(explicit)}):")
    if explicit:
        for workspace_name in explicit:
            output.detail(workspace_name)
    else:
        output.detail("(none)")

    # Sessions (which also show implicit grants)
    sessions = description.sessions
    output.info(f"\nSessions ({len(sessions)}):")
    if sessions:
        for session in sessions:
            output.detail(f"{session.name}  [{session.template}]  workspace: {session.workspace_name}")
    else:
        output.detail("(none)")


def describe_agent(db: Database, *, name: str) -> None:
    """Show detailed information about an agent."""
    render_agent_description(agent_description(db, name=name))
