"""Agent lifecycle orchestration.

The command layer of the agents domain: create / reinit / delete,
list / describe, and the direct shell / exec surface. The on-VM
provisioning bodies live in ``agents/initializer.py``; the
workspace-grant commands and group-membership primitives live in
``agents/grants.py``; the realization body shared with the session
orchestrator lives in ``agents/realize.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import (
    AlreadyExistsError,
    AuthorizationError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.transports import transport
from agentworks.vms.manager import gated_vm_boundary, keep_active

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.transports import Transport

AGENT_PREFIX = "agt-"


def agent_scope(db: Database, vm_name: str, agent_name: str) -> OperationScope:
    """The agent commands' shared AGENT-level operation scope (public:
    ``agents.grants`` shares it): the operation is about the agent (on
    its VM), even though the composed graph is the live VM alone; pass
    the level of the entity the command is ABOUT, not of what it
    walks. The AGENT level's field rules (required vm + agent;
    forbidden workspace, session: agents are VM-scoped, a workspace
    relationship is a grant, never identity) are enforced by the
    scope's own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.AGENT,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm_name,
        agent=agent_name,
    )


def derive_linux_user(agent_name: str) -> str:
    """Derive the Linux username for a newly-created agent: agt-<name>.

    Existing agents retain whatever username was stored in the database at
    their creation time (older agents use the legacy agt-- prefix). Always
    read agent_row.linux_user for the canonical value; this helper is only
    used at agent-create time.
    """
    return f"{AGENT_PREFIX}{agent_name}"


class _AgentDirectEnvScopes(NamedTuple):
    """Per-scope env dicts for ``agent shell`` / ``agent exec``.

    The ``workspace`` field is ``None`` for shells / execs that don't
    pin a workspace context (``agent shell`` without ``--workspace``,
    ``agent exec`` today). When set, workspace-template env enters the
    scope precedence ladder between vm and agent.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None
    agent: dict[str, EnvEntry]


def _resolve_agent_direct_env_scopes(
    registry: Registry,
    vm: VMRow,
    agent: AgentRow,
    *,
    ws: WorkspaceRow | None = None,
) -> _AgentDirectEnvScopes:
    """Resolve per-scope env dicts for ``agent shell`` / ``agent exec``.

    Both the SecretTarget (eager-resolve) and the ``compose_env`` call
    (render) consume the result of this helper, guaranteeing they see
    identical scope state -- no drift between "what was prompted for"
    and "what was passed to the shell."

    Scope sources mirror the scope precedence ladder:

    - ``vm``: the VM's actual template env (from the ``vm.template`` DB row).
    - ``workspace``: when ``ws`` is supplied, the workspace template's env.
    - ``agent``: the agent row's template env (from the DB row).
      The agent pre-exists this call and may have been created under a
      different template than the operator's current ``--template``
      would resolve.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    vm_tmpl = _resolve_vm_template(registry, vm.template)
    agent_tmpl = _resolve_agent_template(registry, agent.template)
    ws_env: dict[str, EnvEntry] | None = None
    if ws is not None:
        ws_env = _resolve_ws_template(registry, ws.template).env
    return _AgentDirectEnvScopes(
        vm=vm_tmpl.env,
        workspace=ws_env,
        agent=agent_tmpl.env,
    )


def _agent_direct_secret_target(
    scopes: _AgentDirectEnvScopes, *, label: str,
) -> SecretTarget:
    """Build the SecretTarget for ``agent shell`` / ``agent exec`` from
    pre-resolved scope dicts.

    Single-phase: the operator opens one shell as the agent's Linux user.
    The companion ``compose_env`` call must consume the same ``scopes``
    so the eager-resolve prompts cover exactly what the runtime env will
    reference (no drift).
    """
    from agentworks.secrets import SecretTarget

    return SecretTarget(
        vm=scopes.vm,
        workspace=scopes.workspace,
        agent=scopes.agent,
        label=label,
    )


