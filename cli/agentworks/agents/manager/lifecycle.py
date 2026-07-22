"""Agent create / delete / reinit.

The mutating half of the agents command layer. The on-VM provisioning
bodies live in ``agents/initializer.py``; the realization body shared
with the session orchestrator lives in ``agents/realize.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.agents.manager as _mgr
from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import (
    AlreadyExistsError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
)
from agentworks.vms.manager import gated_vm_boundary

from ._common import _require_vm, agent_scope

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.vms.nodes import LiveVMNode

# ``_mgr`` binds this module's own package object (safe: by the time
# ``lifecycle`` is imported, ``agentworks.agents.manager`` is already in
# ``sys.modules``, mid-initialization). ``delete_agent`` below reads
# ``_mgr.transport`` at call time rather than importing ``transport``
# directly, so ``tests/conftest.py`` and
# ``tests/agents/test_delete_grant_revoke_orchestrated.py`` monkeypatching
# ``agentworks.agents.manager.transport`` still reaches this call.


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

    pending_agent = pending_agent_node(db, config, name, tmpl_node, vm_node)
    nodes = walk(pending_agent)
    # The walk supplies the boundary union (the credential tokens plus
    # the site's config secrets). Provisioning is hermetic: no
    # operator-env secrets join here; they get prompted at the use
    # site (agent shell, session create, etc.).
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    providers = {node.provider.owner_name: node.provider for node in tmpl_node.credentials}

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
        with output.section("Preflight"):
            output.info(f"Checking agent-template/{agent_tmpl.name}...")
            announce_git_credentials(providers)
            preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        with output.section("Resolving Secrets"):
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

        with output.section("Agent Initialization"):
            from agentworks.agents.realize import realize_agent

            # realize_agent emits its own "Agent '<name>' created ..."
            # line (shared with the session-create ephemeral path), so it
            # stays the command's closing line inside this section rather
            # than being echoed again through result(); mirrors the
            # standalone workspace-create path (see realize_workspace).
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
    vm_node: LiveVMNode | None = None,
) -> None:
    """Delete an agent from a VM.

    Orchestrated on the standalone path (``vm_node=None``, the command
    root and ``delete_session``'s agent-cleanup call):
    ``vms.manager.gated_vm_boundary`` composes the live-VM graph (no
    env-chain targets; this command composes no runtime env), the
    activation gate opens BEFORE the preflight sweep with its
    just-in-time values seeding the boundary resolver, and the
    held-active span covers the session-kill and user-removal SSH work.
    The sessions guard, the confirm gate, and the not-found check stay
    pre-boundary: a refusal costs zero prompts, zero resolves, and zero
    gate events.

    ``vm_node`` is the nested-teardown path (session create's ephemeral
    ROLLBACK, where ``PendingAgentNode.teardown`` runs INSIDE the
    caller's held activation gate). That gate already converged the VM
    and holds it active across the whole unwind, so this path composes
    NO second boundary and resolves NOTHING: it trusts the caller's
    gate and re-enters only the keepalive hold, reaching the platform
    through the node's own site edge. Passing the node (never a bare
    platform) is what keeps a teardown from silently falling into the
    boundary-building standalone branch.
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
        output.info(f"Agent '{name}' has {output.count(len(agent_sessions), 'session')}:")
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
    if vm_node is None:
        # The standalone composition root: build the boundary here.
        from agentworks.bootstrap import build_registry

        registry = build_registry(config)
        boundary: AbstractContextManager[object] = gated_vm_boundary(
            db, config, registry, vm, scope=agent_scope(db, vm.name, name)
        )
    else:
        # The nested-teardown path: the caller's composition already
        # converged the VM and holds its activation gate open across
        # this unwind, so we compose no second boundary and resolve
        # nothing; we re-enter only the keepalive hold, reaching the
        # platform through the node's own site edge.
        #
        # That hold keeps the NODE's VM active, but the delete body
        # issues its SSH + DB work against the agent's own VM (``vm``,
        # the agent row's ``vm_name``). Enforce that they are the same
        # VM: a mismatched node would silently hold one VM active while
        # operating on another. Unreachable today (the pending nodes
        # always pass their own ``self._vm``), so this is a loud guard
        # on a teardown-wiring bug, not a runtime branch we expect to
        # take.
        if vm_node.row.name != vm.name:
            raise StateError(
                f"nested teardown of agent '{name}' was handed a VM "
                f"node for '{vm_node.row.name}', but the agent is on "
                f"'{vm.name}'; the node handed to a teardown must be the "
                f"entity's own VM node (teardown-wiring bug).",
                entity_kind="agent",
                entity_name=name,
            )
        boundary = vm_node.hold_active()
    with boundary:
        # Kill running sessions for this agent (status-aware)
        if agent_sessions:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import check_session_status, ensure_pids_batch
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session

            target = _mgr.transport(vm, config, logger=ssh_logger)
            agent_sessions = ensure_pids_batch(agent_sessions, db=db, config=config)
            # Snapshot console memberships before db.delete_session cascades them.
            console_pairs = [(c.name, s.name) for s in agent_sessions for c in db.list_consoles_for_session(s.name)]
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
                    if (
                        session.pid
                        and session.pid > 0
                        and force_kill_tmux_server(
                            session.pid,
                            target=target,
                            socket_path=session.socket_path,
                        )
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
    providers = {node.provider.owner_name: node.provider for node in tmpl_node.credentials}

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
        with output.section("Preflight"):
            output.info(f"Checking agent-template/{agent_tmpl.name}...")
            announce_git_credentials(providers)
            preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        with output.section("Resolving Secrets"):
            resolver.resolve()

        def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(resolver.values, secret_names),
            )

        git_tokens = credential_tokens(tmpl_node, scoped_ctx)

        with output.section("Agent Initialization"):
            from agentworks.agents.initializer import create_agent_on_vm
            from agentworks.ssh import SSHLogger

            ssh_logger = SSHLogger(vm.name, "agent-reinit")
            try:
                try:
                    create_agent_on_vm(
                        vm,
                        config,
                        registry,
                        agent_tmpl,
                        agent.linux_user,
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

        # The section is closed: the terminal outcome line renders at
        # column 0 via result(), matching the reference create/restart
        # flows.
        output.result(f"Agent '{name}' reinitialized")
