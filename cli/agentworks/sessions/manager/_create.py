"""``create_session``: create and start a session."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.errors import StateError

from ._create_build import _build_session_graph
from ._create_plan import _resolve_session_plan
from ._create_roll import _roll_forward

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport

    from ._create_types import SessionGraph, SessionPlan


def _reload_vm(db: Database, target_vm_name: str) -> VMRow:
    """Reload the VM row inside the gate and assert it has an address.

    The gate may have rejoined Tailscale (only when the VM was
    stopped/deallocated) and updated ``vms.tailscale_host``. The
    in-memory ``vm`` from the pre-check would otherwise read stale and
    the address check below could spuriously raise. (The pre-create
    SecretTarget read only ``vm.template``, which a refresh cannot
    change, so the pre-refresh row was safe to target; the nodes keep
    their construction row, whose identity fields a refresh cannot change
    either.)
    """
    refreshed_vm = db.get_vm(target_vm_name)
    assert refreshed_vm is not None  # existed above; the gate cannot remove it
    if refreshed_vm.tailscale_host is None:
        raise StateError(
            f"VM '{refreshed_vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=refreshed_vm.name,
        )
    return refreshed_vm


def _build_live_transport(vm: VMRow, config: Config) -> tuple[Transport, RunCommand]:
    """Build the admin SSH transport for the live VM and its run command."""
    from agentworks.ssh import SSHLogger

    logger = SSHLogger(vm.name, "session-create")
    target = _mgr.transport(vm, config, logger=logger)
    run_command: RunCommand = target.run
    return target, run_command


def _preflight_and_resolve(
    config: Config,
    *,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    target: Transport,
) -> tuple[dict[str, str], Transport | None]:
    """Run the Preflight sweep and the Resolving-Secrets boundary resolve.

    Both output sections are emitted here, in place. Names the resources
    this create touches (the session template, any ephemeral workspace /
    agent templates, the ephemeral agent's git credentials) in the same
    ``<kind>/<name>`` form vm/agent create use, then runs the readiness
    sweep and the one boundary resolve. Returns the resolved secret
    values and the existing-agent transport, which is probed here (before
    any prompt or mutation, the earlier-failure win) and is ``None`` for
    a pending or admin target.
    """
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.readiness import preflight_all

    with output.section("Preflight"):
        output.info(f"Checking session-template/{graph.template.name}...")
        if plan.new_workspace:
            assert graph.workspace_tmpl is not None  # resolved at build above
            output.info(f"Checking workspace-template/{graph.workspace_tmpl.name}...")
        if plan.new_agent:
            assert graph.agent_tmpl is not None  # resolved at build above
            output.info(f"Checking agent-template/{graph.agent_tmpl.name}...")
        if graph.agent_tmpl_node is not None:
            from agentworks.vms.initializer import announce_git_credentials

            announce_git_credentials(
                {cred.provider.owner_name: cred.provider for cred in graph.agent_tmpl_node.credentials}
            )

        # Probe direct agent SSH for an EXISTING agent before any prompt
        # or mutation: a pre-rollout agent surfaces as an actionable
        # StateError with nothing to roll back (the orchestrated flow
        # moves this probe, and the required-commands probe below, ahead
        # of the resolve boundary: the earlier-failure win). An ephemeral
        # agent's probe runs right after its realization in roll-forward.
        agent_target: Transport | None = None
        if graph.agent_node is not None and not plan.new_agent:
            from agentworks.agents.manager import _assert_agent_ssh_works
            from agentworks.transports import agent_transport

            assert plan.existing_agent is not None
            agent_target = agent_transport(vm, config, plan.existing_agent)
            _assert_agent_ssh_works(agent_target, plan.existing_agent)

        # PREFLIGHT-ALL against the one command-start context: the
        # required-commands check probes a realized (existing) agent or
        # the admin target NOW and defers on a pending one; each
        # git-credential provider predicts its token's resolvability.
        # Then the boundary resolve: the walk-away point.
        preflight_all(
            graph.nodes,
            RunContext(
                config=config,
                operation_scope=graph.scope,
                admin_target=target,
                agent_target=agent_target,
            ),
        )

    with output.section("Resolving Secrets"):
        graph.resolver.resolve()
    return graph.resolver.values, agent_target


def create_session(
    db: Database,
    config: Config,
    *,
    name: str,
    template_name: str | None = None,
    # Workspace selection (CLI-flag-shaped; service consolidates):
    workspace: str | None = None,
    new_workspace: bool = False,
    workspace_name: str | None = None,
    workspace_template: str | None = None,
    # Agent / admin selection (CLI-flag-shaped; service consolidates):
    agent: str | None = None,
    new_agent: bool = False,
    agent_name: str | None = None,
    agent_template: str | None = None,
    admin: bool = False,
    # VM anchor (validated against workspace/agent VMs when both specified):
    vm_name: str | None = None,
) -> None:
    """Create and start a session.

    Accepts the same flag combinations the ``agw session create`` CLI
    surfaces, validates them, prompts the operator for anything left
    unspecified (where interactive), and atomically provisions whichever
    ephemeral resources (workspace, agent) the operator requested
    alongside the session itself. On any failure after a mutation
    begins, every ephemeral resource created during the call is rolled
    back.

    Args:
        name: Session name.
        template_name: Session template (defaults to the operator's default).
        workspace: Existing workspace to attach this session to. Mutex
            with ``new_workspace``.
        new_workspace: When ``True``, create a new workspace.
        workspace_name: Name for the new workspace (defaults to ``name``
            when omitted). Requires ``new_workspace=True``.
        workspace_template: Template for the new workspace. Requires
            ``new_workspace=True``.
        agent: Existing agent name. Mutex with ``new_agent`` and ``admin``.
        new_agent: When ``True``, create a new agent.
        agent_name: Name for the new agent (defaults to ``name`` when
            omitted). Requires ``new_agent=True``.
        agent_template: Template for the new agent. Requires
            ``new_agent=True``.
        admin: When ``True``, run the session as the VM admin (no agent).
            Mutex with ``agent`` and ``new_agent``.
        vm_name: Target VM. Optional when an existing workspace or agent
            pins the VM; required when no other anchor does. When
            specified alongside other anchors, must agree with them.
    """
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies (e.g. typos
    # in agent template's git_credentials list, future TemplateReference
    # typos on inherits) surface as clean framework errors before any
    # flag validation, DB lookup, or ephemeral-resource creation. The
    # registry isn't yet consumed by create_session's flow (operator-env
    # secrets resolve via resolve_for_command's SecretTarget shape later),
    # but constructing it here makes the entry point's error-surface
    # consistent with create_vm / create_agent / reinit_*.
    registry = build_registry(config)

    # ===== Resolve the plan (S1-S8: flags, prompts, anchors, VM) ============
    #
    # All flag-shape validation, canonicalization, VM-anchor narrowing,
    # the workspace / mode / VM prompts, and the pure DB-existence checks
    # happen here; the settled decisions come back as a ``SessionPlan``.
    plan = _resolve_session_plan(
        db,
        name=name,
        workspace=workspace,
        new_workspace=new_workspace,
        workspace_name=workspace_name,
        workspace_template=workspace_template,
        agent=agent,
        new_agent=new_agent,
        agent_name=agent_name,
        agent_template=agent_template,
        admin=admin,
        vm_name=vm_name,
    )

    # ===== Build the node graph (S9: nodes, resolver union, scope) ==========
    #
    # Cheap and pure: template resolution, node construction, the
    # boundary secret union onto the resolver, and the operation scope.
    # No SSH, no DB writes, no secret resolution (that waits for the
    # gate).
    graph = _build_session_graph(db, config, registry=registry, plan=plan, template_name=template_name)

    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active holds: opened once, before the
    # preflight sweep (so every probe reaches a live target), held
    # through the whole command, with its just-in-time values seeding
    # the boundary resolver so nothing resolves or prompts twice.
    with activation_gate(graph.vm_node, gate_secret_resolver(config, registry, graph.resolver)):
        vm = _reload_vm(db, plan.target_vm_name)
        target, run_command = _build_live_transport(vm, config)

        secret_values, agent_target = _preflight_and_resolve(config, plan=plan, graph=graph, vm=vm, target=target)

        # ===== Dependency-ordered roll-forward (S11) ========================
        #
        # Realize the ephemerals then the session's own slice, unwinding
        # the realized ephemerals on any failure. The two-level rollback
        # (session-slice teardown, then ephemeral unwind) and the emit
        # order live in _roll_forward.
        _roll_forward(
            db,
            config,
            registry=registry,
            plan=plan,
            graph=graph,
            vm=vm,
            target=target,
            run_command=run_command,
            agent_target=agent_target,
            secret_values=secret_values,
        )