def _resolve_workspace_for_agent(
    db: Database, vm: VMRow, agent: AgentRow, workspace_name: str | None,
) -> WorkspaceRow | None:
    """Resolve a ``--workspace`` flag for ``agent shell`` / ``agent exec``.

    Returns ``None`` when ``workspace_name`` is ``None``. Otherwise loads
    the workspace and validates (in order) that it exists, belongs to the
    agent's VM, and the agent has access. All failures surface as clean
    typed errors before any SSH work.
    """
    if workspace_name is None:
        return None
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if ws.vm_name != vm.name:
        raise ValidationError(
            f"workspace '{workspace_name}' belongs to VM '{ws.vm_name}', not '{vm.name}'",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if not db.has_any_grant(agent.name, workspace_name):
        raise AuthorizationError(
            f"agent '{agent.name}' does not have access to workspace '{workspace_name}'",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agent grant-workspaces {agent.name} {workspace_name}' to grant access.",
        )
    return ws


def _assert_agent_ssh_works(target: Transport, agent: AgentRow) -> None:
    """Probe direct agent SSH; raise an actionable error on auth rejection.

    The direct-target-user-SSH rollout populates each agent's
    ``~/.ssh/authorized_keys`` with the operator's key set at agent create /
    reinit. Agents that existed before this rollout have a home directory
    with no ``.ssh/authorized_keys`` for the operator, so direct SSH as the
    agent is rejected. Catch that specific case here and turn the otherwise-
    opaque SSH transport failure into a clear "run ``agw agent reinit``"
    instruction.

    A probe round-trip is cheap relative to letting the failure surface
    mid-operation with partial state.

    Two failure shapes are distinguished:

    - Non-zero exit (SSH_TRANSPORT_ERROR = 255 typically): SSH connected
      and ``ssh`` itself reported an auth / transport failure. Treated as
      the pre-rollout case and raised as ``StateError`` with a reinit hint.
    - ``SSHError`` from ``target.run`` (timeout / unreachable host): the
      VM itself isn't reachable. Re-raised as ``ConnectivityError`` so the
      operator sees "VM unreachable" rather than "agent needs reinit."
    """
    from agentworks.errors import ConnectivityError
    from agentworks.ssh import SSH_TRANSPORT_ERROR, SSHError

    try:
        probe = target.run("true", check=False)
    except SSHError as e:
        raise ConnectivityError(
            f"direct SSH probe to agent '{agent.name}' failed: {e}",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Check that VM '{agent.vm_name}' is reachable.",
        ) from e
    if probe.ok:
        return
    # SSH transport failures (auth rejected, host unreachable, etc.) report
    # SSH_TRANSPORT_ERROR (255). Combined with no other obvious signal, this
    # is our best indication that direct agent SSH is not yet provisioned.
    if probe.returncode == SSH_TRANSPORT_ERROR:
        raise StateError(
            f"agent '{agent.name}' rejected direct SSH (likely predates the "
            "direct-target-user-SSH rollout).",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agw agent reinit {agent.name}' to populate its authorized_keys.",
        )


def create_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str,
    template: str | None = None,
    grant_all_workspaces: bool = False,
) -> None:
    """Create an agent on a VM.

    Orchestrated: the graph derives from the resolved agent template
    (its declared git credentials become edges to the credential nodes)
    and the VM's row (its site field is the edge to the vm-site node);
    the activation gate replaces this command's ``keep_active``,
    opening BEFORE the preflight sweep with its just-in-time values
    seeding the boundary resolver; tokens are delivered scoped to each
    node's declared names and handed, pre-resolved, to the phase-free
    realization body. The completed agent is never rollback-tracked
    (the body cleans its own partial state, and a failure after the row
    exists keeps the agent, exactly the imperative shape), so no
    realization log exists here.
    """

    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies (e.g.
    # GitCredentialKind's error policy on agent template's
    # git_credentials list, future TemplateReference typos on
    # inherits) fire before any template / DB / VM business logic
    # surfaces its own NotFoundError.
    registry = build_registry(config)

    agent_tmpl = resolve_template(registry, template)

    validate_name(name)

    if db.get_agent(name) is not None:
        raise AlreadyExistsError(
            f"agent '{name}' already exists",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, vm_name)

    # BUILD: the command names its direct resources (the resolved
    # template, this VM) and constructs the pending agent node with its
    # edges attached; the walk assembles the graph. Construction is
    # cheap and touches no secret machinery; the walk union below is
    # the boundary's source. Nothing resolves yet. A stranded site
    # fails here, before any prompt.
    from agentworks.agents.nodes import (
        agent_template_node,
        credential_tokens,
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
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.initializer import announce_git_credentials
    from agentworks.vms.nodes import live_vm_node

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)
    tmpl_node = agent_template_node(registry, agent_tmpl)

    def _teardown_platform_ctx() -> RunContext:
        # The nested teardown's op-start context: built at teardown
        # time (post-boundary, values resolved), scoped to the site's
        # declared names.
        return RunContext(
            config=config,
            secrets=ScopedSecrets(
                resolver.values, vm_node.site.secret_refs()
            ),
        )

    pending_agent = pending_agent_node(
        db, config, name, tmpl_node, vm_node, _teardown_platform_ctx
    )
    nodes = walk(pending_agent)
    # The walk supplies the boundary union (the credential tokens plus
    # the site's config secrets). Provisioning is hermetic: no
    # operator-env secrets join here; they get prompted at the use
    # site (agent shell, session create, etc.).
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    providers = {
        node.provider.owner_name: node.provider for node in tmpl_node.credentials
    }

    scope = OperationScope(
        level=ScopeLevel.AGENT,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm_name,
        agent=name,
    )

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: an unresolvable token fails before
        # any prompt, then git tokens and any site config secret
        # (proxmox's API token) resolve in one prompt session.
        output.phase("Preflight")
        output.detail(f"Checking agent-template/{agent_tmpl.name}...")
        announce_git_credentials(providers)
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        output.phase("Resolving Secrets")
        resolver.resolve()

        def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(resolver.values, secret_names),
            )

        # Each credential's token, read through its node's SCOPED
        # delivery; the write-step runup inside the body applies the
        # skip-and-degrade policy as before.
        git_tokens = credential_tokens(tmpl_node, scoped_ctx)

        output.phase("Agent Initialization")
        from agentworks.agents.realize import realize_agent

        realize_agent(
            db,
            config,
            registry,
            name=name,
            vm=vm,
            template=agent_tmpl,
            git_tokens=git_tokens,
            grant_all_workspaces=grant_all_workspaces,
        )
        # Bookkeeping only, deliberately not via a realization log:
        # this command never unwinds a realized agent (a failure after
        # the row exists keeps the agent, as the imperative command
        # did), and the body already cleaned up its own partial state
        # before re-raising.
        pending_agent.mark_realized()


