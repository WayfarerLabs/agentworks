"""Workspace creation, description, and listing."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks import output
from agentworks.agents.grants import MAX_WORKSPACE_NAME_LENGTH
from agentworks.db.projections import project_session_mode
from agentworks.errors import AlreadyExistsError, NotFoundError
from agentworks.name_filters import validate_name_filters
from agentworks.naming import validate_name
from agentworks.workspaces.manager._common import _guard_vm_status, _resolve_vm, _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.machine_output import JsonObject
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.workspaces.template import WorkspaceTemplate

# NAME-column truncation cap for ``workspace list``, derived from the
# workspace-name cap so the two cannot drift: a valid name (<= 29) never
# truncates, and the dynamically-sized column stays bounded.
_NAME_CELL_WIDTH = MAX_WORKSPACE_NAME_LENGTH


@dataclass(frozen=True)
class WorkspaceListRow:
    name: str
    vm_name: str
    template: str | None
    created_at: str

    @classmethod
    def from_row(cls, workspace: WorkspaceRow) -> WorkspaceListRow:
        return cls(workspace.name, workspace.vm_name, workspace.template, workspace.created_at)


@dataclass(frozen=True)
class WorkspaceListing:
    """Ordered facts backing the workspace list renderers."""

    workspaces: tuple[WorkspaceListRow, ...]


@dataclass(frozen=True)
class WorkspaceSession:
    name: str
    template: str
    mode: str
    agent_name: str | None


@dataclass(frozen=True)
class WorkspaceAgent:
    name: str
    linux_user: str


@dataclass(frozen=True)
class WorkspaceDetailFacts:
    name: str
    vm_name: str
    template: str | None
    path: str
    created_at: str

    @classmethod
    def from_row(cls, workspace: WorkspaceRow) -> WorkspaceDetailFacts:
        return cls(
            workspace.name,
            workspace.vm_name,
            workspace.template,
            workspace.workspace_path,
            workspace.created_at,
        )


@dataclass(frozen=True)
class WorkspaceDescription:
    workspace: WorkspaceDetailFacts
    sessions: tuple[WorkspaceSession, ...]
    agents: tuple[WorkspaceAgent, ...]


def workspace_listing_data(listing: WorkspaceListing) -> JsonObject:
    """Project workspace list facts into the closed JSON v1 shape."""
    return {
        "workspaces": [
            {
                "name": workspace.name,
                "vm_name": workspace.vm_name,
                "template": workspace.template,
                "created_at": workspace.created_at,
            }
            for workspace in listing.workspaces
        ],
    }


def workspace_description_data(description: WorkspaceDescription) -> JsonObject:
    """Project workspace detail facts into the closed JSON v1 shape."""
    workspace = description.workspace
    return {
        "workspace": {
            "name": workspace.name,
            "vm_name": workspace.vm_name,
            "template": workspace.template,
            "path": workspace.path,
            "created_at": workspace.created_at,
            "sessions": [
                {
                    "name": session.name,
                    "template": session.template,
                    "mode": project_session_mode(session.mode),
                    "agent_name": session.agent_name,
                }
                for session in description.sessions
            ],
            "agents": [{"name": agent.name, "linux_user": agent.linux_user} for agent in description.agents],
        },
    }


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str | None = None,
    template_name: str | None = None,
    spec: str | None = None,
    open_vscode: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Create a workspace on a VM.

    Orchestrated: the graph derives from the VM's row (its site field
    is the edge to the vm-site node) and the pending workspace node's
    VM edge; the activation gate replaces this command's
    ``keep_active``, opening BEFORE the preflight sweep with its
    just-in-time values seeding the boundary resolver; the mutation is
    the phase-free realization body
    (:func:`agentworks.workspaces.realize.realize_workspace`), the
    single copy shared with the orchestrated session create. The
    completed workspace is never rollback-tracked (the body cleans its
    own partial files, and a failure after the row exists keeps the
    workspace, exactly the imperative shape), so no realization log
    exists here.
    """
    from agentworks.bootstrap import load_request_registry

    # build_registry runs first so framework miss-policies fire before
    # any template / DB / VM business logic.
    registry = load_request_registry(config, live_database=db)

    ws_name = name
    validate_name(ws_name, max_length=MAX_WORKSPACE_NAME_LENGTH)

    if db.get_workspace(ws_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{ws_name}' already exists",
            entity_kind="workspace",
            entity_name=ws_name,
        )
    from agentworks.instance_specs import refuse_orphan_creation_state

    refuse_orphan_creation_state(db, "workspace", ws_name)

    # Cheap validation FIRST, before the gate and before any secret is
    # touched: template resolution, the repo advisories (config-only,
    # no tokens), and the VM init-status guard all fail with zero
    # prompts and zero VM starts, the same bail-early precedence every
    # migrated sibling keeps.
    from agentworks.instance_specs import parse_instance_spec
    from agentworks.workspaces.templates import resolve_template_with_provenance

    overlay = None if spec is None else parse_instance_spec("workspace", spec)
    layered_template = resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlay is None else cast("WorkspaceTemplate", overlay.declaration),
        instance_name=name,
    )
    template = layered_template.value
    # Advise if the resolved template's repo remote will not resolve
    # cleanly against the declared git credentials (config-only, no
    # tokens). Each credential judges the URL by its own host/scope
    # semantics; see git_credentials.remote_advisories. Only the single
    # template actually being used is checked, and only here at use time.
    if template.repo:
        from agentworks.git_credentials import remote_advisories

        for advisory in remote_advisories(registry, template.repo):
            output.warn(advisory)

    vm = _resolve_vm(db, vm_name)
    from agentworks.resources.live_publish import project_workspace_live_resource

    pending = project_workspace_live_resource(
        name=name,
        vm_name=vm.name,
        template_name=template_name or "default",
        layered=layered_template,
    )
    registry = load_request_registry(
        config,
        live_database=db,
        pending_publishers=(lambda target: target.add_live(pending),),
    )
    from agentworks.instance_specs import ensure_effective_references_enabled

    ensure_effective_references_enabled(registry, pending.outbound)
    _guard_vm_status(vm)

    # BUILD: the command names its direct resources (this VM, the
    # chosen workspace name) and constructs the pending workspace node
    # with its VM edge attached; the walk assembles the graph.
    # Construction is cheap and touches no secret machinery; the walk
    # union below is the boundary's source. Nothing resolves yet.
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import pending_workspace_node

    resolver = Resolver(config, registry, interaction=interaction)

    vm_node = live_vm_node(db, config, registry, vm)

    pending_workspace = pending_workspace_node(
        db,
        config,
        ws_name,
        vm_node,
        template_name,
        interaction=interaction,
    )
    nodes = walk(pending_workspace)
    # The walk supplies the boundary union (the site's config secrets;
    # a workspace template's env secrets are runtime inputs, delivered
    # where sessions run, so they stay out of it: hermetic
    # provisioning, the same pin the vm-template node carries).
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = _workspace_scope(db, vm, ws_name)

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: the sweep covers every participating
        # node, then the site's config secrets resolve in one pass (or
        # arrive pre-seeded from the gate). This command has never
        # framed phases, so no banners here; the realize body never
        # frames either.
        preflight_all(
            nodes,
            RunContext(config=config, operation_scope=scope),
            registry=registry,
            interaction=interaction,
        )
        resolver.resolve()

        from agentworks.workspaces.realize import realize_workspace

        vscode_path = realize_workspace(
            db,
            config,
            registry,
            name=ws_name,
            vm=vm,
            template=template,
            overlay=overlay,
        )
        # Bookkeeping only, deliberately not via a realization log:
        # this command never unwinds a realized workspace (a failure
        # after the row exists keeps the workspace, as the imperative
        # command did), and the body already cleaned up its own
        # partial files before re-raising.
        pending_workspace.mark_realized()

        if open_vscode:
            subprocess.run(["code", str(vscode_path)], check=False)


