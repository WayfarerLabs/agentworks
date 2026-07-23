"""``_build_session_graph``: the pre-gate build phase of ``create_session``.

Section S9: resolve the session template, construct the derived node
graph (VM, workspace, agent, session), assemble the boundary secret
union onto the resolver (the walk union plus the pre-create session
target), and build the operation scope. All of this is cheap and pure:
no SSH, no DB writes, no secret resolution (that happens later, inside
the activation gate).

The body is the original ``create_session`` build block moved verbatim;
the plan is unpacked into the same local names the block used, so no
construction call was reordered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output

from ._create_types import SessionGraph

if TYPE_CHECKING:
    from agentworks.agents.nodes import (
        AgentTemplateNode,
        LiveAgentNode,
        PendingAgentNode,
    )
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry
    from agentworks.workspaces.nodes import (
        LiveWorkspaceNode,
        PendingWorkspaceNode,
    )
    from agentworks.workspaces.templates import (
        ResolvedTemplate as ResolvedWorkspaceTemplate,
    )

    from ._create_types import SessionPlan


def _build_session_graph(
    db: Database,
    config: Config,
    registry: Registry,
    plan: SessionPlan,
    *,
    template_name: str | None,
) -> SessionGraph:
    """Resolve the template and build the node graph, resolver union, and
    scope into a :class:`SessionGraph` (section S9)."""
    name = plan.name
    workspace_name = plan.workspace_name
    new_workspace = plan.new_workspace
    workspace_template = plan.workspace_template
    agent_name = plan.agent_name
    new_agent = plan.new_agent
    agent_template = plan.agent_template
    existing_ws = plan.existing_ws
    existing_agent = plan.existing_agent
    vm = plan.vm
    target_vm_name = plan.target_vm_name

    # ===== Template resolution (no SSH, no mutations) =======================

    template = _mgr._resolve_template(registry, template_name)

    # ===== Build: the derived node graph ====================================
    #
    # The orchestrated composition: the command names only its direct
    # resources (this VM, the chosen workspace/agent, the session
    # template) and constructs each node ONCE; everything else enters
    # through declared edges (the VM row's site field, an ephemeral
    # agent template's git_credentials), and every edge holder shares
    # the same object (the walk enforces one-object-per-key loudly).
    # Construction is cheap and touches no secret machinery; the
    # walk union below is the boundary's source. Nothing resolves
    # yet.
    from agentworks.agents.nodes import (
        agent_template_node,
        live_agent_node,
        pending_agent_node,
    )
    from agentworks.capabilities.base import (
        OperationScope,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.nodes import pending_session_node
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import (
        live_workspace_node,
        pending_workspace_node,
    )

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)

    workspace_node: LiveWorkspaceNode | PendingWorkspaceNode
    pending_workspace: PendingWorkspaceNode | None = None
    workspace_tmpl: ResolvedWorkspaceTemplate | None = None
    if new_workspace:
        # Cheap validation now, before the gate and before any secret
        # is touched: template resolution, the repo advisories
        # (config-only, no tokens), and the VM init-status guard fail
        # with zero prompts and zero VM starts, the bail-early
        # precedence every migrated command keeps.
        from agentworks.workspaces.manager import _guard_vm_status
        from agentworks.workspaces.templates import (
            resolve_template as _resolve_ws_tmpl,
        )

        workspace_tmpl = _resolve_ws_tmpl(registry, workspace_template)
        if workspace_tmpl.repo:
            from agentworks.git_credentials import remote_advisories

            for advisory in remote_advisories(registry, workspace_tmpl.repo):
                output.warn(advisory)
        _guard_vm_status(vm)
        pending_workspace = pending_workspace_node(
            db,
            config,
            workspace_name,
            vm_node,
            workspace_template,
        )
        workspace_node = pending_workspace
    else:
        assert existing_ws is not None  # loaded by the existing-workspace block
        workspace_node = live_workspace_node(existing_ws, vm_node)

    # The agent node: live (existing agent), pending (ephemeral), or
    # none (admin mode). A pending agent's declared git credentials
    # become edges through its template node: the graph replaces the
    # hand-rolled ephemeral provider fold, and the SAME agent object is
    # both the session's dep and the required-commands check's target
    # (the one-object contract), so the realization flip below is
    # observed without rewiring.
    agent_node: LiveAgentNode | PendingAgentNode | None = None
    pending_agent: PendingAgentNode | None = None
    agent_tmpl: ResolvedAgentTemplate | None = None
    agent_tmpl_node: AgentTemplateNode | None = None
    if new_agent:
        from agentworks.agents.templates import (
            resolve_template as _resolve_agent_tmpl,
        )

        assert agent_name is not None  # defaulted to ``name`` above
        agent_tmpl = _resolve_agent_tmpl(registry, agent_template)
        agent_tmpl_node = agent_template_node(registry, agent_tmpl)
        pending_agent = pending_agent_node(
            db,
            config,
            agent_name,
            agent_tmpl_node,
            vm_node,
        )
        agent_node = pending_agent
    elif agent_name is not None:
        assert existing_agent is not None  # loaded by the anchor / prompt blocks
        agent_node = live_agent_node(existing_agent, vm_node)

    session_node = pending_session_node(
        db,
        config,
        name,
        template,
        agent=agent_node,
        admin=agent_name is None,
        workspace=workspace_node,
        vm=vm_node,
    )
    nodes = walk(session_node)

    # The walk supplies the boundary union, and the session's
    # runtime env chain joins the SAME pass through the pre-create
    # SecretTarget seam, so the env-chain secrets and the graph's
    # config/token secrets stay ONE prompt session. Hermeticity is
    # unchanged: exactly what the target's env references prompts here,
    # and what rides the shells' own composition roots still does.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    resolver.register_targets(
        [
            _mgr._session_secret_target_pre_create(
                registry,
                name=name,
                workspace_name=workspace_name,
                vm=vm,
                session_template=template,
                new_workspace=new_workspace,
                workspace_template=workspace_template,
                existing_workspace=existing_ws,
                new_agent=new_agent,
                agent_template=agent_template,
                existing_agent=existing_agent,
                is_admin_mode=(agent_name is None),
            ),
        ]
    )

    scope = OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=target_vm_name,
        workspace=workspace_name,
        session=name,
        agent=agent_name,
        admin=agent_name is None,
    )

    return SessionGraph(
        vm_node=vm_node,
        workspace_node=workspace_node,
        agent_node=agent_node,
        session_node=session_node,
        pending_workspace=pending_workspace,
        pending_agent=pending_agent,
        agent_tmpl_node=agent_tmpl_node,
        workspace_tmpl=workspace_tmpl,
        agent_tmpl=agent_tmpl,
        resolver=resolver,
        scope=scope,
        template=template,
        nodes=nodes,
    )
