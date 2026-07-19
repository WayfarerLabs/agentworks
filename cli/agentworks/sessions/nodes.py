"""Session-domain node implementations.

The session node HOLDS its harness capability instance and composes it;
the harness is ``Readiness``-only, never walked. The headline
construction contract: the factory passes the SAME agent-node object as
both the session's dependency edge and the harness's ``target``, one
object per node, so when the orchestrator flips the agent realized, the
harness sees it. Two constructions of "the same" agent would leave the
harness watching an object nobody flips, deferring forever.

The harness owns the four-way readiness fork the operation scope's LEVEL
makes explicit (``capabilities/harness/base.py``):

- out of scope for the level (a system-scoped doctor scan reaching a
  session): SKIP, legitimately, a no-op;
- in scope, target pending: DEFER to runup (the probe needs a real
  user on a real VM);
- in scope, target realized: PROBE now (the earlier-failure win for
  existing agents);
- in scope, target absent: a LOUD error (a selection bug, never a
  silent skip).

The node is the rich consuming resource of ``capabilities/README.md``:
its ``preflight`` / ``runup`` fan into the held harness's, and the
harness's declared secrets fold into the node's ``secret_refs`` (empty
for the built-ins, plumbing present).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness import Harness
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.orchestration.node import Node
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import LiveWorkspaceNode, PendingWorkspaceNode

    from ..agents.nodes import LiveAgentNode, PendingAgentNode
    from .templates import ResolvedSessionTemplate

    type AgentNode = LiveAgentNode | PendingAgentNode
    type WorkspaceNode = LiveWorkspaceNode | PendingWorkspaceNode


class LiveSessionNode:
    """An existing session, from its DB row: edges to its agent (or
    none in admin mode), workspace, and VM nodes; composes its held
    harness (whose target, an existing agent, is realized, so the probe
    fires at preflight: the earlier-failure win)."""

    def __init__(
        self,
        row: SessionRow,
        harness: Harness,
        agent: AgentNode | None,
        workspace: WorkspaceNode,
        vm: LiveVMNode,
    ) -> None:
        self._row = row
        self._harness = harness
        self._agent = agent
        self._workspace = workspace
        self._vm = vm

    @property
    def key(self) -> str:
        return f"session/{self._row.name}"

    @property
    def row(self) -> SessionRow:
        return self._row

    @property
    def harness(self) -> Harness:
        """The held harness instance, for the op call sites (``start`` /
        ``restart``). Readiness is composed through :meth:`preflight` /
        :meth:`runup`; the op surface is driven directly by the
        service-layer operation."""
        return self._harness

    def deps(self) -> tuple[Node, ...]:
        deps: tuple[Node, ...] = (self._workspace, self._vm)
        if self._agent is not None:
            deps = (self._agent, *deps)
        return deps

    def secret_refs(self) -> tuple[str, ...]:
        return self._harness.secret_refs()

    def preflight(self, ctx: RunContext) -> None:
        self._harness.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._harness.runup(ctx)


class PendingSessionNode:
    """The session a create command will make: name chosen up front,
    edges attached at construction, holding its harness whose ``target``
    IS the same agent object as the dependency edge (the one-object
    contract this module's docstring pins)."""

    def __init__(
        self,
        db: Database,
        config: Config,
        name: str,
        harness: Harness,
        agent: AgentNode | None,
        workspace: WorkspaceNode,
        vm: LiveVMNode,
    ) -> None:
        self._db = db
        self._config = config
        self._name = name
        self._harness = harness
        self._agent = agent
        self._workspace = workspace
        self._vm = vm
        self._realized = False

    @property
    def key(self) -> str:
        return f"session/{self._name}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def harness(self) -> Harness:
        """The held harness instance, for the op call sites (``start`` /
        ``restart``). Readiness is composed through :meth:`preflight` /
        :meth:`runup`; the op surface is driven directly by the
        service-layer operation."""
        return self._harness

    def deps(self) -> tuple[Node, ...]:
        deps: tuple[Node, ...] = (self._workspace, self._vm)
        if self._agent is not None:
            deps = (self._agent, *deps)
        return deps

    def secret_refs(self) -> tuple[str, ...]:
        return self._harness.secret_refs()

    def preflight(self, ctx: RunContext) -> None:
        self._harness.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._harness.runup(ctx)

    @property
    def realized(self) -> bool:
        return self._realized

    def mark_realized(self) -> None:
        if self._realized:
            raise StateError(
                f"{self.key} was already marked realized; the "
                f"pending-to-realized flip is one-way and once."
            )
        self._realized = True

    def teardown(self) -> None:
        """Clean up the session's PARTIAL realization artifacts: the
        DB row, the implicit workspace grant, and (when no other grant
        remains) the agent's workspace-group membership.

        This is a partial-state cleaner by parity with the imperative
        session-internal rollback: it runs when the realizing slice
        fails mid-way (any of its artifacts may or may not exist yet),
        so every step is best-effort, warns on failure, and never
        raises; a raise here would mask the original error the caller
        is unwinding for. A COMPLETED session (tmux server up) is
        never torn down at all: session create's completed-session
        window is deliberately non-rollbackable, matching the
        imperative shape, so this method is only ever driven against
        partial state.
        """
        from agentworks import output

        try:
            self._db.delete_session(self._name)
        except Exception as e:
            output.warn(f"rollback: failed to delete session row '{self._name}': {e}")
        if self._agent is None:
            return
        agent_name = self._agent.name
        workspace_name = self._workspace.name
        try:
            self._db.delete_agent_grant(
                agent_name, workspace_name, "implicit", session_name=self._name
            )
            remaining = self._db.has_any_grant(agent_name, workspace_name)
        except Exception as e:
            output.warn(
                f"rollback: failed to revoke implicit grant for agent "
                f"'{agent_name}' on workspace '{workspace_name}': {e}"
            )
            return
        if not remaining:
            try:
                from agentworks.agents.grants import remove_from_workspace_group

                # Re-read the VM row: the activation gate may have
                # updated its Tailscale address since node construction.
                vm_row = self._db.get_vm(self._vm.row.name) or self._vm.row
                remove_from_workspace_group(
                    vm_row,
                    self._config,
                    self._db,
                    self._agent.linux_user,
                    workspace_name,
                    logger=None,
                )
            except Exception as e:
                output.warn(
                    f"rollback: failed to remove agent '{agent_name}' from "
                    f"workspace '{workspace_name}' group: {e}"
                )


def _shell_harness_for_template(
    template: ResolvedSessionTemplate,
    *,
    session_name: str,
    target: AgentNode | None,
    admin: bool,
    vm: LiveVMNode,
    workspace: WorkspaceNode,
) -> Harness:
    """Build the ``shell`` harness the session node holds, from the
    resolved template's still-flat ``command`` / ``restart_command`` /
    ``required_commands`` fields.

    This flat-blob adapter is the P3->P4 interim seam (plan "Named
    interim seams"): the harness is hardcoded to ``shell`` and its config
    is assembled from the flat fields here. Phase 4 reshapes the template
    to the ``(harness, harness_config)`` pair, deletes the flat fields,
    and this adapter reads ``resolved.harness`` / ``resolved.harness_config``
    directly instead.
    """
    from agentworks.capabilities.harness import harness_for

    shell_config: dict[str, object] = {
        "command": template.command,
        "restart_command": template.restart_command,
        "required_commands": list(template.required_commands),
    }
    return harness_for("shell")(
        template.name,
        shell_config,
        session_name=session_name,
        vm_name=vm.row.name,
        workspace_name=workspace.name,
        target=target,
        admin=admin,
    )


def pending_session_node(
    db: Database,
    config: Config,
    name: str,
    template: ResolvedSessionTemplate,
    *,
    agent: AgentNode | None,
    admin: bool,
    workspace: WorkspaceNode,
    vm: LiveVMNode,
) -> PendingSessionNode:
    """Build the pending ``session/<name>`` node.

    ``agent`` (or ``admin=True``) is the launch identity: the SAME
    object is wired as the session's dependency edge AND as the held
    harness's ``target``, by construction, so the harness observes the
    orchestrator's ``mark_realized`` flip. Exactly one of ``agent`` /
    ``admin`` must be given (the session-scope invariant)."""
    if (agent is not None) == admin:
        raise StateError(
            f"session '{name}': exactly one of an agent node or "
            f"admin=True must be given (the session runs as one of them)."
        )
    harness = _shell_harness_for_template(
        template,
        session_name=name,
        target=agent,
        admin=admin,
        vm=vm,
        workspace=workspace,
    )
    return PendingSessionNode(db, config, name, harness, agent, workspace, vm)


def live_session_node(
    row: SessionRow,
    template: ResolvedSessionTemplate,
    *,
    agent: AgentNode | None,
    workspace: WorkspaceNode,
    vm: LiveVMNode,
) -> LiveSessionNode:
    """Build the live ``session/<name>`` node from its row, with the
    same one-object target wiring as the pending factory.

    Admin mode comes from the ROW'S word (``agent_name`` is null),
    never from the ``agent`` argument's absence: inferring admin from a
    missing argument would structurally disable the fork's loud branch
    (an agent-mode row handed no agent node would silently probe the
    admin user instead of raising). The factory cross-checks both
    directions and raises on mismatch."""
    if row.agent_name is not None:
        if agent is None:
            raise StateError(
                f"session '{row.name}' runs as agent "
                f"'{row.agent_name}' but no agent node was handed to "
                f"the factory; refusing to fall back to admin mode."
            )
        if agent.name != row.agent_name:
            raise StateError(
                f"session '{row.name}' runs as agent "
                f"'{row.agent_name}' but the handed agent node is "
                f"'{agent.name}'; the row's word and the graph must "
                f"agree."
            )
    elif agent is not None:
        raise StateError(
            f"session '{row.name}' is an admin session but an agent "
            f"node ('{agent.name}') was handed to the factory."
        )
    harness = _shell_harness_for_template(
        template,
        session_name=row.name,
        target=agent,
        admin=row.agent_name is None,
        vm=vm,
        workspace=workspace,
    )
    return LiveSessionNode(row, harness, agent, workspace, vm)
