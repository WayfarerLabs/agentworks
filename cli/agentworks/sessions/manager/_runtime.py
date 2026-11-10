"""Read-only session launch and stop preparation for runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import DesiredRuntimeState, SessionMode, SessionStatus
from agentworks.errors import BrokenStateError, NotFoundError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.agents.nodes import LiveAgentNode
    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.orchestration.node import Node
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.nodes import LiveSessionNode
    from agentworks.sessions.template import SessionTemplate
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode
    from agentworks.workspaces.nodes import LiveWorkspaceNode


@dataclass(frozen=True, kw_only=True)
class SessionLaunchDefinition:
    """Local launch identity, graph, and policy; no resolved credentials."""

    db: Database
    config: Config
    registry: Registry
    session: SessionRow
    workspace: WorkspaceRow
    vm: VMRow
    agent: AgentRow | None
    template: ResolvedSessionTemplate
    vm_node: LiveVMNode
    workspace_node: LiveWorkspaceNode
    agent_node: LiveAgentNode | None
    session_node: LiveSessionNode
    nodes: tuple[Node, ...]
    scope: OperationScope
    env_target: SecretTarget
    replace_running: bool
    running_restart_authorized: bool
    force: bool
    intent: HarnessLaunchIntent


@dataclass(frozen=True, kw_only=True)
class PreparedSessionLaunch:
    """One eligible workload with its own SESSION scope and owning transport."""

    definition: SessionLaunchDefinition
    admin_target: Transport
    session_target: Transport
    observed_status: SessionStatus
    legacy: bool
    recovered_startup: bool
    noop: bool

    @property
    def secret_targets(self) -> tuple[SecretTarget, ...]:
        return (self.definition.env_target,) if self.requires_launch else ()

    @property
    def requires_launch(self) -> bool:
        return not self.noop and not (self.recovered_startup and not self.definition.replace_running)

    @property
    def secret_nodes(self) -> tuple[Node, ...]:
        return self.definition.nodes if self.requires_launch else ()


@dataclass(frozen=True, kw_only=True)
class PreparedSessionStop:
    """Exact observed teardown authority, without workload secret requirements."""

    db: Database
    config: Config
    session: SessionRow
    workspace: WorkspaceRow
    vm: VMRow
    admin_target: Transport | None
    session_target: Transport | None
    observed_status: SessionStatus
    parent_off: bool
    force: bool


def prepare_session_definition(
    db: Database,
    config: Config,
    registry: Registry,
    session: SessionRow,
    *,
    vm_node: LiveVMNode,
    replace_running: bool = False,
    running_restart_authorized: bool = False,
    force: bool = False,
    intent: HarnessLaunchIntent = HarnessLaunchIntent.RESUME_OR_NEW,
) -> SessionLaunchDefinition:
    """Resolve the local definition and graph before infrastructure activation."""
    from agentworks.agents.nodes import live_agent_node
    from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
    from agentworks.orchestration.walk import walk
    from agentworks.resources.access import ensure_recipe_enabled
    from agentworks.sessions.nodes import live_session_node
    from agentworks.workspaces.nodes import live_workspace_node

    workspace = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)
    if vm.vm_uuid != vm_node.row.vm_uuid:
        raise StateError("session parent changed during selection", entity_kind="session", entity_name=session.name)
    from agentworks.vms.manager import require_vm_ssh_boundary

    require_vm_ssh_boundary(db, config, vm)
    template = _mgr._resolve_template(registry, session.template, db=db, instance_name=session.name)
    from typing import cast

    from agentworks.instance_specs import ensure_effective_references_enabled, get_instance_overlay
    from agentworks.sessions.template import effective_references, validate_effective_harness
    from agentworks.sessions.templates import resolve_template_with_provenance

    stored_overlay = get_instance_overlay(db, "session", session.name)
    if stored_overlay is not None:
        layered = resolve_template_with_provenance(
            registry,
            session.template,
            overlay=cast("SessionTemplate", stored_overlay.declaration),
            instance_name=session.name,
        )
        template = layered.value
        ensure_effective_references_enabled(
            registry, effective_references(template, ("session", session.name), layered.provenance)
        )
        validate_effective_harness(template, ("session", session.name), layered.provenance)
    ensure_harness_integration_enabled(registry, template.harness_integration)
    ensure_recipe_enabled(registry, "session-template", template.name)
    workspace_node = live_workspace_node(workspace, vm_node)
    agent = db.get_agent(session.agent_name) if session.agent_name is not None else None
    if session.agent_name is not None and agent is None:
        raise NotFoundError("session agent not found", entity_kind="agent", entity_name=session.agent_name)
    agent_node = live_agent_node(agent, vm_node) if agent is not None else None
    session_node = live_session_node(session, template, agent=agent_node, workspace=workspace_node, vm=vm_node)
    session_node.harness_integration.prepare_context(
        _mgr._session_context_values(db, vm, workspace, session.name, SessionMode(session.mode), session.agent_name),
        intent=intent,
    )
    return SessionLaunchDefinition(
        db=db,
        config=config,
        registry=registry,
        session=session,
        workspace=workspace,
        vm=vm,
        agent=agent,
        template=template,
        vm_node=vm_node,
        workspace_node=workspace_node,
        agent_node=agent_node,
        session_node=session_node,
        nodes=tuple(walk(session_node)),
        scope=_mgr._session_scope(db, session, workspace, vm),
        env_target=_mgr._session_secret_target(
            registry,
            db=db,
            vm=vm,
            ws=workspace,
            session_name=session.name,
            session_template=template,
            mode=SessionMode(session.mode),
            agent_name=session.agent_name,
        ),
        replace_running=replace_running,
        running_restart_authorized=running_restart_authorized,
        force=force,
        intent=intent,
    )


def observe_session_runtime(
    session: SessionRow, *, admin_target: Transport, session_target: Transport | None = None
) -> SessionStatus:
    """Observe exact dedicated or legacy presence without repairing evidence."""
    if session.socket_path is not None:
        return _mgr.check_session_status(session, target=admin_target)
    from agentworks.sessions.tmux import ProbeStatus, probe_tmux_session

    target = session_target or admin_target
    run = (
        target.run if session_target is not None else partial(target.run, sudo=session.mode == SessionMode.AGENT.value)
    )
    presence = probe_tmux_session(session.name, run_command=run, socket_path=None)
    return {
        ProbeStatus.PRESENT: SessionStatus.RUNNING,
        ProbeStatus.ABSENT: SessionStatus.STOPPED,
        ProbeStatus.UNKNOWN: SessionStatus.UNKNOWN,
    }[presence]


def validate_session_observed_policy(
    session: SessionRow,
    status: SessionStatus,
    *,
    replace_running: bool,
    force: bool,
    intent: HarnessLaunchIntent,
    running_restart_authorized: bool,
) -> None:
    """Refuse policy conflicts proved by an available pre-accept observation."""
    if status is SessionStatus.BROKEN and not force:
        raise BrokenStateError(
            "session runtime requires explicit force recovery",
            entity_kind="session",
            entity_name=session.name,
        )
    if status is SessionStatus.RUNNING:
        if not replace_running and intent is HarnessLaunchIntent.FORCE_NEW:
            raise StateError("session is already running", entity_kind="session", entity_name=session.name)
        if replace_running and not running_restart_authorized:
            raise StateError(
                "session became running without restart authorization",
                entity_kind="session",
                entity_name=session.name,
            )


def require_session_launch_identity(definition: SessionLaunchDefinition) -> SessionRow:
    """Reload selected identity, parent, and target before a consequential action."""
    from agentworks.sessions.manager._lifecycle import _require_selected_session

    session = _require_selected_session(definition.db, definition.session.name, definition.session.session_uuid)
    workspace = _mgr._require_workspace(definition.db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(definition.db, workspace)
    if (
        workspace.name != definition.workspace.name
        or workspace.vm_name != definition.vm.name
        or vm.vm_uuid != definition.vm.vm_uuid
    ):
        raise StateError("session parent changed during preparation", entity_kind="session", entity_name=session.name)
    if session.desired_runtime_state != definition.session.desired_runtime_state:
        raise StateError("session target changed during preparation", entity_kind="session", entity_name=session.name)
    if vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
        raise StateError("session parent has stopped desired state", entity_kind="vm", entity_name=vm.name)
    return session


def prepare_session_launch(
    definition: SessionLaunchDefinition,
    *,
    interaction: TtyInteractionPolicy,
) -> PreparedSessionLaunch:
    """Classify eligibility and preflight this session without writing evidence."""
    from agentworks.capabilities.base import RunContext
    from agentworks.harness_setup.readiness import require_setup_ready
    from agentworks.sessions.tmux import startup_protection_enabled

    db, config, session = definition.db, definition.config, definition.session
    from agentworks.sessions.manager._lifecycle import _validated_socket_path

    current = require_session_launch_identity(definition)
    if current.workspace_name != definition.workspace.name:
        raise StateError(
            "session workspace changed during preparation", entity_kind="session", entity_name=session.name
        )
    from agentworks.vms.manager import require_vm_ssh_boundary

    require_vm_ssh_boundary(db, config, definition.vm)
    admin_target = _mgr.transport(definition.vm, config)
    status = observe_session_runtime(current, admin_target=admin_target)
    if status is SessionStatus.UNKNOWN:
        raise StateError("session runtime state is unknown", entity_kind="session", entity_name=session.name)
    if status is SessionStatus.BROKEN and not definition.force:
        raise BrokenStateError(
            "session runtime requires explicit force recovery", entity_kind="session", entity_name=session.name
        )
    recovered_startup = False
    if status is SessionStatus.RUNNING:
        if not definition.replace_running:
            if definition.intent is HarnessLaunchIntent.FORCE_NEW:
                raise StateError("session is already running", entity_kind="session", entity_name=session.name)
            if current.socket_path is not None:
                if _mgr._needs_repair(current):
                    recovered_startup = True  # status proved the interrupted launch's ownership
                else:
                    protection = startup_protection_enabled(
                        current.name,
                        socket_path=_validated_socket_path(db, current),
                        run_command=partial(admin_target.run, sudo=current.mode == SessionMode.AGENT.value),
                    )
                    if protection is None:
                        raise StateError(
                            "could not verify session startup completion",
                            entity_kind="session",
                            entity_name=session.name,
                        )
                    recovered_startup = protection
            if not recovered_startup:
                return PreparedSessionLaunch(
                    definition=definition,
                    admin_target=admin_target,
                    session_target=admin_target,
                    observed_status=status,
                    legacy=current.socket_path is None,
                    recovered_startup=False,
                    noop=True,
                )
        elif not definition.running_restart_authorized:
            raise StateError(
                "session became running without restart authorization", entity_kind="session", entity_name=session.name
            )
    target = _mgr._build_session_target(current, vm=definition.vm, config=config, db=db, admin_target=admin_target)
    if not recovered_startup or definition.replace_running:
        context = RunContext(
            config=config,
            operation_scope=definition.scope,
            admin_target=admin_target,
            agent_target=None if current.agent_name is None else target,
        )
        for node in definition.nodes:
            node.preflight(context)
        require_setup_ready(
            db,
            definition.registry,
            definition.session_node.harness_integration,
            vm=definition.vm,
            workspace=definition.workspace,
            agent_name=current.agent_name,
            runner=target,
        )
    return PreparedSessionLaunch(
        definition=definition,
        admin_target=admin_target,
        session_target=target,
        observed_status=status,
        legacy=current.socket_path is None,
        recovered_startup=recovered_startup,
        noop=False,
    )


def prepare_session_stop(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    parent_off: bool = False,
    force: bool = False,
) -> PreparedSessionStop:
    """Prepare direct teardown, never activating or holding the parent VM."""
    workspace = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)
    if not parent_off:
        from agentworks.vms.manager import require_vm_ssh_boundary

        require_vm_ssh_boundary(db, config, vm)
    admin_target = None if parent_off else _mgr.transport(vm, config)
    status = SessionStatus.STOPPED if parent_off else observe_session_runtime(session, admin_target=admin_target)  # type: ignore[arg-type]
    if status is SessionStatus.UNKNOWN:
        raise StateError("session runtime state is unknown", entity_kind="session", entity_name=session.name)
    if status is SessionStatus.BROKEN and not force:
        raise BrokenStateError(
            "session runtime requires explicit force recovery", entity_kind="session", entity_name=session.name
        )
    target = None
    if status is not SessionStatus.STOPPED:
        assert admin_target is not None
        target = _mgr._build_session_target(session, vm=vm, config=config, db=db, admin_target=admin_target)
    return PreparedSessionStop(
        db=db,
        config=config,
        session=session,
        workspace=workspace,
        vm=vm,
        admin_target=admin_target,
        session_target=target,
        observed_status=status,
        parent_off=parent_off,
        force=force,
    )


def execute_session_stop(prepared: PreparedSessionStop) -> bool:
    """Apply a prepared stop without changing accepted intent or resolving values."""
    from agentworks.sessions.manager._lifecycle import _require_selected_session

    session = _require_selected_session(prepared.db, prepared.session.name, prepared.session.session_uuid)
    workspace = _mgr._require_workspace(prepared.db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(prepared.db, workspace)
    if vm.vm_uuid != prepared.vm.vm_uuid or workspace.name != prepared.workspace.name:
        raise StateError("session parent changed before teardown", entity_kind="session", entity_name=session.name)
    if session.desired_runtime_state != prepared.session.desired_runtime_state:
        raise StateError("session target changed before teardown", entity_kind="session", entity_name=session.name)
    if prepared.parent_off or prepared.observed_status is SessionStatus.STOPPED:
        return False
    assert prepared.admin_target is not None and prepared.session_target is not None
    session = (
        _mgr._ensure_pid(session, target=prepared.admin_target, db=prepared.db)
        if session.socket_path is not None
        else session
    )
    _mgr._teardown_session(
        session,
        target=prepared.session_target,
        target_owns_session=True,
        db=prepared.db,
        force=prepared.force,
        legacy_running_authorized=True,
    )
    return True


def validate_session_values(prepared: PreparedSessionLaunch, values: Mapping[str, str]) -> None:
    """Validate the complete rendered environment before any deferred mutation."""
    if not prepared.requires_launch:
        return
    definition = prepared.definition
    _mgr._resolve_session_env(
        definition.registry,
        values=values,
        db=definition.db,
        vm=definition.vm,
        ws=definition.workspace,
        session_name=definition.session.name,
        session_template=definition.template,
        mode=SessionMode(definition.session.mode),
        agent_name=definition.session.agent_name,
        linux_user=_mgr._resolve_session_linux_user(definition.db, definition.session, definition.vm),
    )
