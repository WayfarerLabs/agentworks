"""Workspace/VM/session resolution and VM activation boundaries."""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.errors import (
    AgentworksError,
    NotFoundError,
    StateError,
    ValidationError,
)
from agentworks.name_filters import validate_name_filters
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


def _require_workspace(db: Database, name: str) -> WorkspaceRow:
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
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


def _session_scope(db: Database, session: SessionRow, ws: WorkspaceRow, vm: VMRow) -> OperationScope:
    """The singular session ops' SESSION-level operation scope: the
    operation is about the session (running as its agent, or as the
    admin), even though the composed graph is the live VM alone; pass
    the level of the entity the command is ABOUT, not of what it
    walks. The SESSION level's field rules (required vm + workspace +
    session; exactly one of agent/admin) are enforced by the scope's
    own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws.name,
        session=session.name,
        agent=session.agent_name,
        admin=session.agent_name is None,
    )


@contextlib.contextmanager
def _prepare_vm(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    operation: str | None = None,
    held_native_guard: NativeMutationGuard | None = None,
    interaction: TtyInteractionPolicy,
) -> Iterator[tuple[WorkspaceRow, VMRow, RunCommand, RunCommand, Transport]]:
    """The singular session ops' composition root (stop / delete /
    describe / attach / logs): validate the session's workspace and VM
    rows, then yield ``(ws, vm, run_command, run_as_root, target)``
    INSIDE ``gated_vm_boundary``'s held-active span, which replaces
    the imperative bind + point gate and the callers' own ``vm_active``
    holds. The scope is SESSION-level, built here from the session row
    once the vm/workspace rows it names are resolved: the ops are
    about the session, not the VM they walk. If ``operation`` is set,
    an SSHLogger attaches to the Transport so all calls log
    automatically.
    """
    from agentworks.bootstrap import load_request_registry
    from agentworks.ssh import SSHLogger

    ws = _mgr._require_workspace(db, session.workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    # Cheap row validation stays pre-gate: a VM with no Tailscale
    # address can never serve a session op, so it must fail with zero
    # prompts and zero VM starts. (The imperative body checked this
    # after its gate; the gate never changes the row's address, so the
    # command's outcome is identical.)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )

    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        scope=_session_scope(db, session, ws, vm),
        interaction=interaction,
        held_native_guard=held_native_guard,
    ):
        logger = SSHLogger(vm.name, operation) if operation else None
        target = _mgr.transport(vm, config, logger=logger)
        run_command: RunCommand = target.run
        run_as_root: RunCommand = partial(target.run, sudo=True)
        yield ws, vm, run_command, run_as_root, target


def _require_session(db: Database, name: str) -> SessionRow:
    session = db.get_session(name)
    if session is None:
        raise NotFoundError(
            f"session '{name}' not found",
            entity_kind="session",
            entity_name=name,
        )
    return session


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Regenerate the workspace tmuxinator config from current session state."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.workspaces.templates import resolve_live_tmuxinator
    from agentworks.workspaces.tmuxinator import generate_config, remove_config

    registry = load_request_registry(config, live_database=db)
    target = _mgr.transport(vm, config, logger=logger)
    if not resolve_live_tmuxinator(db, registry, ws.name, ws.template):
        remove_config(target, ws.name, ws.workspace_path)
        return

    sessions = db.list_sessions(workspace_name=ws.name)
    # Build socket paths for tmuxinator (admin sessions have NULL, agent sessions always set)
    socket_paths = {s.name: s.socket_path for s in sessions}
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions, socket_paths=socket_paths)
    target.write_file(f"{ws.workspace_path}/.tmuxinator.yml", config_text)


def filter_sessions(
    db: Database,
    *,
    config: Config | None = None,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
) -> list[SessionRow]:
    """Load sessions with optional relationship, integration, and mode filters.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``admin_only``
    restricts to admin-mode sessions (no agent); it is mutually
    exclusive with ``agent_name`` at the caller level. See
    ``Database.list_sessions``.

    An unknown name in any filter raises ``NotFoundError`` rather than
    matching nothing; every element of a list filter is checked.
    ``_filter_sessions_with_registry`` is the shared validation point for the
    batch session surface. This public function is its row-only facade; listing
    calls the shared helper directly when it must reuse the filter registry for
    display.

    A harness-integration filter builds one offline finalized registry, then
    validates every requested resource before considering enablement and
    readiness. Disabled or not-ready values warn and are excluded. With no
    harness-integration filter, the existing database-only path is preserved.
    """
    sessions, _registry = _filter_sessions_with_registry(
        db,
        config=config,
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        harness_integration_name=harness_integration_name,
        console_name=console_name,
        admin_only=admin_only,
    )
    return sessions


def _filter_sessions_with_registry(
    db: Database,
    *,
    config: Config | None = None,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    harness_integration_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
    admin_only: bool = False,
) -> tuple[list[SessionRow], Registry | None]:
    """Validate batch filters and return rows plus any integration registry."""
    registry: Registry | None = None
    selected_integrations: frozenset[str] | None = None
    if harness_integration_name is not None:
        if config is None:
            raise ValidationError(
                "config is required with a harness-integration filter",
                entity_kind="harness-integration",
            )
        from agentworks.bootstrap import load_request_registry

        registry = load_request_registry(config, include_live_resources=False)
        selected_integrations = _available_harness_integrations(registry, harness_integration_name)

    validate_name_filters(
        db,
        vm_name=vm_name,
        workspace_name=workspace_name,
        agent_name=agent_name,
        console_name=console_name,
    )
    sessions = db.list_sessions(
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        console_name=console_name,
        admin_only=admin_only,
    )
    if selected_integrations is None:
        return sessions, None
    assert registry is not None
    if not selected_integrations:
        return [], registry

    from agentworks.sessions.templates import resolve_live_template

    selected: list[SessionRow] = []
    for session in sessions:
        try:
            template = resolve_live_template(db, registry, session.name, session.template)
        except AgentworksError as exc:
            output.warn(
                f"Skipping session '{session.name}' for harness-integration filtering: "
                f"its effective template could not be resolved ({exc})."
            )
            continue
        if template.harness_integration in selected_integrations:
            selected.append(session)
    return selected, registry


def _available_harness_integrations(
    registry: Registry,
    requested: str | list[str],
) -> frozenset[str]:
    """Validate and reduce requested integration names in contract order."""
    values = [requested] if isinstance(requested, str) else list(requested)
    unique_values = list(dict.fromkeys(values))
    from agentworks.resources.access import ResourceIdentity, resolve_resource

    # Resolve the complete input before emitting availability warnings. A
    # mixed known/unknown filter is therefore one hard error, never a partial
    # selection whose earlier values happened to produce presentation.
    for name in unique_values:
        resolve_resource(registry, ResourceIdentity("harness-integration", name))

    from agentworks.resources.graph import Enablement

    enabled: list[str] = []
    for name in unique_values:
        if registry.graph.enablement_of("harness-integration", name) is Enablement.disabled:
            output.warn(f"Harness integration '{name}' is disabled; ignoring this filter value.")
        else:
            enabled.append(name)

    ready: list[str] = []
    for name in enabled:
        readiness = registry.graph.readiness_of("harness-integration", name)
        if not readiness.is_ready:
            detail = f": {readiness.reason}" if readiness.reason else ""
            output.warn(f"Harness integration '{name}' is not ready{detail}; ignoring this filter value.")
        else:
            ready.append(name)
    return frozenset(ready)
