"""Carrier dataclasses for the ``create_session`` orchestrator.

``create_session`` was a single 800-line function; it now reads as a
short sequence of named phases (resolve the plan, build the graph,
preflight and resolve secrets, roll forward). These two frozen
dataclasses are the hand-offs between those phases:

- :class:`SessionPlan` carries the pre-build decisions (S1-S8): the
  validated / prompted flag shape, the loaded anchor rows, and the
  resolved VM.
- :class:`SessionGraph` carries the built node graph and its derived
  boundary machinery (S9): the nodes, the ephemeral/template nodes, the
  resolver primed with the boundary union, and the operation scope.

There was no pre-existing carrier-object convention among the sibling
orchestrators (``create_vm`` / ``create_workspace`` / ``create_agent``
are each one function), so these are introduced local to this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.agents.nodes import (
        AgentTemplateNode,
        LiveAgentNode,
        PendingAgentNode,
    )
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.capabilities.base import OperationScope
    from agentworks.db import AgentRow, VMRow, WorkspaceRow
    from agentworks.orchestration.node import Node
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.nodes import PendingSessionNode
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import PendingWorkspaceNode
    from agentworks.workspaces.templates import (
        ResolvedTemplate as ResolvedWorkspaceTemplate,
    )


@dataclass(frozen=True, kw_only=True)
class SessionPlan:
    """The pre-build decisions for a ``create_session`` call (S1-S8).

    Every flag mutex, canonicalization, VM-anchor cross-check, and
    interactive prompt has already fired by the time this exists; the
    fields are the settled inputs the build and roll-forward consume.

    ``workspace_name`` is non-optional here: it was defaulted to the
    session name (or supplied) before the plan froze. ``agent_name`` is
    ``None`` exactly in admin mode, which :attr:`is_admin_mode` exposes.
    """

    name: str
    workspace_name: str
    new_workspace: bool
    workspace_template: str | None
    agent_name: str | None
    new_agent: bool
    agent_template: str | None
    existing_ws: WorkspaceRow | None
    existing_agent: AgentRow | None
    vm: VMRow
    target_vm_name: str

    @property
    def is_admin_mode(self) -> bool:
        """Admin mode runs the session as the VM admin, with no agent.

        Derived rather than stored so it can never drift from
        ``agent_name``: the two are the same fact, and the build / scope
        / secret-target sites all key admin mode off ``agent_name is
        None``.
        """
        return self.agent_name is None


@dataclass(frozen=True, kw_only=True)
class SessionGraph:
    """The built node graph and boundary machinery for a create (S9).

    Construction touched no secret machinery and did no SSH or DB
    writes; the :attr:`resolver` has been primed with the walk's secret
    union and the pre-create session target, and the boundary resolve
    happens later, inside the activation gate.
    """

    vm_node: LiveVMNode
    agent_node: LiveAgentNode | PendingAgentNode | None
    session_node: PendingSessionNode
    pending_workspace: PendingWorkspaceNode | None
    pending_agent: PendingAgentNode | None
    agent_tmpl_node: AgentTemplateNode | None
    workspace_tmpl: ResolvedWorkspaceTemplate | None
    agent_tmpl: ResolvedAgentTemplate | None
    resolver: Resolver
    scope: OperationScope
    template: ResolvedSessionTemplate
    nodes: tuple[Node, ...]
