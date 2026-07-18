"""Session-domain node implementations.

The session node HOLDS its readiness machinery (today the
required-commands check; the harness capability instance when that
effort lands) and composes it; the machinery is ``Readiness``-only,
never walked. The headline construction contract: the factory passes
the SAME agent-node object as both the session's dependency edge and
the held machinery's ``target``, one object per node, so when the
orchestrator flips the agent realized, the watcher sees it. Two
constructions of "the same" agent would leave the check watching an
object nobody flips, deferring forever.

``RequiredCommandsCheck`` carries the four-way readiness fork the
operation scope's LEVEL makes explicit:

- out of scope for the level (a system-scoped doctor scan reaching a
  session): SKIP, legitimately, a no-op;
- in scope, target pending: DEFER to runup (the probe needs a real
  user on a real VM);
- in scope, target realized: PROBE now (the earlier-failure win for
  existing agents);
- in scope, target absent: a LOUD error (a selection bug, never a
  silent skip).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.capabilities.base import ScopeLevel
from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.db import SessionRow
    from agentworks.orchestration.node import Node
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import LiveWorkspaceNode, PendingWorkspaceNode

    from ..agents.nodes import LiveAgentNode, PendingAgentNode
    from .templates import ResolvedSessionTemplate

    AgentNode = LiveAgentNode | PendingAgentNode
    WorkspaceNode = LiveWorkspaceNode | PendingWorkspaceNode


class RequiredCommandsCheck:
    """The session's launch-target readiness: every command the
    template declares as required must exist for the user the session
    runs as. ``Readiness``-only: HELD by the session node and composed,
    never walked.

    This is the harness-LIKE stand-in the harness capability replaces
    when it lands (the check is the harness's own readiness by design;
    the fork semantics move with it, unchanged). It addresses through
    its OWN construction-time identity (``session_name``, its
    ``target`` object) and reads only the LEVEL off the operation
    scope; the scope's name fields are error framing, never addressing.

    Probes with ``$SHELL -lic 'command -v <cmd>'``, the same shell
    flags the tmux pane command uses, so PATH additions in login and
    interactive dotfiles are visible to the probe. Fires at most once
    per operation (preflight when it can, else runup).
    """

    def __init__(
        self,
        *,
        session_name: str,
        template_name: str,
        required_commands: tuple[str, ...],
        target: AgentNode | None,
        admin: bool,
    ) -> None:
        self._session_name = session_name
        self._template_name = template_name
        self._required_commands = required_commands
        self._target = target
        self._admin = admin
        self._probed = False

    def preflight(self, ctx: RunContext) -> None:
        self._check(ctx, stage="preflight")

    def runup(self, ctx: RunContext) -> None:
        self._check(ctx, stage="runup")

    def _check(self, ctx: RunContext, *, stage: str) -> None:
        scope = ctx.operation_scope
        if scope is None or scope.level is not ScopeLevel.SESSION:
            # Out of scope for the level (a system-scoped doctor scan):
            # there is legitimately no session target here; skip.
            return
        if self._probed:
            return
        if self._admin:
            transport = ctx.admin_target()
        else:
            if self._target is None:
                # Anti-silent-skip: in scope with no target is a
                # selection bug, never something to skip past.
                raise StateError(
                    f"session '{self._session_name}': no launch target "
                    f"for the required-commands check (agent mode with "
                    f"no agent node); refusing to skip it."
                )
            if not self._target.realized:
                return  # pending target: defer to runup
            transport = ctx.agent_target()
        if transport is None:
            if stage == "preflight":
                # The command-start context did not carry the target;
                # the op-start context must.
                return
            raise StateError(
                f"session '{self._session_name}': the required-commands "
                f"check reached runup with no launch target on the "
                f"context; the orchestrator must hand the op-start "
                f"context the target transport."
            )
        self._probe(transport)
        self._probed = True

    def _probe(self, transport: Transport) -> None:
        missing: list[str] = []
        for cmd in self._required_commands:
            inner = f"command -v {shlex.quote(cmd)} >/dev/null 2>&1"
            probe = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
            if not getattr(probe, "ok", False):
                missing.append(cmd)
        if not missing:
            return
        target_label = (
            "the admin user"
            if self._admin or self._target is None
            else f"agent '{self._target.name}'"
        )
        joined = ", ".join(repr(c) for c in missing)
        verb = "is" if len(missing) == 1 else "are"
        raise StateError(
            f"template '{self._template_name}' requires {joined}, which "
            f"{verb} not installed or not on PATH for {target_label}.",
            entity_kind="session",
            entity_name=self._session_name,
            hint=(
                f"Install the missing command(s) on {target_label}, or "
                "create the session with a different template "
                "(--template)."
            ),
        )


class LiveSessionNode:
    """An existing session, from its DB row: edges to its agent (or
    none in admin mode), workspace, and VM nodes; composes its held
    required-commands check (whose target, an existing agent, is
    realized, so the probe fires at preflight: the earlier-failure
    win)."""

    def __init__(
        self,
        row: SessionRow,
        check: RequiredCommandsCheck,
        agent: AgentNode | None,
        workspace: WorkspaceNode,
        vm: LiveVMNode,
    ) -> None:
        self._row = row
        self._check = check
        self._agent = agent
        self._workspace = workspace
        self._vm = vm

    @property
    def key(self) -> str:
        return f"session/{self._row.name}"

    @property
    def row(self) -> SessionRow:
        return self._row

    def deps(self) -> tuple[Node, ...]:
        deps: tuple[Node, ...] = (self._workspace, self._vm)
        if self._agent is not None:
            deps = (self._agent, *deps)
        return deps

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        self._check.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._check.runup(ctx)


class PendingSessionNode:
    """The session a create command will make: name chosen up front,
    edges attached at construction, holding its required-commands
    check whose ``target`` IS the same agent object as the dependency
    edge (the one-object contract this module's docstring pins)."""

    def __init__(
        self,
        name: str,
        check: RequiredCommandsCheck,
        agent: AgentNode | None,
        workspace: WorkspaceNode,
        vm: LiveVMNode,
    ) -> None:
        self._name = name
        self._check = check
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

    def deps(self) -> tuple[Node, ...]:
        deps: tuple[Node, ...] = (self._workspace, self._vm)
        if self._agent is not None:
            deps = (self._agent, *deps)
        return deps

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        self._check.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._check.runup(ctx)

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
        # The session's own realized artifacts (tmux server, DB row)
        # are torn down by the orchestrator's command-shaped cleanup;
        # the realizing slice lands with the session orchestrators.
        raise NotImplementedError(
            "the pending session node's teardown lands with the session "
            "orchestrators (its realizing slice defines the artifacts)"
        )


def pending_session_node(
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
    check's ``target``, by construction, so the check observes the
    orchestrator's ``mark_realized`` flip. Exactly one of ``agent`` /
    ``admin`` must be given (the session-scope invariant)."""
    if (agent is not None) == admin:
        raise StateError(
            f"session '{name}': exactly one of an agent node or "
            f"admin=True must be given (the session runs as one of them)."
        )
    check = RequiredCommandsCheck(
        session_name=name,
        template_name=template.name,
        required_commands=tuple(template.required_commands),
        target=agent,
        admin=admin,
    )
    return PendingSessionNode(name, check, agent, workspace, vm)


def live_session_node(
    row: SessionRow,
    template: ResolvedSessionTemplate,
    *,
    agent: AgentNode | None,
    workspace: WorkspaceNode,
    vm: LiveVMNode,
) -> LiveSessionNode:
    """Build the live ``session/<name>`` node from its row (admin mode
    when the row carries no agent), with the same one-object target
    wiring as the pending factory."""
    check = RequiredCommandsCheck(
        session_name=row.name,
        template_name=template.name,
        required_commands=tuple(template.required_commands),
        target=agent,
        admin=agent is None,
    )
    return LiveSessionNode(row, check, agent, workspace, vm)