def workspace_description(db: Database, name: str) -> WorkspaceDescription:
    """Collect the ordered workspace detail facts without presentation."""
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    sessions = tuple(
        WorkspaceSession(
            name=session.name,
            template=session.template,
            mode=project_session_mode(session.mode),
            agent_name=session.agent_name,
        )
        for session in db.list_sessions(workspace_name=name)
    )
    agents = tuple(
        WorkspaceAgent(name=agent.name, linux_user=agent.linux_user)
        for agent in db.list_agents(vm_name=ws.vm_name)
        if db.has_any_grant(agent.name, name)
    )
    return WorkspaceDescription(workspace=WorkspaceDetailFacts.from_row(ws), sessions=sessions, agents=agents)


def render_workspace_description(description: WorkspaceDescription) -> None:
    """Render workspace detail facts with the legacy human layout."""
    ws = description.workspace
    output.info(f"Name:       {ws.name}")
    output.info(f"VM:         {ws.vm_name}")
    output.info(f"Template:   {ws.template or 'default'}")
    output.info(f"Path:       {ws.path}")
    output.info(f"Created:    {ws.created_at}")

    # Sessions
    sessions = description.sessions
    output.info(f"\nSessions ({len(sessions)}):")
    if sessions:
        for s in sessions:
            mode_label = s.mode if s.mode == "unknown" else f"agent: {s.agent_name}" if s.agent_name else "admin"
            output.detail(f"{s.name}  [{s.template}]  {mode_label}")
    else:
        output.detail("(none)")

    # Agents with grants
    agents = description.agents
    output.info(f"\nAgents with access ({len(agents)}):")
    if agents:
        for agent in agents:
            output.detail(f"{agent.name}  (user: {agent.linux_user})")
    else:
        output.detail("(none)")


