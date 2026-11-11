"""Prepare local policy, provider contexts, and concrete guest workloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import DesiredRuntimeState, SessionStatus, VMStatus
from agentworks.errors import StateError, UserAbort

from ._models import Outcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import SessionRow
    from agentworks.vms.nodes import LiveVMNode

    from ._models import Kind, RuntimeInvocation


OFF = frozenset((VMStatus.STOPPED, VMStatus.DEALLOCATED))


def session_vm_name(invocation: RuntimeInvocation, session: SessionRow) -> str:
    workspace = invocation.db.get_workspace(session.workspace_name)
    if workspace is None or workspace.vm_name not in invocation.vms:
        raise StateError("session parent is unavailable", entity_kind="session", entity_name=session.name)
    return workspace.vm_name


def prepare_local_workloads(invocation: RuntimeInvocation) -> None:
    import agentworks.sessions.manager as sessions
    from agentworks.sessions.multi_console.runtime import validate_console_definition

    for name, session in invocation.sessions.items():
        if invocation.unavailable("session", name):
            continue
        try:
            vm_name = session_vm_name(invocation, session)
            target = (
                DesiredRuntimeState.STOPPED
                if invocation.operation in {"stop", "stop-all"}
                else DesiredRuntimeState.RUNNING
                if invocation.operation is not None
                else session.desired_runtime_state
            )
            if target is None:
                invocation.fail("session", name, "desired state requires adoption", blocked=True)
            elif target is DesiredRuntimeState.RUNNING:
                invocation.definitions[name] = sessions.prepare_session_definition(
                    invocation.db,
                    invocation.config,
                    invocation.registry,
                    session,
                    vm_node=invocation.nodes[vm_name],
                    replace_running=invocation.operation == "restart",
                    force=invocation.force,
                    intent=invocation.intent,
                    running_restart_authorized=name in invocation.authorized_running,
                )
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("session", name, error, blocked=True)

    observations: dict[str, SessionStatus] = {}
    if invocation.operation in {"start", "restart"}:
        candidates = [
            session
            for name, session in invocation.sessions.items()
            if not invocation.unavailable("session", name)
            and name in invocation.definitions
            and invocation.vms[session_vm_name(invocation, session)].desired_runtime_state
            is not DesiredRuntimeState.STOPPED
        ]
        if candidates:
            try:
                observations = sessions.observe_session_statuses(
                    candidates, db=invocation.db, config=invocation.config, include_legacy=True
                )
            except UserAbort:
                raise
            except Exception:
                # Failed guest observation is operational uncertainty, not a local policy refusal.
                observations = {}

    for name, selected in tuple(invocation.sessions.items()):
        if invocation.unavailable("session", name):
            continue
        try:
            session = selected
            vm_name = session_vm_name(invocation, session)
            vm = invocation.vms[vm_name]
            target = (
                DesiredRuntimeState.STOPPED
                if invocation.operation in {"stop", "stop-all"}
                else DesiredRuntimeState.RUNNING
                if invocation.operation is not None
                else session.desired_runtime_state
            )
            assert target is not None
            if invocation.operation is not None:
                if target is DesiredRuntimeState.RUNNING:
                    definition = invocation.definitions[name]
                    sessions.validate_session_observed_policy(
                        session,
                        observations.get(name, SessionStatus.UNKNOWN),
                        replace_running=definition.replace_running,
                        force=definition.force,
                        intent=definition.intent,
                        running_restart_authorized=definition.running_restart_authorized,
                    )
                previous = session.desired_runtime_state
                session = sessions.accept_session_runtime_state(
                    invocation.db, session, state=target, held_native_guard=invocation.guards[vm_name]
                )
                invocation.sessions[name] = session
                invocation.record("session", name, desired_runtime_state=target)
                if previous != target:
                    invocation.accepted_changes.add(name)
                    if target is DesiredRuntimeState.RUNNING:
                        invocation.definitions[name] = sessions.prepare_session_definition(
                            invocation.db,
                            invocation.config,
                            invocation.registry,
                            session,
                            vm_node=invocation.nodes[vm_name],
                            replace_running=invocation.operation == "restart",
                            force=invocation.force,
                            intent=invocation.intent,
                            running_restart_authorized=name in invocation.authorized_running,
                        )
            if target is DesiredRuntimeState.RUNNING and vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
                invocation.fail("session", name, "parent VM has stopped desired state", blocked=True)
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("session", name, error, blocked=True)
    for name, console in invocation.consoles.items():
        if invocation.unavailable("console", name):
            continue
        if console.desired_runtime_state is None:
            invocation.fail("console", name, "desired state requires adoption", blocked=True)
        elif console.desired_runtime_state is DesiredRuntimeState.RUNNING:
            try:
                validate_console_definition(invocation.db, console)
                if invocation.vms[console.vm_name].desired_runtime_state is DesiredRuntimeState.STOPPED:
                    invocation.fail("console", name, "parent VM has stopped desired state", blocked=True)
            except UserAbort:
                raise
            except Exception as error:
                invocation.fail("console", name, error, blocked=True)


def infrastructure_reader(invocation: RuntimeInvocation, node: LiveVMNode) -> Callable[[str], str]:
    gate_names = frozenset(node.gate_secret_refs())

    def resolve(name: str) -> str:
        if name not in gate_names and name not in node.repair_secret_refs():
            raise StateError(
                "infrastructure requested an undeclared credential", entity_kind="vm", entity_name=node.row.name
            )
        return invocation.gate_value(name)

    return resolve


def _needs_running_guest(invocation: RuntimeInvocation, vm_name: str) -> bool:
    routes = dict(invocation.selection.session_routes)
    return any(
        not invocation.unavailable("session", name)
        and routes[name] == vm_name
        and session.desired_runtime_state is DesiredRuntimeState.RUNNING
        for name, session in invocation.sessions.items()
    ) or any(
        console.vm_name == vm_name
        and not invocation.unavailable("console", name)
        and console.desired_runtime_state is DesiredRuntimeState.RUNNING
        for name, console in invocation.consoles.items()
    )


def _needs_running_vm(invocation: RuntimeInvocation, vm_name: str) -> bool:
    target = invocation.vms[vm_name].desired_runtime_state
    return (
        vm_name in invocation.selection.explicit_vms
        and target is DesiredRuntimeState.RUNNING
        or target is not DesiredRuntimeState.STOPPED
        and _needs_running_guest(invocation, vm_name)
    )


def _block_family(invocation: RuntimeInvocation, vm_name: str, error: Exception | str) -> None:
    invocation.failed_families.add(vm_name)
    invocation.ensure_vm_result(vm_name)
    invocation.fail("vm", vm_name, error)
    routes = dict(invocation.selection.session_routes)
    for name in invocation.sessions:
        if not invocation.unavailable("session", name) and routes[name] == vm_name:
            invocation.fail("session", name, "parent infrastructure preparation failed", blocked=True)
    for name, console in invocation.consoles.items():
        if console.vm_name == vm_name and not invocation.unavailable("console", name):
            invocation.fail("console", name, "parent infrastructure preparation failed", blocked=True)


def prepare_infrastructure(invocation: RuntimeInvocation) -> None:
    import agentworks.vms.manager as vms

    for name, vm in invocation.vms.items():
        if name in invocation.failed_families:
            continue
        eligible = (
            name in invocation.selection.explicit_vms
            or any(
                session_vm_name(invocation, session) == name and not invocation.unavailable("session", session_name)
                for session_name, session in invocation.sessions.items()
            )
            or any(
                console.vm_name == name and not invocation.unavailable("console", console_name)
                for console_name, console in invocation.consoles.items()
            )
        )
        if not eligible:
            continue
        try:
            if _needs_running_vm(invocation, name):
                from agentworks.execution import trust

                trust.require_record(name)
            context = vms.prepare_vm_runtime(
                invocation.db,
                invocation.config,
                invocation.registry,
                vm,
                resolve_secret=infrastructure_reader(invocation, invocation.nodes[name]),
                held_native_guard=invocation.guards[name],
                vm_node=invocation.nodes[name],
            )
            invocation.contexts[name] = context
            if name in invocation.selection.explicit_vms:
                invocation.record("vm", name, observation=context.observed_status)
            if (
                name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.ON_DEMAND
                or name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.STOPPED
                and context.observed_status in OFF
            ):
                invocation.completed.add(("vm", name))
            if context.observed_status is VMStatus.UNKNOWN:
                _block_family(invocation, name, "provider power observation is unknown")
        except UserAbort:
            raise
        except Exception as error:
            _block_family(invocation, name, error)
    # Every provider context is prepared before any power action.
    for name, context in invocation.contexts.items():
        if name in invocation.failed_families:
            continue
        if not _needs_running_vm(invocation, name):
            continue
        try:
            changed = vms.activate_vm_runtime(context, resolve_secret=infrastructure_reader(invocation, context.node))
            invocation.stack.enter_context(context.node.hold_active())
            context.observed_status = vms.observe_vm_runtime(context)
            if context.observed_status is not VMStatus.RUNNING:
                raise StateError(
                    "VM is not running after infrastructure activation", entity_kind="vm", entity_name=name
                )
            if changed or name in invocation.selection.explicit_vms:
                invocation.ensure_vm_result(name)
                invocation.record(
                    "vm",
                    name,
                    observation=context.observed_status,
                    action="start" if context.started else "repair" if context.repaired else "unchanged",
                    outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED,
                )
                invocation.completed.add(("vm", name))
        except (UserAbort, KeyboardInterrupt):
            if context.started or context.repaired:
                invocation.ensure_vm_result(name)
                invocation.record("vm", name, action="start" if context.started else "repair", outcome=Outcome.CHANGED)
                invocation.completed.add(("vm", name))
            raise
        except Exception as error:
            _block_family(invocation, name, error)
            if context.started or context.repaired:
                invocation.record(
                    "vm",
                    name,
                    action="start completed; guest preparation failed" if context.started else "repair completed",
                )


def prepare_guest_workloads(invocation: RuntimeInvocation) -> None:
    import agentworks.sessions.manager as sessions
    import agentworks.sessions.multi_console as consoles
    from agentworks.transports import transport
    from agentworks.vms.manager import require_vm_ssh_boundary

    for name, session in invocation.sessions.items():
        if invocation.unavailable("session", name):
            continue
        vm_name = session_vm_name(invocation, session)
        vm, context = invocation.vms[vm_name], invocation.contexts[vm_name]
        try:
            if session.desired_runtime_state is DesiredRuntimeState.RUNNING:
                prepared = sessions.prepare_session_launch(invocation.definitions[name])
                invocation.launches[name] = prepared
                invocation.record(
                    "session",
                    name,
                    observation=prepared.observed_status,
                    action="checkpoint-runtime-identity"
                    if prepared.checkpoint_only
                    else "unchanged"
                    if prepared.noop
                    else "start"
                    if prepared.requires_launch
                    else "recover-startup",
                )
            elif invocation.operation in {"stop", "stop-all"}:
                # Explicit stop repairs through admin access after the secret barrier.
                invocation.record("session", name, action="stop")
            elif (
                vm_name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.STOPPED
                and context.observed_status not in OFF
            ):
                invocation.record("session", name, action="parent-stop")
            else:
                prepared_stop = sessions.prepare_session_stop(
                    invocation.db,
                    invocation.config,
                    session,
                    parent_off=context.observed_status in OFF,
                    force=invocation.force,
                )
                invocation.stops[name] = prepared_stop
                invocation.record(
                    "session",
                    name,
                    observation=prepared_stop.observed_status,
                    action="checkpoint-runtime-identity" if prepared_stop.checkpoint_only else "stop",
                )
        except UserAbort:
            raise
        except Exception as error:
            if session.desired_runtime_state is DesiredRuntimeState.STOPPED:
                _guest_stop_failure(invocation, "session", name, vm_name, error)
            else:
                invocation.fail("session", name, error, blocked=True)
    for name, console in invocation.consoles.items():
        if invocation.unavailable("console", name):
            continue
        vm, context = invocation.vms[console.vm_name], invocation.contexts[console.vm_name]
        try:
            if (
                console.desired_runtime_state is DesiredRuntimeState.STOPPED
                and vm.name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.STOPPED
                and context.observed_status not in OFF
            ):
                invocation.record("console", name, action="parent-stop")
                continue
            target = None
            if context.observed_status not in OFF:
                require_vm_ssh_boundary(invocation.db, invocation.config, vm)
                target = transport(vm, invocation.config)
            if console.desired_runtime_state is DesiredRuntimeState.RUNNING:
                assert target is not None
                prepared_console = consoles.prepare_console_reconcile(
                    invocation.db,
                    invocation.registry,
                    console=console,
                    vm=vm,
                    target=target,
                    held_native_guard=invocation.guards[vm.name],
                )
                invocation.console_starts[name] = prepared_console
                invocation.record("console", name, action=prepared_console.action.value)
            else:
                prepared_console_stop = consoles.prepare_console_stop(
                    invocation.db, console=console, vm=vm, target=target, held_native_guard=invocation.guards[vm.name]
                )
                invocation.console_stops[name] = prepared_console_stop
                invocation.record("console", name, action="stop")
        except UserAbort:
            raise
        except Exception as error:
            if console.desired_runtime_state is DesiredRuntimeState.STOPPED:
                _guest_stop_failure(invocation, "console", name, vm.name, error)
            else:
                invocation.fail("console", name, error, blocked=True)


def _guest_stop_failure(invocation: RuntimeInvocation, kind: Kind, name: str, vm_name: str, error: Exception) -> None:
    from agentworks.vms.manager import observe_vm_runtime

    try:
        if observe_vm_runtime(invocation.contexts[vm_name]) in OFF:
            invocation.record(
                kind,
                name,
                action="parent-off",
                outcome=Outcome.CHANGED if name in invocation.accepted_changes else Outcome.UNCHANGED,
            )
            return
    except UserAbort:
        raise
    except Exception:
        pass
    invocation.fail(kind, name, error, blocked=True)
