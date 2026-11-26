"""``create_session``: create and start a session."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateConflictError, StateError
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures

from ._create_build import _build_session_graph
from ._create_plan import _resolve_session_plan
from ._create_roll import _roll_forward

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.agents.template import AgentTemplate
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.instance_specs import InstanceOverlay
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.template import SessionTemplate
    from agentworks.workspaces.template import WorkspaceTemplate

    from ._create_types import SessionGraph, SessionPlan


def _publish_pending_plan(
    registry: Registry,
    plan: SessionPlan,
    *,
    template_name: str | None,
    session_overlay: InstanceOverlay[BaseModel] | None,
    workspace_overlay: InstanceOverlay[BaseModel] | None,
    agent_overlay: InstanceOverlay[BaseModel] | None,
) -> None:
    """Publish a compound session create as prospective live resources."""
    from typing import cast

    from agentworks.resources.live_publish import (
        project_agent_live_resource,
        project_session_live_resource,
        project_workspace_live_resource,
    )

    if plan.new_workspace:
        from agentworks.workspaces.templates import resolve_template_with_provenance as resolve_workspace

        selected = "default" if plan.workspace_template is None else plan.workspace_template
        workspace_layered = resolve_workspace(
            registry,
            selected,
            overlay=(None if workspace_overlay is None else cast("WorkspaceTemplate", workspace_overlay.declaration)),
            instance_name=plan.workspace_name,
        )
        registry.add_live(
            project_workspace_live_resource(
                name=plan.workspace_name,
                vm_name=plan.target_vm_name,
                template_name=selected,
                layered=workspace_layered,
            )
        )

    if plan.new_agent:
        from agentworks.agents.templates import resolve_template_with_provenance as resolve_agent

        assert plan.agent_name is not None
        selected = "default" if plan.agent_template is None else plan.agent_template
        agent_layered = resolve_agent(
            registry,
            selected,
            overlay=None if agent_overlay is None else cast("AgentTemplate", agent_overlay.declaration),
            instance_name=plan.agent_name,
        )
        registry.add_live(
            project_agent_live_resource(
                name=plan.agent_name,
                vm_name=plan.target_vm_name,
                template_name=selected,
                layered=agent_layered,
            )
        )

    from agentworks.sessions.templates import resolve_template_with_provenance as resolve_session

    selected = "default" if template_name is None else template_name
    session_layered = resolve_session(
        registry,
        selected,
        overlay=None if session_overlay is None else cast("SessionTemplate", session_overlay.declaration),
        instance_name=plan.name,
    )
    registry.add_live(
        project_session_live_resource(
            name=plan.name,
            workspace_name=plan.workspace_name,
            agent_name=plan.agent_name,
            template_name=selected,
            layered=session_layered,
        )
    )


def _reload_vm(db: Database, target_vm_name: str) -> VMRow:
    """Reload the VM row and require that it still exists and has an address.

    The row the plan read can be stale: an ``agw vm recover`` or
    ``vm rekey`` that finished since may have updated
    ``vms.tailscale_host``, and without the reload the address check
    below could spuriously raise. Create reloads it before building its
    graph, inside the activation gate, whose start records
    ``last_started_at``, and once more under its lock set, where it is
    compared with the plan. (The pre-create SecretTarget read only
    ``vm.template``, which a refresh cannot change, so the pre-refresh row
    was safe to target; the nodes keep their construction row, whose
    identity fields a refresh cannot change either.)
    """
    refreshed_vm = db.get_vm(target_vm_name)
    if refreshed_vm is None:
        raise StateConflictError(
            f"VM '{target_vm_name}' was deleted since this command read it",
            entity_kind="vm",
            entity_name=target_vm_name,
        )
    if refreshed_vm.tailscale_host is None:
        raise StateError(
            f"VM '{refreshed_vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=refreshed_vm.name,
        )
    return refreshed_vm


def _preflight_and_resolve(
    db: Database,
    config: Config,
    *,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    interaction: TtyInteractionPolicy,
) -> dict[str, str]:
    """Run the Preflight sweep and the Resolving-Secrets boundary resolve.

    Both output sections are emitted here, in place. Names the resources
    this create touches (the session template, any ephemeral workspace /
    agent templates, the ephemeral agent's git credentials) in the same
    ``<kind>/<name>`` form vm/agent create use, then runs the readiness
    sweep and the one boundary resolve. An existing agent's direct SSH is
    probed here, before any prompt or mutation (the earlier-failure win).
    Returns the resolved secret values.
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
            from agentworks.git_credentials import announce_git_credentials

            announce_git_credentials(graph.agent_tmpl_node.credentials)

        # Preflight precedes secret resolution, so its accesses log nothing.
        admin_access = _mgr._identity_access(vm, config, None, logger=None)
        # Probe direct agent SSH for an EXISTING agent before any prompt
        # or mutation: a pre-rollout agent surfaces as an actionable
        # StateError with nothing to roll back (the orchestrated flow
        # moves this probe, and the required-commands probe below, ahead
        # of the resolve boundary: the earlier-failure win). An ephemeral
        # agent's probe runs right after its realization in roll-forward.
        agent_access = None
        if graph.agent_node is not None and not plan.new_agent:
            from agentworks.agents.manager.access import _assert_agent_access_works

            assert plan.existing_agent is not None
            agent_access = _mgr._identity_access(vm, config, plan.existing_agent, logger=None)
            _assert_agent_access_works(agent_access, admin_access, plan.existing_agent)

        # PREFLIGHT-ALL against the one command-start context: the
        # required-commands check probes a realized (existing) agent or
        # the admin NOW and defers on a pending one, and the sweep
        # predicts each node's declared secrets (the git-credential
        # tokens here) before that node's own preflight.
        # Then the boundary resolve: the walk-away point.
        preflight_all(
            graph.nodes,
            RunContext(
                config=config,
                operation_scope=graph.scope,
                admin_access=admin_access,
                agent_access=agent_access,
            ),
            registry=graph.registry,
            interaction=interaction,
        )

        # Setup can inspect every bound owner now only if this operation
        # creates neither the workspace nor the agent. Pending owners are
        # checked once their setup has been applied, in the session slice.
        if not plan.new_workspace and not plan.new_agent:
            from agentworks.harness_setup.readiness import require_setup_ready

            assert plan.existing_ws is not None
            require_setup_ready(
                db,
                graph.registry,
                graph.session_node.harness_integration,
                vm=vm,
                workspace=plan.existing_ws,
                agent_name=plan.agent_name,
                access=agent_access or admin_access,
            )

    with output.section("Resolving Secrets"):
        graph.resolver.resolve()
    return graph.resolver.values


def create_session(
    db: Database,
    config: Config,
    *,
    name: str,
    template_name: str | None = None,
    spec: str | None = None,
    # Workspace selection (CLI-flag-shaped; service consolidates):
    workspace: str | None = None,
    new_workspace: bool = False,
    workspace_name: str | None = None,
    workspace_template: str | None = None,
    workspace_spec: str | None = None,
    # Agent / admin selection (CLI-flag-shaped; service consolidates):
    agent: str | None = None,
    new_agent: bool = False,
    agent_name: str | None = None,
    agent_template: str | None = None,
    agent_spec: str | None = None,
    admin: bool = False,
    # VM anchor (validated against workspace/agent VMs when both specified):
    vm_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> None:
    """Create and start a session.

    Accepts the same flag combinations the ``agw session create`` CLI
    surfaces, validates them, prompts the operator for anything left
    unspecified (where interactive), and provisions the requested workspace,
    agent and session. Core failures unwind newly created resources. Collected
    facet failures retain completed owners and their partial setup; explicit
    required setup blocks only its session consumer. A session can be running
    when the final collected-failure report makes the command fail.

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
    from agentworks.bootstrap import load_request_registry

    # A declaration-only registry runs first so framework miss-policies (e.g. typos
    # in agent template's git_credentials list, future TemplateReference
    # typos on inherits) surface as clean framework errors before any
    # flag validation, DB lookup, or ephemeral-resource creation. The
    # registry isn't yet consumed by create_session's flow (operator-env
    # secrets resolve via resolve_for_command's SecretTarget shape later),
    # but constructing it here makes the entry point's error-surface
    # consistent with create_vm / create_agent / reinit_*. The pending-plus-
    # durable registry built after plan resolution is authoritative for mutation.
    registry = load_request_registry(config, include_live_resources=False)

    if workspace_spec is not None and not new_workspace:
        from agentworks.errors import ValidationError

        raise ValidationError("--workspace-spec requires --new-workspace", entity_kind="session", entity_name=name)
    if agent_spec is not None and not new_agent:
        from agentworks.errors import ValidationError

        raise ValidationError("--agent-spec requires --new-agent", entity_kind="session", entity_name=name)

    from agentworks.instance_specs import parse_instance_spec, refuse_orphan_creation_state
    from agentworks.naming import validate_name
    from agentworks.sessions.tmux import MAX_SESSION_NAME_LENGTH

    session_overlay = None if spec is None else parse_instance_spec("session", spec)
    workspace_overlay = None if workspace_spec is None else parse_instance_spec("workspace", workspace_spec)
    agent_overlay = None if agent_spec is None else parse_instance_spec("agent", agent_spec)

    # The session identity is already final at the service boundary. Validate
    # it before consulting persisted orphan state, then reject that state
    # before plan resolution can prompt for workspace or mode. Ephemeral child
    # identities are not final until the plan canonicalizes/defaults them, so
    # their probes remain below.
    validate_name(name, max_length=MAX_SESSION_NAME_LENGTH)
    refuse_orphan_creation_state(db, "session", name)

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

    registry = load_request_registry(
        config,
        live_database=db,
        pending_publishers=(
            lambda target: _publish_pending_plan(
                target,
                plan,
                template_name=template_name,
                session_overlay=session_overlay,
                workspace_overlay=workspace_overlay,
                agent_overlay=agent_overlay,
            ),
        ),
    )
    from agentworks.instance_specs import ensure_effective_references_enabled
    from agentworks.resources.live import LiveResource

    pending_identities = [("session", plan.name)]
    if plan.new_workspace:
        pending_identities.append(("workspace", plan.workspace_name))
    if plan.new_agent:
        assert plan.agent_name is not None
        pending_identities.append(("agent", plan.agent_name))
    for pending_kind, pending_name in pending_identities:
        published = registry.lookup(pending_kind, pending_name)
        assert isinstance(published, LiveResource)
        ensure_effective_references_enabled(registry, published.outbound)

    # ===== Build the node graph (S9: nodes, resolver union, scope) ==========
    #
    # Cheap and pure: template resolution, node construction, the
    # boundary secret union onto the resolver, and the operation scope.
    # No SSH, no DB writes, no secret resolution (that waits for the
    # gate).
    from dataclasses import replace

    from agentworks.recheck import (
        agent_fields,
        require_request_unchanged,
        require_unchanged,
        require_vm_unchanged,
        workspace_fields,
    )

    def require_plan_rows_current() -> None:
        """The workspace and agent the plan names are as the plan read them."""
        require_unchanged(
            "workspace",
            plan.workspace_name,
            workspace_fields(plan.existing_ws),
            workspace_fields(db.get_workspace(plan.workspace_name)),
        )
        if plan.agent_name is not None:
            require_unchanged(
                "agent",
                plan.agent_name,
                agent_fields(plan.existing_agent),
                agent_fields(db.get_agent(plan.agent_name)),
            )

    def require_plan_current(current_vm: VMRow) -> None:
        """The VM, and the workspace and agent the plan names, are as the plan read them."""
        if current_vm.vm_uuid != plan.vm.vm_uuid:
            raise StateError("session VM changed during preparation", entity_kind="vm", entity_name=plan.target_vm_name)
        if current_vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
            raise StateError(
                "session parent has stopped desired state",
                entity_kind="vm",
                entity_name=current_vm.name,
            )
        require_plan_rows_current()

    current_vm = _reload_vm(db, plan.target_vm_name)
    require_plan_current(current_vm)
    plan = replace(plan, vm=current_vm)
    graph = _build_session_graph(
        db,
        config,
        registry=registry,
        plan=plan,
        template_name=template_name,
        session_overlay=session_overlay,
        workspace_overlay=workspace_overlay,
        agent_overlay=agent_overlay,
        interaction=interaction,
    )

    from agentworks.native_locks import probe_locks
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active holds: opened once, before the
    # preflight sweep (so every probe reaches a live target), held
    # through the whole command, with its just-in-time values seeding
    # the boundary resolver so nothing resolves or prompts twice.
    from agentworks.vms.manager import require_vm_ssh_boundary

    from ._intent import session_create_request

    request = session_create_request(db, plan)
    probe_locks(db.path, request)
    require_vm_ssh_boundary(db, config, graph.vm_node.row)
    with activation_gate(graph.vm_node, gate_secret_resolver(config, registry, graph.resolver)):
        vm = _reload_vm(db, plan.target_vm_name)
        # Preflight precedes secret resolution and carries no workload values.
        secret_values = _preflight_and_resolve(
            db,
            config,
            plan=plan,
            graph=graph,
            vm=vm,
            interaction=interaction,
        )

        # Every prompt is answered: the set is held from here through the
        # roll-forward and any rollback, which reuse it through the graph's VM
        # node, and what the plan read is read again under it. A new
        # workspace's target tree joins as the guest's lookup walk names it.
        from agentworks.workspaces.locks import (
            require_trees_unchanged,
            walk_trees,
            with_trees,
        )

        trees = (
            ()
            if graph.pending_workspace is None
            else (walk_trees(vm, config, [graph.pending_workspace.workspace_path]),)
        )
        with graph.vm_node.locked(with_trees(request, *trees)) as held:
            require_vm_unchanged(db, graph.vm_node.row)
            require_plan_rows_current()
            require_request_unchanged("session", plan.name, request, session_create_request(db, plan))
            for tree in trees:
                require_trees_unchanged(vm, config, tree)
            if db.get_session(plan.name) is not None:
                raise StateConflictError(
                    f"session '{plan.name}' was created since this command read it",
                    entity_kind="session",
                    entity_name=plan.name,
                    hint="Run the command again with another name.",
                )

            # ===== Dependency-ordered roll-forward (S11) ========================
            #
            # Realize the ephemerals then the session's own slice, unwinding
            # the realized ephemerals on core failures. Required setup retains
            # completed owners for repair. The two-level rollback
            # (session-slice teardown, then ephemeral unwind) and the emit
            # order live in _roll_forward. The operation log opens once its
            # redactions are known.
            setup_failures: list[SetupFailure] = []
            from agentworks.vms.manager import operation_log

            with operation_log(vm.name, "session-create", redactions=tuple(secret_values.values())) as logger:
                _roll_forward(
                    db,
                    config,
                    registry=registry,
                    plan=plan,
                    graph=graph,
                    vm=vm,
                    logger=logger,
                    secret_values=secret_values,
                    setup_failures=setup_failures,
                    held=held,
                )
            raise_setup_failures(setup_failures)