def describe_workspace(db: Database, name: str) -> None:
    """Show workspace details."""
    render_workspace_description(workspace_description(db, name))


def workspace_listing(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
) -> WorkspaceListing:
    """Collect the ordered workspace list facts.

    An unknown name in the VM filter raises ``NotFoundError`` rather
    than matching nothing (issue #304).

    """
    validate_name_filters(db, vm_name=vm_name)
    return WorkspaceListing(
        workspaces=tuple(WorkspaceListRow.from_row(workspace) for workspace in db.list_workspaces(vm_name=vm_name))
    )


def render_workspace_listing(listing: WorkspaceListing, *, names_only: bool = False) -> None:
    """Render workspace list facts with the legacy human layout."""
    workspaces = listing.workspaces

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No workspaces found" line below
        # is for human readers only.
        for ws in workspaces:
            output.info(ws.name)
        return

    if not workspaces:
        output.info("No workspaces found.")
        return

    def _tpl_name(t: str | None) -> str:
        if t is None or t == "(built-in)":
            return "default"
        return t

    # Cap the NAME column at the workspace-name cap (29) so an over-cap legacy
    # row cannot balloon the dynamically-sized column; a valid name never
    # truncates.
    rows = [
        (output.truncate(ws.name, _NAME_CELL_WIDTH), ws.vm_name, _tpl_name(ws.template), ws.created_at)
        for ws in workspaces
    ]

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    vm_w = max(len("VM"), max(len(r[1]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[2]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  CREATED"
    output.info(header)
    output.info("-" * len(header))
    for ws_name, ws_vm, tpl, created in rows:
        output.info(f"{ws_name:<{name_w}}  {ws_vm:<{vm_w}}  {tpl:<{tpl_w}}  {created}")


def list_workspaces(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """List workspaces with the legacy human renderer."""
    render_workspace_listing(workspace_listing(db, vm_name=vm_name), names_only=names_only)
