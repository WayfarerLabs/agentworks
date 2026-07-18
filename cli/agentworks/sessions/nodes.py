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
from typing import TYPE_CHECKING, Literal

from agentworks.capabilities.base import ScopeLevel
from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.orchestration.node import Node
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import LiveWorkspaceNode, PendingWorkspaceNode

    from ..agents.nodes import LiveAgentNode, PendingAgentNode
    from .templates import ResolvedSessionTemplate

    type AgentNode = LiveAgentNode | PendingAgentNode
    type WorkspaceNode = LiveWorkspaceNode | PendingWorkspaceNode


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
        vm_name: str,
    ) -> None:
        self._session_name = session_name
        self._template_name = template_name
        self._required_commands = required_commands
        self._target = target
        self._admin = admin
        self._vm_name = vm_name
        self._probed = False

    def preflight(self, ctx: RunContext) -> None:
        self._check(ctx, stage="preflight")

    def runup(self, ctx: RunContext) -> None:
        self._check(ctx, stage="runup")

    def _check(
        self, ctx: RunContext, *, stage: Literal["preflight", "runup"]
    ) -> None:
        scope = ctx.operation_scope
        if scope is None:
            # A scope-less context reaching node readiness is an
            # orchestrator bug, not an out-of-scope level: skipping
            # here would silently disable the check forever.
            raise StateError(
                f"session '{self._session_name}': the required-commands "
                f"check received a context with no operation scope; the "
                f"orchestrator must attach one (the skip case is "
                f"out-of-scope-for-the-LEVEL, never scope-less)."
            )
        if scope.level is not ScopeLevel.SESSION:
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
        """Probe every required command with ``$SHELL -lic 'command -v
        <cmd>'``, the same shell flags the tmux pane command uses.
        Matters because PATH additions can live in any of the dotfiles
        those flags source:

        - ``-l`` (login): /etc/profile, ~/.profile, ~/.bash_profile,
          where mise activation and the agentworks profile fragments
          live.
        - ``-i`` (interactive): ~/.bashrc, ~/.zshrc, and any user PATH
          addition guarded by ``[[ $- == *i* ]]`` or ``[ -n "$PS1" ]``.
        - ``-c``: run the probe and exit.

        The probe runs over the SSH command channel without a PTY, so
        shells may emit a "no job control in this shell" warning when
        started interactive. The warning lands on stderr and doesn't
        change the exit status; the probe uses ``check=False`` so
        stderr is discarded.

        One residual gap: tools that gate PATH on ``[[ -t 0 ]]`` (real
        TTY check) won't be visible to the probe. Closing that would
        require requesting a PTY for the probe, which has its own side
        effects. PATH mutations gated on a real TTY are rare; leaving
        uncovered for now.

        Without this check, a missing binary surfaces only as a cryptic
        downstream failure: the pane command dies instantly, the fresh
        per-session tmux server exits, and the next ``server-access``
        call fails against a now-dead socket. Checking up front turns
        that into an actionable error with no partial state to roll
        back (and, at restart, with the old session still running).
        """
        missing: list[str] = []
        for cmd in self._required_commands:
            inner = f"command -v {shlex.quote(cmd)} >/dev/null 2>&1"
            probe = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
            if not probe.ok:
                missing.append(cmd)
        if not missing:
            return
        # Label parity with the imperative call sites: agent mode names
        # the agent, admin mode names the VM.
        target_label = (
            f"VM '{self._vm_name}'"
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
        db: Database,
        config: Config,
        name: str,
        check: RequiredCommandsCheck,
        agent: AgentNode | None,
        workspace: WorkspaceNode,
        vm: LiveVMNode,
    ) -> None:
        self._db = db
        self._config = config
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
                from agentworks.agents.manager import _remove_from_workspace_group

                # Re-read the VM row: the activation gate may have
                # updated its Tailscale address since node construction.
                vm_row = self._db.get_vm(self._vm.row.name) or self._vm.row
                _remove_from_workspace_group(
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
        vm_name=vm.row.name,
    )
    return PendingSessionNode(db, config, name, check, agent, workspace, vm)


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
    check = RequiredCommandsCheck(
        session_name=row.name,
        template_name=template.name,
        required_commands=tuple(template.required_commands),
        target=agent,
        admin=row.agent_name is None,
        vm_name=vm.row.name,
    )
    return LiveSessionNode(row, check, agent, workspace, vm)