def delete_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
    platform: VMPlatform | None = None,
    platform_ctx: RunContext | None = None,
) -> None:
    """Delete an agent from a VM.

    Orchestrated on the standalone path (``platform=None``, the
    command root and ``delete_session``'s agent-cleanup call):
    ``vms.manager.gated_vm_boundary`` composes the live-VM graph (no
    env-chain targets; this command composes no runtime env), the
    activation gate replaces this command's ``keep_active``, opening
    BEFORE the preflight sweep with its just-in-time values seeding
    the boundary resolver, and the held-active span covers the
    session-kill and user-removal SSH work. The sessions guard, the
    confirm gate, and the not-found check stay pre-boundary: a
    refusal costs zero prompts, zero resolves, and zero gate events.

    ``platform`` accepts the caller's already-bound platform (session
    create's ephemeral ROLLBACK path, where teardown runs INSIDE the
    caller's held gate span), paired with ``platform_ctx``, that
    composition's op-start context for the hold's power ops: that path
    must not rebuild a boundary or re-run the resolve pass
    mid-rollback, so it keeps the imperative ``keep_active`` hold on
    the handed-in platform. This is the INTERIM nested-teardown seam;
    it closes when the session-create unwind hands a node instead of
    a platform.
    """
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    # Check for sessions using this agent
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    if agent_sessions and not force:
        for s in agent_sessions:
            output.detail(f"{s.name}")
        raise StateError(
            f"agent '{name}' has {len(agent_sessions)} session(s).",
            entity_kind="agent",
            entity_name=name,
            hint="Delete the sessions first, or pass --force to also stop them.",
        )

    if not yes:
        msg = f"Delete agent '{name}'?"
        if agent_sessions:
            msg += f" ({len(agent_sessions)} session(s) will also be stopped)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    vm = _require_vm(db, agent.vm_name)

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-delete")
    output.info(f"Deleting agent '{name}' on VM '{vm.name}'...")
    if platform is None:
        # The standalone composition root: build the boundary here.
        from agentworks.bootstrap import build_registry

        registry = build_registry(config)
        boundary: AbstractContextManager[object] = gated_vm_boundary(
            db, config, registry, vm, scope=agent_scope(db, vm.name, name)
        )
    else:
        # The nested-teardown path: the caller's composition already
        # resolved and holds its gate open, so only the hold is
        # re-entered (never a second boundary or resolve); the
        # handed-in ctx serves the hold's power ops.
        if platform_ctx is None:
            raise StateError(
                f"delete_agent('{name}') was handed a bound platform "
                f"without its op-start context; the nested-teardown "
                f"path passes both or neither."
            )
        boundary = keep_active(db, config, vm, platform, platform_ctx)
    with boundary:

        # Kill running sessions for this agent (status-aware)
        if agent_sessions:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import check_session_status, ensure_pids_batch
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session

            target = transport(vm, config, logger=ssh_logger)
            agent_sessions = ensure_pids_batch(agent_sessions, db=db, config=config)
            # Snapshot console memberships before db.delete_session cascades them.
            console_pairs = [
                (c.name, s.name)
                for s in agent_sessions
                for c in db.list_consoles_for_session(s.name)
            ]
            unstoppable: list[str] = []
            for session in agent_sessions:
                status = check_session_status(session, target=target)
                if status == SessionStatus.OK:
                    if not kill_session(session.name, run_command=target.run, socket_path=session.socket_path):
                        # Race: session may have exited between check and kill. Recheck.
                        recheck = check_session_status(session, target=target)
                        if recheck != SessionStatus.STOPPED:
                            unstoppable.append(session.name)
                            continue
                elif status == SessionStatus.BROKEN:
                    if session.pid and session.pid > 0 and force_kill_tmux_server(
                        session.pid, target=target, socket_path=session.socket_path,
                    ):
                        pass  # killed successfully
                    else:
                        unstoppable.append(session.name)
                elif status == SessionStatus.UNKNOWN:
                    unstoppable.append(session.name)
            if unstoppable:
                raise StateError(
                    f"cannot delete agent '{name}': {len(unstoppable)} session(s) could not be stopped "
                    f"({', '.join(unstoppable)}).",
                    entity_kind="agent",
                    entity_name=name,
                    hint="Resolve the stuck sessions manually before retrying.",
                )
            for session in agent_sessions:
                db.delete_session(session.name)
            output.detail(f"Deleted {len(agent_sessions)} session(s)")

            # Best-effort: take down dangling 'Waiting for session...' windows in
            # any console that listed one of these sessions.
            if console_pairs:
                from agentworks.sessions.multi_console import kill_session_windows

                kill_session_windows(target, pairs=console_pairs)

        # Remove from all workspace groups
        from agentworks.agents.grants import remove_from_workspace_group
        from agentworks.agents.initializer import delete_agent_on_vm

        granted_workspaces = db.list_granted_workspaces(name)
        for ws_name in granted_workspaces:
            remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=ssh_logger)

        delete_agent_on_vm(vm, config, agent.linux_user, logger=ssh_logger)
        ssh_logger.close()

        db.delete_agent(name)

        # Refresh operator SSH config so the per-agent block disappears.
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)

        output.info(f"Agent '{name}' deleted")


