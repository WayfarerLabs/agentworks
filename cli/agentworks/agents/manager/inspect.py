"""Agent list / describe.

The read-only half of the agents command layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks import output
from agentworks.errors import NotFoundError
from agentworks.name_filters import validate_name_filters

from ._common import MAX_AGENT_NAME_LENGTH, MAX_GRANTS_DISPLAY

if TYPE_CHECKING:
    from agentworks.agents.template import AgentTemplate
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, InstanceStateInspection
    from agentworks.instance_description import InstanceStateDescription
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry

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
    instance_state: InstanceStateDescription | None = None


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
    data: JsonObject = {
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
    if description.instance_state is not None:
        from agentworks.instance_description import instance_state_data

        agent_data = cast("JsonObject", data["agent"])
        agent_data["instance_state"] = instance_state_data(description.instance_state)
    return data


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
    """Render agent list facts with the shared table formatter.

    Emits a trailing legend line for the ``*`` marker (see below) only when
    a rendered row actually shows one. The GRANTS cell is truncated to
    ``MAX_GRANTS_DISPLAY`` before it enters the table (rather than relying
    on the table's own cap to do it), and the legend condition is read off
    those same rendered lines, never off the pre-truncation grant data: a
    marker on a late workspace in a long list can fall outside the cap, and
    when it does, the legend must not print either.
    """
    agents = listing.agents

    if names_only:
        for agent in agents:
            output.info(agent.name)
        return

    if not agents:
        output.info("No agents found.")
        return

    headers = ["NAME", "VM", "TEMPLATE", "WORKSPACE GRANTS"]
    rows: list[tuple[str, str, str, str]] = []
    for agent in agents:
        name = output.truncate(agent.name, _NAME_CELL_WIDTH)
        if agent.grant_all:
            grants = "--ALL--"
        elif not agent.grants:
            grants = "(none)"
        else:
            # Only a PURELY implicit grant gets the marker; a grant that is
            # also explicit already reads as intentional.
            parts = [f"{grant.workspace_name}{'*' if grant.grant_type == 'implicit' else ''}" for grant in agent.grants]
            grants = output.truncate(", ".join(parts), MAX_GRANTS_DISPLAY)
        rows.append((name, agent.vm_name, agent.template or "-", grants))

    # render_table takes one scalar cap for every column. It must cover the
    # largest per-column cap this view actually needs (NAME's
    # _NAME_CELL_WIDTH and GRANTS' MAX_GRANTS_DISPLAY, both already applied
    # above), or it would re-truncate an already-truncated cell below its
    # intended width.
    table_cap = max(_NAME_CELL_WIDTH, MAX_GRANTS_DISPLAY)
    lines = output.render_table(headers, rows, max_col_width=table_cap)
    for line in lines:
        output.info(line)

    data_lines = lines[2:]  # header, rule, then one line per agent
    if any("*" in line for line in data_lines):
        output.info("* granted implicitly")


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
    config: Config,
    *,
    name: str,
) -> AgentDescription:
    """Collect ordered agent detail facts without presentation."""
    with db.snapshot():
        agent = db.get_agent(name)
        if agent is None:
            raise NotFoundError(
                f"agent '{name}' not found",
                entity_kind="agent",
                entity_name=name,
            )
        from agentworks.instance_description import load_instance_description_registry

        registry = load_instance_description_registry(db, config, "agent", name)
        inspection = db.instance_state.inspect_owner_state("agent", name)
        instance_state = _agent_instance_state(registry, agent, inspection)

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
            instance_state=instance_state,
        )


def _agent_instance_state(
    registry: Registry,
    agent: AgentRow,
    inspection: InstanceStateInspection,
) -> InstanceStateDescription:
    from agentworks.agents.templates import resolve_template_with_provenance
    from agentworks.instance_description import single_declaration_instance_state
    from agentworks.resources.access import ResourceIdentity

    selection = ResourceIdentity(
        "agent-template",
        "default" if agent.template is None else agent.template,
    )
    return single_declaration_instance_state(
        instance_kind="agent",
        selection=selection,
        inspection=inspection,
        resolve=lambda overlay: resolve_template_with_provenance(
            registry,
            agent.template,
            overlay=cast("AgentTemplate | None", overlay),
            instance_name=agent.name,
        ),
    )


def render_agent_description(description: AgentDescription) -> None:
    """Render agent detail facts with the legacy human layout."""
    output.info(f"Name:       {description.name}")
    output.info(f"VM:         {description.vm_name}")
    output.info(f"Linux user: {description.linux_user}")
    output.info(f"Template:   {description.template or '-'}")
    output.info(f"Grant all:  {'yes' if description.grant_all else 'no'}")
    output.info(f"Created:    {description.created_at}")

    if description.instance_state is not None:
        from agentworks.instance_description import render_instance_state

        render_instance_state(description.instance_state)

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


def describe_agent(db: Database, config: Config, *, name: str) -> None:
    """Show detailed information about an agent."""
    render_agent_description(agent_description(db, config, name=name))
