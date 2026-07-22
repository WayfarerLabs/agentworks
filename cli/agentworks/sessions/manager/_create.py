"""``create_session``: create and start a session."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import SessionMode
from agentworks.errors import (
    AlreadyExistsError,
    NotFoundError,
    StateError,
    ValidationError,
)

if TYPE_CHECKING:
    from agentworks.agents.nodes import (
        AgentTemplateNode,
        LiveAgentNode,
        PendingAgentNode,
    )
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport
    from agentworks.workspaces.nodes import (
        LiveWorkspaceNode,
        PendingWorkspaceNode,
    )
    from agentworks.workspaces.templates import (
        ResolvedTemplate as ResolvedWorkspaceTemplate,
    )


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
    from agentworks.config import validate_name
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    # build_registry runs first so framework miss-policies (e.g. typos
    # in agent template's git_credentials list, future TemplateReference
    # typos on inherits) surface as clean framework errors before any
    # flag validation, DB lookup, or ephemeral-resource creation. The
    # registry isn't yet consumed by create_session's flow (operator-env
    # secrets resolve via resolve_for_command's SecretTarget shape later),
    # but constructing it here makes the entry point's error-surface
    # consistent with create_vm / create_agent / reinit_*.
    registry = build_registry(config)

    # ===== Flag-shape validation (mutexes + ephemeral-arg gating) ===========

    if workspace and new_workspace:
        raise ValidationError(
            "specify --workspace or --new-workspace, not both",
            entity_kind="session",
            entity_name=name,
        )
    if not new_workspace and (workspace_name or workspace_template):
        raise ValidationError(
            "--workspace-name and --workspace-template require --new-workspace",
            entity_kind="session",
            entity_name=name,
        )
    agent_modes = sum(1 for x in (bool(agent), new_agent, admin) if x)
    if agent_modes > 1:
        raise ValidationError(
            "specify at most one of --agent, --new-agent, or --admin",
            entity_kind="session",
            entity_name=name,
        )
    if not new_agent and (agent_name or agent_template):
        raise ValidationError(
            "--agent-name and --agent-template require --new-agent",
            entity_kind="session",
            entity_name=name,
        )

    # ===== Canonicalize CLI-flag shape into internal form ===================
    #
    # After this block:
    #   workspace_name : str | None   -- the workspace's name (None until
    #                                    DB lookup / default-to-session-name)
    #   new_workspace  : bool         -- True iff we're creating it
    #   workspace_template : str | None
    #   agent_name : str | None       -- the agent's name (None == admin mode)
    #   new_agent  : bool
    #   agent_template : str | None
    #
    # ``workspace`` / ``agent`` / ``admin`` are consumed here and unused below.

    if workspace:
        workspace_name = workspace
    if agent:
        agent_name = agent
    if admin:
        agent_name = None
        new_agent = False

    # ===== Early VM-anchor narrowing for the workspace prompt ===============
    #
    # If ``--vm`` and/or ``--agent`` were specified, they already pin a VM.
    # Load the agent row now (rather than in the later VM-anchor block) so
    # we can:
    #   1. Cross-check ``--vm`` against the agent's VM before any prompt
    #      fires (no point prompting for a workspace when we know the
    #      command is inconsistent).
    #   2. Filter the workspace chooser to workspaces on the known VM,
    #      so the operator doesn't have to mentally exclude irrelevant
    #      entries (and so picking one on the wrong VM isn't reachable).
    existing_agent: AgentRow | None = None
    known_vm: str | None = vm_name
    if not new_agent and agent_name is not None:
        existing_agent = db.get_agent(agent_name)
        if existing_agent is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        if known_vm is not None and known_vm != existing_agent.vm_name:
            raise ValidationError(
                f"VM mismatch: --vm={known_vm}, agent '{agent_name}'={existing_agent.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_agent.vm_name

    # ===== Workspace prompt (force explicit choice even with one option) ===
    #
    # No auto-select: workspace is part of the session's identity, and a
    # single-workspace "shortcut" today silently changes behavior the day
    # the operator adds a second one. Always prompt. Include a
    # ``[Create new]`` option so the interactive UX is fully equivalent
    # to passing ``--new-workspace`` on the CLI. Filter to ``known_vm``
    # when other anchors pin one. Non-interactive: raise.

    if not workspace_name and not new_workspace:
        chosen_existing, new_workspace = _mgr._prompt_workspace_choice(db, known_vm)
        if chosen_existing is not None:
            workspace_name = chosen_existing

    # ===== Pure validation (no SSH, no mutations) ===========================

    # Default ephemeral resource names to the session name when omitted.
    if new_workspace and workspace_name is None:
        workspace_name = name
    if new_agent and agent_name is None:
        agent_name = name
    assert workspace_name is not None  # invariant after canonicalize + prompt

    validate_name(name)
    if new_workspace:
        validate_name(workspace_name)
    if new_agent:
        assert agent_name is not None
        validate_name(agent_name)

    # DB existence checks. Session must not exist. Ephemeral workspace /
    # agent must not exist; existing workspace / agent must exist.
    if db.get_session(name) is not None:
        raise AlreadyExistsError(
            f"session '{name}' already exists",
            entity_kind="session",
            entity_name=name,
        )
    if new_workspace and db.get_workspace(workspace_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{workspace_name}' already exists",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if new_agent:
        assert agent_name is not None  # defaulted to ``name`` above
        if db.get_agent(agent_name) is not None:
            raise AlreadyExistsError(
                f"agent '{agent_name}' already exists",
                entity_kind="agent",
                entity_name=agent_name,
            )

    # ===== Existing-workspace lookup + VM-anchor accretion =================
    #
    # If the operator named an existing workspace, load it now -- both
    # to validate it exists and to contribute its VM to ``known_vm``
    # before the mode prompt fires. This lets the mode prompt filter
    # agents by the workspace's VM, and lets a downstream VM mismatch
    # surface before the mode prompt rather than after.
    existing_ws: WorkspaceRow | None = None
    if not new_workspace:
        existing_ws = db.get_workspace(workspace_name)
        if existing_ws is None:
            raise NotFoundError(
                f"workspace '{workspace_name}' not found",
                entity_kind="workspace",
                entity_name=workspace_name,
            )
        if known_vm is not None and known_vm != existing_ws.vm_name:
            anchor_label = "--vm" if vm_name is not None else f"agent '{agent_name}'"
            raise ValidationError(
                f"VM mismatch: {anchor_label}={known_vm}, workspace '{workspace_name}'={existing_ws.vm_name}",
                entity_kind="session",
                entity_name=name,
            )
        known_vm = existing_ws.vm_name

    # ===== Mode prompt (force explicit choice; no silent default) ==========
    #
    # Fires before VM resolution so the operator's pick of an existing
    # agent can pin the VM (one less prompt for the common case). When
    # ``known_vm`` is set, the chooser filters to that VM's agents; when
    # not, it shows agents across all VMs (each labeled with its VM) and
    # picking one sets the VM. ``admin`` and ``[Create new agent]`` don't
    # pin a VM -- those paths fall through to the VM-prompt at the end.

    if agent_name is None and not new_agent and not admin:
        vm_for_mode_prompt: VMRow | None = None
        if known_vm is not None:
            vm_for_mode_prompt = db.get_vm(known_vm)
            assert vm_for_mode_prompt is not None  # known_vm was sourced from a real row

        chosen_agent, new_agent, admin = _mgr._prompt_mode_choice(db, vm_for_mode_prompt)
        if chosen_agent is not None:
            # Existing-agent pick: the prompt already filtered by
            # ``known_vm`` (if set) OR the picked agent's VM becomes
            # the new known_vm. No vm-anchor cross-check needed -- the
            # filter / pick path enforces agreement by construction.
            agent_name = chosen_agent
            existing_agent = db.get_agent(agent_name)
            assert existing_agent is not None  # came from list_agents
            known_vm = existing_agent.vm_name

        # Re-run the agent-specific default / validation / existence
        # checks that the upfront block did for the flag path. The
        # workspace equivalents ran already because the workspace
        # prompt sits BEFORE that block; the mode prompt sits AFTER
        # because it may need to pin the VM via an existing-agent
        # pick. Without this, a ``[Create new agent]`` pick lands at
        # the eager-resolve SecretTarget with ``is_admin_mode=True``
        # (wrong scope) and asserts ``agent_name is not None``
        # inside the ephemeral-create block.
        if new_agent:
            if agent_name is None:
                agent_name = name
            validate_name(agent_name)
            if db.get_agent(agent_name) is not None:
                raise AlreadyExistsError(
                    f"agent '{agent_name}' already exists",
                    entity_kind="agent",
                    entity_name=agent_name,
                )

    # ===== VM resolution (final step; prompts only if nothing pinned it) ===
    #
    # By this point every anchor (vm_name, existing workspace, existing
    # agent -- whether passed as a flag or picked from a prompt) has
    # contributed to ``known_vm`` and the cross-checks fired as each
    # anchor was loaded. If ``known_vm`` is still ``None`` we genuinely
    # have no anchor (e.g. ``--new-workspace --admin`` with no ``--vm``),
    # so prompt for VM. The cross-check below is defense-in-depth: a
    # future refactor that adds a new anchor without piping it through
    # ``known_vm`` would trip it.
    if known_vm is None:
        vm = _mgr._prompt_vm(db)
    else:
        loaded_vm = db.get_vm(known_vm)
        if loaded_vm is None:
            raise NotFoundError(
                f"VM '{known_vm}' not found",
                entity_kind="vm",
                entity_name=known_vm,
            )
        vm = loaded_vm
    target_vm_name = vm.name

    vm_anchors: list[tuple[str, str]] = []
    if vm_name is not None:
        vm_anchors.append(("--vm", vm_name))
    if existing_ws is not None:
        vm_anchors.append((f"workspace '{workspace_name}'", existing_ws.vm_name))
    if existing_agent is not None:
        vm_anchors.append((f"agent '{agent_name}'", existing_agent.vm_name))
    if any(candidate != target_vm_name for _, candidate in vm_anchors):
        detail = ", ".join(f"{src}={v}" for src, v in vm_anchors)
        raise ValidationError(
            f"VM mismatch: {detail}",
            entity_kind="session",
            entity_name=name,
        )

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
        credential_tokens,
        live_agent_node,
        pending_agent_node,
    )
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.unwind import RealizationLog
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

    # The activation gate replaces this command's imperative
    # ensure_active + vm_active holds: opened once, before the
    # preflight sweep (so every probe reaches a live target), held
    # through the whole command, with its just-in-time values seeding
    # the boundary resolver so nothing resolves or prompts twice.
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # Reload the VM row: the gate may have rejoined Tailscale (only
        # when the VM was stopped/deallocated) and updated
        # ``vms.tailscale_host``. The in-memory ``vm`` from our pre-check
        # would otherwise read stale and the check below could spuriously
        # raise. (The SecretTarget above read only vm.template, which a
        # refresh cannot change, so the pre-refresh row was safe to
        # target; the nodes keep their construction row, whose identity
        # fields a refresh cannot change either.)
        refreshed_vm = db.get_vm(target_vm_name)
        assert refreshed_vm is not None  # existed above; the gate cannot remove it
        vm = refreshed_vm
        if vm.tailscale_host is None:
            raise StateError(
                f"VM '{vm.name}' has no Tailscale address",
                entity_kind="vm",
                entity_name=vm.name,
            )

        from agentworks.ssh import SSHLogger

        logger = SSHLogger(vm.name, "session-create")
        target = _mgr.transport(vm, config, logger=logger)
        run_command: RunCommand = target.run

        # Preflight phase: name the resources this create touches (the
        # session template, any ephemeral workspace / agent templates, and
        # the ephemeral agent's git credentials) in the same
        # <kind>/<name> form vm/agent create use, then run the readiness
        # sweep. Framed as a phase so session create reads like a plan
        # executing, matching vm create.
        with output.section("Preflight"):
            output.info(f"Checking session-template/{template.name}...")
            if new_workspace:
                assert workspace_tmpl is not None  # resolved at build above
                output.info(f"Checking workspace-template/{workspace_tmpl.name}...")
            if new_agent:
                assert agent_tmpl is not None  # resolved at build above
                output.info(f"Checking agent-template/{agent_tmpl.name}...")
            if agent_tmpl_node is not None:
                from agentworks.vms.initializer import announce_git_credentials

                announce_git_credentials(
                    {cred.provider.owner_name: cred.provider for cred in agent_tmpl_node.credentials}
                )

            # Probe direct agent SSH for an EXISTING agent before any
            # prompt or mutation: a pre-rollout agent surfaces as an
            # actionable StateError with nothing to roll back (the
            # orchestrated flow moves this probe, and the required-commands
            # probe below, ahead of the resolve boundary: the
            # earlier-failure win). An ephemeral agent's probe runs right
            # after its realization below.
            agent_target: Transport | None = None
            if agent_node is not None and not new_agent:
                from agentworks.agents.manager import _assert_agent_ssh_works
                from agentworks.transports import agent_transport

                assert existing_agent is not None
                agent_target = agent_transport(vm, config, existing_agent)
                _assert_agent_ssh_works(agent_target, existing_agent)

            # PREFLIGHT-ALL against the one command-start context: the
            # required-commands check probes a realized (existing) agent or
            # the admin target NOW and defers on a pending one; each
            # git-credential provider predicts its token's resolvability.
            # Then the boundary resolve: the walk-away point.
            preflight_all(
                nodes,
                RunContext(
                    config=config,
                    operation_scope=scope,
                    admin_target=target,
                    agent_target=agent_target,
                ),
            )

        with output.section("Resolving Secrets"):
            resolver.resolve()
        secret_values = resolver.values

        def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(secret_values, secret_names),
            )

        # ===== Dependency-ordered roll-forward ==============================
        #
        # Realize the pending nodes in dependency order, recording each
        # completed realization; on any later failure the log unwinds
        # them in reverse (agent before workspace, today's proven
        # rollback order). The session's own partial state is cleaned by
        # its node's teardown in the slice below, and a COMPLETED
        # session (tmux up) is deliberately never rolled back.
        log = RealizationLog()
        try:
            # ---- Ephemeral realizations (each its own plan stage) ----------
            if pending_workspace is not None:
                from agentworks.workspaces.realize import realize_workspace

                assert workspace_tmpl is not None  # resolved at build above
                # The realizer emits its own "Creating workspace ..." line
                # (used by the standalone `workspace create` path too), so the
                # session flow must not echo it a second time here.
                with output.section("Creating Workspace"):
                    realize_workspace(
                        db,
                        config,
                        registry,
                        name=workspace_name,
                        vm=vm,
                        template=workspace_tmpl,
                    )
                    log.mark_realized(pending_workspace)
            if pending_agent is not None:
                from agentworks.agents.realize import realize_agent

                assert agent_name is not None  # defaulted to ``name`` above
                assert agent_tmpl is not None and agent_tmpl_node is not None
                with output.section("Creating Agent"):
                    output.info(f"Creating agent '{agent_name}' on VM '{vm.name}' (template: {agent_tmpl.name})...")
                    # Each credential's token, read through its node's
                    # SCOPED delivery (the boundary pass above covered
                    # them; the graph-derived fold replaces the nested
                    # create_agent's git_tokens hand-off).
                    git_tokens = credential_tokens(agent_tmpl_node, scoped_ctx)
                    realize_agent(
                        db,
                        config,
                        registry,
                        name=agent_name,
                        vm=vm,
                        template=agent_tmpl,
                        git_tokens=git_tokens,
                    )
                    log.mark_realized(pending_agent)

            # ---- The session's own realizing slice -------------------------
            ws = _mgr._require_workspace(db, workspace_name)

            resolved_agent_name: str | None = None
            agent_row: AgentRow | None = None
            if agent_name is not None:
                mode = SessionMode.AGENT
                agent_row = db.get_agent(agent_name)
                if agent_row is None:
                    raise NotFoundError(
                        f"agent '{agent_name}' not found",
                        entity_kind="agent",
                        entity_name=agent_name,
                    )
                # Unreachable in practice: existing-agent VM was already
                # cross-checked in the upfront anchor block, and a fresh
                # ephemeral agent was just created on this same VM. Kept as
                # a tripwire so a future refactor that reorders or drops the
                # upfront check fails loudly rather than silently corrupting
                # cross-VM state.
                if agent_row.vm_name != vm.name:
                    raise ValidationError(
                        f"agent '{agent_name}' is on VM '{agent_row.vm_name}', "
                        f"but workspace '{workspace_name}' is on VM '{vm.name}'",
                        entity_kind="session",
                        entity_name=name,
                    )
                linux_user = agent_row.linux_user
                resolved_agent_name = agent_name
                if agent_target is None:
                    # The ephemeral agent just realized: probe its direct
                    # SSH BEFORE any session mutation, same contract as
                    # the existing-agent probe above.
                    from agentworks.agents.manager import _assert_agent_ssh_works
                    from agentworks.transports import agent_transport

                    agent_target = agent_transport(vm, config, agent_row)
                    _assert_agent_ssh_works(agent_target, agent_row)
            else:
                mode = SessionMode.ADMIN
                linux_user = vm.admin_username

            # Op-start runup: the required-commands check probes a
            # just-realized ephemeral agent here (it deferred at
            # preflight; the log's flip above is what it observed). For
            # targets that were realized at preflight the check already
            # fired and this is a no-op.
            session_node.runup(
                RunContext(
                    config=config,
                    operation_scope=scope,
                    admin_target=target,
                    agent_target=agent_target,
                )
            )

            # Compute socket path up front (deterministic from linux_user +
            # session name). Needed for the DB insert since the CHECK
            # constraint requires agent sessions to have a socket_path.
            expected_socket: str | None = None
            if mode == SessionMode.AGENT:
                from agentworks.sessions.tmux import agent_socket_path

                expected_socket = agent_socket_path(linux_user, name)

            mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
            with output.section("Starting Session"):
                output.info(
                    f"Starting session '{name}' on workspace '{workspace_name}' "
                    f"({mode_label}, template: {template.name})..."
                )

                try:
                    # Everything that creates partial session state (on-VM group
                    # membership, implicit-grant row, session row, restricted-config
                    # write, tmux session) runs inside this block so a KI /
                    # exception anywhere here triggers the session node's
                    # partial-state teardown.
                    if resolved_agent_name is not None:
                        # Auto-grant implicit workspace access if the agent has no
                        # existing grant on this workspace.
                        if not db.has_any_grant(resolved_agent_name, workspace_name):
                            from agentworks.agents.grants import add_to_workspace_group

                            add_to_workspace_group(vm, config, db, linux_user, workspace_name)
                        db.insert_agent_grant(resolved_agent_name, workspace_name, "implicit", session_name=name)

                    # Op-start RunContext for the harness's start op: mirrors
                    # the runup readiness ctx above (targets), plus the scoped
                    # secrets (the session node's declared union, empty for the
                    # built-in shell harness; ScopedSecrets never delivers).
                    # Template-var substitution lifts OUT of the harness and
                    # wraps its returned string. The op runs BEFORE the insert
                    # so a freshly minted harness_state (claude-code's session
                    # id) lands with the new row; it does only read-only work
                    # (a login-shell string for shell, a find probe for
                    # claude-code), so it stays ahead of any tmux mutation.
                    start_ctx = RunContext(
                        config=config,
                        operation_scope=scope,
                        admin_target=target,
                        agent_target=agent_target,
                        secrets=ScopedSecrets(secret_values, session_node.secret_refs()),
                    )
                    command = _mgr._substitute_template_vars(
                        session_node.harness.start(start_ctx),
                        {"session_name": name, "workspace_name": workspace_name},
                    )
                    if (note := session_node.harness.launch_note()) is not None:
                        output.detail(note)

                    # Insert DB record before any tmux work so a crash mid-create
                    # leaves a recoverable row (and the teardown can find it to
                    # delete). The harness's start op ran just above, so its
                    # state blob lands with the new row.
                    db.insert_session(
                        name,
                        workspace_name,
                        template.name,
                        mode,
                        agent_name=resolved_agent_name,
                        created_workspace=pending_workspace is not None,
                        created_agent=pending_agent is not None,
                        socket_path=expected_socket,
                        harness_state=session_node.harness.state,
                    )

                    deploy_restricted_config(run_command, history_limit=config.session.history_limit)
                    session_env = _mgr._resolve_session_env(
                        registry,
                        values=secret_values,
                        db=db,
                        vm=vm,
                        ws=ws,
                        session_name=name,
                        session_template=template,
                        mode=mode,
                        agent_name=resolved_agent_name,
                        linux_user=linux_user,
                    )
                    # Pick the SSH transport for tmux operations:
                    # - admin sessions: admin's run_command (unchanged)
                    # - agent sessions: agent's run_command (direct
                    #   target-user SSH). agent_target was built and probed above
                    #   so a pre-rollout agent never reaches this point. admin's
                    #   ``target`` is still passed for socket-root setup which
                    #   requires root.
                    session_run_command: RunCommand
                    if mode == SessionMode.AGENT:
                        assert agent_target is not None  # built in the agent branches above
                        session_run_command = agent_target.run
                    else:
                        session_run_command = run_command
                    sock, pid = create_tmux_session(
                        name,
                        ws.workspace_path,
                        command,
                        linux_user,
                        run_command=session_run_command,
                        target=target,
                        admin_username=vm.admin_username,
                        is_admin=(mode == SessionMode.ADMIN),
                        env=session_env,
                    )
                except (KeyboardInterrupt, Exception):
                    # Session-internal cleanup only (DB row, grant, group
                    # membership: the node's partial-state teardown). The
                    # realized ephemerals are unwound by the outer handlers,
                    # whose warn prints one clean reason line before the
                    # rollback's delete messages start landing.
                    session_node.teardown()
                    raise

                # The session's realizing slice is complete: flip the node.
                # Deliberately NOT via the realization log: a completed
                # session (tmux up, row written) is never rolled back, so
                # failures past this point unwind only the ephemerals, and
                # the session survives them. That pins the completed-session
                # window as non-rollbackable.
                session_node.mark_realized()

                # Persist socket path, PID, and boot ID
                if sock:
                    db.update_session_socket_path(name, sock)
                if pid is not None:
                    boot_id = _mgr._get_boot_id(target)
                    if boot_id is not None:
                        db.update_session_pid(name, pid, boot_id=boot_id)
                    else:
                        output.warn(f"Could not read boot ID for session '{name}', PID not stored")
                else:
                    output.warn(f"Could not capture PID for session '{name}', will auto-repair on next access")

            # The section is closed: the terminal result line and the
            # post-start bookkeeping (tmuxinator regen, console add) render
            # at column 0, mirroring restart_session. They stay inside the
            # outer try so a failure here still triggers the ephemeral
            # rollback (a completed session itself is never rolled back).
            mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
            output.result(f"Session '{name}' started ({mode_label}, template: {template.name})")

            # Update tmuxinator config and add to console if it exists
            _mgr._regenerate_tmuxinator(db, config, vm, ws)
            from agentworks.sessions.console import add_session_to_console

            add_session_to_console(name, run_command=run_command, socket_path=sock)
        except KeyboardInterrupt:
            output.warn(f"Cancelling session create '{name}'... rolling back.")
            log.unwind()
            raise
        except Exception as e:
            # Print the reason BEFORE the rollback's delete-* messages so the
            # operator sees the failure context first, not after a stream of
            # 'Agent deleted' / 'Workspace deleted' lines. The CLI's
            # exception handler still prints the canonical 'Error: ...' line
            # with the typed hint at the very end; this warn just bridges
            # the silence between "thing X created" and the rollback output.
            output.warn(f"Session create '{name}' failed; rolling back. Reason: {e}")
            log.unwind()
            raise