def reinit_agent(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Re-run agent setup using the stored template.

    Orchestrated: the graph derives from the agent's row and its stored
    template (the live agent node, the template node whose declared
    credentials become edges, the VM chain); the activation gate
    replaces this command's ``keep_active``, opening BEFORE the
    preflight sweep with its just-in-time values seeding the boundary
    resolver; tokens are delivered scoped to each node's declared
    names. Nothing here is created, so there is no realization log and
    nothing to unwind; a failed reinit leaves the agent re-runnable, as
    before.
    """

    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies fire before
    # template / DB / VM business logic.
    registry = build_registry(config)

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    agent_tmpl = resolve_template(registry, agent.template)

    vm = _require_vm(db, agent.vm_name)

    # BUILD: the live agent from its row, plus the resolved template
    # whose declared credentials become edges (the template is a
    # planned-ops participant at reinit: the materials rewrite needs
    # its tokens, so they must join the boundary union). The live
    # agent's row carries no template edge, so the walk is multi-root.
    from agentworks.agents.nodes import (
        agent_template_node,
        credential_tokens,
        live_agent_node,
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
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.initializer import announce_git_credentials
    from agentworks.vms.nodes import live_vm_node

    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm)
    agent_node = live_agent_node(agent, vm_node)
    tmpl_node = agent_template_node(registry, agent_tmpl)
    nodes = walk(agent_node, tmpl_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    providers = {
        node.provider.owner_name: node.provider for node in tmpl_node.credentials
    }

    scope = OperationScope(
        level=ScopeLevel.AGENT,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=agent.vm_name,
        agent=name,
    )

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: git tokens and any site config secret
        # resolve in one prompt session. Provisioning is hermetic: no
        # operator-env secrets are prompted at reinit.
        output.phase("Preflight")
        output.detail(f"Checking agent-template/{agent_tmpl.name}...")
        announce_git_credentials(providers)
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        output.phase("Resolving Secrets")
        resolver.resolve()

        def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(resolver.values, secret_names),
            )

        git_tokens = credential_tokens(tmpl_node, scoped_ctx)

        output.phase("Agent Initialization")
        from agentworks.agents.initializer import create_agent_on_vm
        from agentworks.ssh import SSHLogger
        ssh_logger = SSHLogger(vm.name, "agent-reinit")
        try:
            try:
                create_agent_on_vm(
                    vm, config, registry, agent_tmpl, agent.linux_user,
                    agent_name=agent.name,
                    git_tokens=git_tokens,
                    logger=ssh_logger,
                )
            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling agent reinit '{name}'. The agent may be in a partial state. "
                    f"Re-run 'agent reinit {name}' to retry. SSH log: {ssh_logger.path}"
                )
                raise
            except Exception as e:
                raise ExternalError(
                    f"reinitializing agent: {e}",
                    entity_kind="agent",
                    entity_name=name,
                    hint=f"SSH log: {ssh_logger.path}",
                ) from e
        finally:
            ssh_logger.close()

        # Refresh operator SSH config (declarative rebuild; picks up any
        # config changes that affect the per-agent block).
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)

        output.info(f"Agent '{name}' reinitialized")


MAX_GRANTS_DISPLAY = 60


def _format_grants(db: Database, agent_name: str, grant_all: bool) -> str:
    """Format workspace grants for display in agent list."""
    if grant_all:
        return "--ALL--"

    grants = db.list_granted_workspaces_with_types(agent_name)
    if not grants:
        return "(none)"

    parts: list[str] = []
    for ws_name, has_explicit, has_implicit in grants:
        # Mark with * if implicit-only (no explicit grant)
        suffix = "*" if has_implicit and not has_explicit else ""
        parts.append(f"{ws_name}{suffix}")

    result = ", ".join(parts)
    if len(result) > MAX_GRANTS_DISPLAY:
        result = result[: MAX_GRANTS_DISPLAY - 3] + "..."
    return result


def list_agents(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    names_only: bool = False,
) -> None:
    """List agents.

    With ``names_only=True``, emit one agent name per line and skip
    the table render. Used by shell completion (see issue #147).
    """
    agents = db.list_agents(vm_name=vm_name)

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No agents found" line below is
        # for human readers only.
        for agent in agents:
            output.info(agent.name)
        return

    if not agents:
        output.info("No agents found.")
        return

    output.info(f"{'NAME':<20} {'VM':<15} {'TEMPLATE':<12} {'WORKSPACE GRANTS'}")
    output.info("-" * 80)
    for agent in agents:
        grants = _format_grants(db, agent.name, agent.grant_all)
        output.info(f"{agent.name:<20} {agent.vm_name:<15} {agent.template or '-':<12} {grants}")


def describe_agent(
    db: Database,
    *,
    name: str,
) -> None:
    """Show detailed information about an agent."""
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    output.info(f"Name:       {agent.name}")
    output.info(f"VM:         {agent.vm_name}")
    output.info(f"Linux user: {agent.linux_user}")
    output.info(f"Template:   {agent.template or '-'}")
    output.info(f"Grant all:  {'yes' if agent.grant_all else 'no'}")
    output.info(f"Created:    {agent.created_at}")

    # Explicit grants
    grants = db.list_granted_workspaces_with_types(name)
    explicit = [ws for ws, has_explicit, _ in grants if has_explicit]
    output.info(f"\nExplicit grants ({len(explicit)}):")
    if explicit:
        for ws in explicit:
            output.detail(ws)
    else:
        output.detail("(none)")

    # Sessions (which also show implicit grants)
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    output.info(f"\nSessions ({len(agent_sessions)}):")
    if agent_sessions:
        for s in agent_sessions:
            output.detail(f"{s.name}  [{s.template}]  workspace: {s.workspace_name}")
    else:
        output.detail("(none)")


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str | None = None,
) -> int:
    """Open a shell as an agent user on a VM.

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`agentworks.vms.manager.exec_vm`.

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph
    derives from the VM's row, the activation gate replaces this
    command's ``keep_active`` use (opening BEFORE the preflight sweep;
    its just-in-time values seed the boundary resolver), and the
    held-active span covers the whole interactive session.
    """
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.env import ResourceContext, compose_env
    from agentworks.transports import agent_transport

    # Resolve workspace upfront (needed for authz check, env scope, AND
    # ctx) before any SSH probe so failures surface as clean validation
    # errors and the eager-resolve below sees the right scope chain.
    ws = _resolve_workspace_for_agent(db, vm, agent, workspace_name)

    # The orchestrated composition root (gated_vm_boundary): the agent
    # shell's env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), after every node's
    # preflight; the activation gate opens before the sweep and its
    # held-active span covers the whole interactive session. The same
    # scope dicts feed both the SecretTarget and compose_env below so
    # the two consumers can't drift.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db, config, registry, vm,
        targets=[_agent_direct_secret_target(scopes, label=f"agent-shell={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
    ) as (_vm_node, resolver):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=agent.linux_user,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
            agent_name=agent.name,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            agent=scopes.agent,
        )

        # Direct agent SSH: no admin+sudo detour. The agent's
        # authorized_keys accepts the operator's key set.
        target = agent_transport(vm, config, agent)

        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error
        # rather than dropping into a remote shell that immediately exits
        # on Permission denied.
        _assert_agent_ssh_works(target, agent)

        if ws is not None:
            import shlex

            q_path = shlex.quote(ws.workspace_path)
            # SSH as the agent, then cd into the workspace and exec an
            # interactive login shell. No sudo / su involved.
            shell_cmd = f"cd {q_path} && exec $SHELL -li"
            return target.interactive(shell_cmd, env=env)
        # SSH as the agent with no command -> interactive login shell.
        return target.interactive("", env=env)


def exec_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    command: list[str],
    workspace_name: str | None = None,
) -> int:
    """Execute a command as an agent user on a VM via direct agent SSH.

    Opens a non-interactive SSH session directly as the agent's Linux user
    and runs the command in a login shell so the agent's PATH /
    profile is in scope. Stdout / stderr stream through to the caller; the
    return value is the remote command's exit code.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to the agent's VM and the agent must have
    access.

    Orchestrated (``vms.manager.gated_vm_boundary``), mirroring
    :func:`shell_agent`: the gate opens before the preflight sweep and
    the held-active span covers the streamed remote command.
    """
    import shlex

    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import reject_dash_prefixed_command
    from agentworks.transports import agent_transport

    reject_dash_prefixed_command(command, kind="agent", name=name)

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    # Resolve workspace upfront so cross-VM / authz failures surface as
    # clean typed errors before any SSH work and the eager-resolve below
    # sees the right scope chain.
    ws = _resolve_workspace_for_agent(db, vm, agent, workspace_name)

    # The orchestrated composition root (gated_vm_boundary): the agent
    # exec env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), after every node's
    # preflight; the gate's held-active span covers the streamed
    # remote command. The same scope dicts feed both the SecretTarget
    # and compose_env below so the two consumers can't drift.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db, config, registry, vm,
        targets=[_agent_direct_secret_target(scopes, label=f"agent-exec={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
    ) as (_vm_node, resolver):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=agent.linux_user,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
            agent_name=agent.name,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            agent=scopes.agent,
        )

        target = agent_transport(vm, config, agent)

        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error.
        _assert_agent_ssh_works(target, agent)

        remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
        if ws is not None:
            remote_cmd = f"cd {shlex.quote(ws.workspace_path)} && {remote_cmd}"
        # Wrap in a login shell so the agent's PATH (mise shims,
        # ~/.local/bin, etc.) is set up. This matches the env an operator
        # gets via `agent shell`.
        return target.call_streaming(
            f"$SHELL -lc {shlex.quote(remote_cmd)}", env=env,
        )


# -- Helpers ---------------------------------------------------------------


def _require_vm(db: Database, vm_name: str) -> VMRow:
    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    return vm


def _require_workspace(db: Database, workspace_name: str) -> WorkspaceRow:
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )
    return vm
