"""Local selection and exact guarded resource reloads."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import AgentworksError, NotFoundError, StateError, UserAbort, ValidationError

from ._models import RuntimeResult, RuntimeSelection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.vms.nodes import VMSiteNode

    from ._models import RuntimeInvocation


def _vm(db: Database, name: str) -> VMRow:
    vm = db.get_vm(name)
    if vm is None:
        raise NotFoundError(f"VM '{name}' not found", entity_kind="vm", entity_name=name)
    return vm


def session_selection(db: Database, sessions: Sequence[SessionRow]) -> RuntimeSelection:
    vms = {}
    routes: list[tuple[str, str]] = []
    for session in sessions:
        workspace = db.get_workspace(session.workspace_name)
        if workspace is None:
            raise NotFoundError(
                "session workspace not found", entity_kind="workspace", entity_name=session.workspace_name
            )
        vms[workspace.vm_name] = _vm(db, workspace.vm_name)
        routes.append((session.name, workspace.vm_name))
    return RuntimeSelection(tuple(vms.values()), tuple(sessions), session_routes=tuple(routes))


def select_vm_scope(db: Database, *, name: str | None, all_vms: bool) -> RuntimeSelection:
    if (name is not None) == all_vms:
        raise ValidationError("select one VM name or all VMs", entity_kind="vm")
    if all_vms:
        vms = tuple(db.list_vms())
    else:
        assert name is not None
        vms = (_vm(db, name),)
    sessions = []
    routes: list[tuple[str, str]] = []
    for vm in vms:
        children = db.list_sessions(vm_name=vm.name)
        sessions.extend(children)
        routes.extend((session.name, vm.name) for session in children)
    return RuntimeSelection(
        vms,
        tuple(sessions),
        explicit_vms=frozenset(vm.name for vm in vms),
        expand_consoles=True,
        session_routes=tuple(routes),
    )


def select_session_scope(db: Database, name: str) -> RuntimeSelection:
    session = db.get_session(name)
    if session is None:
        raise NotFoundError(f"session '{name}' not found", entity_kind="session", entity_name=name)
    return session_selection(db, [session])


def select_console_scope(db: Database, name: str) -> RuntimeSelection:
    console = db.get_console(name)
    if console is None:
        raise NotFoundError(f"console '{name}' not found", entity_kind="console", entity_name=name)
    return RuntimeSelection((_vm(db, console.vm_name),), console_routes=((name, console.vm_name),))


def seed_results(invocation: RuntimeInvocation) -> None:
    for vm in invocation.selection.vms:
        if vm.name in invocation.selection.explicit_vms:
            invocation.results[("vm", vm.name)] = RuntimeResult(
                kind="vm", name=vm.name, desired_runtime_state=vm.desired_runtime_state
            )
    for session in invocation.selection.sessions:
        target = session.desired_runtime_state
        invocation.results[("session", session.name)] = RuntimeResult(
            kind="session", name=session.name, desired_runtime_state=target
        )


def acquire_families(invocation: RuntimeInvocation) -> None:
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.vms.nodes import live_vm_node

    site_nodes: dict[str, VMSiteNode] = {}
    session_routes = dict(invocation.selection.session_routes)
    for selected in sorted(invocation.selection.vms, key=lambda vm: vm.name):
        try:
            guard = invocation.stack.enter_context(native_mutation_guard(invocation.db.path, selected.name))
            current = _vm(invocation.db, selected.name)
            if current.vm_uuid != selected.vm_uuid:
                raise StateError("VM was replaced during selection", entity_kind="vm", entity_name=selected.name)
            invocation.guards[current.name] = guard
            invocation.vms[current.name] = current
            if ("vm", current.name) in invocation.results:
                invocation.results[("vm", current.name)] = replace(
                    invocation.results[("vm", current.name)], desired_runtime_state=current.desired_runtime_state
                )
            invocation.nodes[current.name] = live_vm_node(
                invocation.db,
                invocation.config,
                invocation.registry,
                current,
                held_native_guard=guard,
                site_nodes=site_nodes,
            )
        except UserAbort:
            raise
        except AgentworksError as error:
            invocation.failed_families.add(selected.name)
            if ("vm", selected.name) in invocation.results:
                invocation.fail("vm", selected.name, error, blocked=True)
            for session in invocation.selection.sessions:
                if session_routes[session.name] == selected.name:
                    invocation.fail("session", session.name, error, blocked=True)
    for selected_session in invocation.selection.sessions:
        if invocation.unavailable("session", selected_session.name):
            continue
        try:
            current_session = invocation.db.get_session(selected_session.name)
            workspace = (
                invocation.db.get_workspace(current_session.workspace_name) if current_session is not None else None
            )
            if (
                current_session is None
                or current_session.session_uuid != selected_session.session_uuid
                or workspace is None
                or current_session.workspace_name != selected_session.workspace_name
                or workspace.vm_name != session_routes[selected_session.name]
                or workspace.vm_name not in invocation.guards
            ):
                raise StateError(
                    "session identity or parent changed during selection",
                    entity_kind="session",
                    entity_name=selected_session.name,
                )
            selected_workspace = invocation.db.get_workspace(selected_session.workspace_name)
            assert selected_workspace is not None
            invocation.sessions[current_session.name] = current_session
            invocation.results[("session", current_session.name)] = replace(
                invocation.results[("session", current_session.name)],
                desired_runtime_state=current_session.desired_runtime_state,
            )
        except UserAbort:
            raise
        except AgentworksError as error:
            invocation.fail("session", selected_session.name, error, blocked=True)
    routes = list(invocation.selection.console_routes)
    if invocation.selection.expand_consoles:
        routes.extend(
            (console.name, vm.name)
            for vm in invocation.vms.values()
            for console in invocation.db.list_consoles(vm_name=vm.name)
        )
    for name, parent in routes:
        current_console = invocation.db.get_console(name)
        if current_console is None or current_console.vm_name != parent or parent not in invocation.guards:
            invocation.results[("console", name)] = RuntimeResult(
                kind="console", name=name, desired_runtime_state=None, target_known=False
            )
            invocation.fail("console", name, "console parent changed or its VM family is unavailable", blocked=True)
            continue
        invocation.consoles[name] = current_console
        invocation.results[("console", name)] = RuntimeResult(
            kind="console",
            name=name,
            desired_runtime_state=current_console.desired_runtime_state,
        )
