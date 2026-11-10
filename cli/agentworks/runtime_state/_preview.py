"""Read-only previews use provider credentials and direct bounded observers."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import DesiredRuntimeState, SessionStatus, VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.sessions.tmux import ProbeStatus

from ._models import Outcome, RuntimeInvocation, RuntimeResult, render_results, require_success
from ._prepare import (
    OFF,
    _block_family,
    _needs_running_guest,
    infrastructure_reader,
    prepare_local_workloads,
    session_vm_name,
)
from ._selection import seed_results

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import VMSiteNode

    from ._models import RuntimeSelection


def preview_selected(
    db: Database, config: Config, selection: RuntimeSelection, *, interaction: TtyInteractionPolicy
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    registry = load_request_registry(config, live_database=db)
    resolver = Resolver(config, registry, interaction=interaction)
    # This stack never receives mutation guards, active holds, or transient routes.
    with ExitStack() as stack:
        invocation = RuntimeInvocation(
            db=db,
            config=config,
            registry=registry,
            resolver=resolver,
            interaction=interaction,
            selection=selection,
            stack=stack,
            intent=HarnessLaunchIntent.RESUME_OR_NEW,
        )
        seed_results(invocation)
        try:
            site_nodes: dict[str, VMSiteNode] = {}
            for selected in selection.vms:
                current = db.get_vm(selected.name)
                if current is None or current.vm_uuid != selected.vm_uuid:
                    raise StateError("VM changed during preview selection", entity_kind="vm", entity_name=selected.name)
                invocation.vms[current.name] = current
                if ("vm", current.name) in invocation.results:
                    invocation.results[("vm", current.name)] = replace(
                        invocation.results[("vm", current.name)], desired_runtime_state=current.desired_runtime_state
                    )
                invocation.nodes[current.name] = live_vm_node(db, config, registry, current, site_nodes=site_nodes)
            for selected_session in selection.sessions:
                session = db.get_session(selected_session.name)
                workspace = db.get_workspace(session.workspace_name) if session is not None else None
                if (
                    session is None
                    or session.session_uuid != selected_session.session_uuid
                    or workspace is None
                    or session.workspace_name != selected_session.workspace_name
                    or workspace.vm_name != dict(selection.session_routes)[selected_session.name]
                ):
                    invocation.fail(
                        "session",
                        selected_session.name,
                        "session identity or parent changed during preview",
                        blocked=True,
                    )
                    continue
                invocation.sessions[session.name] = session
                invocation.results[("session", session.name)] = replace(
                    invocation.results[("session", session.name)],
                    desired_runtime_state=session.desired_runtime_state,
                )
            routes = list(selection.console_routes)
            if selection.expand_consoles:
                routes.extend(
                    (console.name, vm.name) for vm in selection.vms for console in db.list_consoles(vm_name=vm.name)
                )
            for name, parent in routes:
                console = db.get_console(name)
                if console is None or console.vm_name != parent:
                    raise StateError(
                        "console changed during preview selection", entity_kind="console", entity_name=name
                    )
                invocation.consoles[name] = console
                invocation.results[("console", name)] = RuntimeResult(
                    kind="console", name=name, desired_runtime_state=console.desired_runtime_state
                )
            prepare_local_workloads(invocation)
            _observe_providers(invocation)
            _preview_sessions(invocation)
            _preview_consoles(invocation)
            results = tuple(invocation.results.values())
            render_results(results, dry_run=True)
            require_success(results)
        except UserAbort:
            render_results(tuple(invocation.results.values()), dry_run=True)
            raise
        finally:
            invocation.close()


def _observe_providers(invocation: RuntimeInvocation) -> None:

    import agentworks.vms.manager as vms

    for name, vm in invocation.vms.items():
        try:
            context = vms.prepare_vm_runtime(
                invocation.db,
                invocation.config,
                invocation.registry,
                vm,
                resolve_secret=infrastructure_reader(invocation, invocation.nodes[name]),
                vm_node=invocation.nodes[name],
            )
            invocation.contexts[name] = context
            power = context.observed_status
            if power is VMStatus.UNKNOWN:
                _block_family(invocation, name, "provider power observation is unknown")
                continue
            wants_running = (
                name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.RUNNING
                or vm.desired_runtime_state is not DesiredRuntimeState.STOPPED
                and _needs_running_guest(invocation, name)
            )
            changed = (
                wants_running
                and power in OFF
                or name in invocation.selection.explicit_vms
                and vm.desired_runtime_state is DesiredRuntimeState.STOPPED
                and power is VMStatus.RUNNING
            )
            if name in invocation.selection.explicit_vms or changed:
                invocation.ensure_vm_result(name)
                invocation.results[("vm", name)] = replace(
                    invocation.results[("vm", name)],
                    observation=power,
                    action="start" if wants_running and power in OFF else "stop" if changed else "inspect",
                    outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED,
                )
        except UserAbort:
            raise
        except Exception as error:
            _block_family(invocation, name, error)


def _preview_sessions(invocation: RuntimeInvocation) -> None:

    import agentworks.sessions.manager as sessions

    for name, session in invocation.sessions.items():
        if invocation.unavailable("session", name):
            continue
        try:
            context = invocation.contexts[session_vm_name(invocation, session)]
            power = context.observed_status
            target = session.desired_runtime_state
            if power in OFF:
                status = SessionStatus.STOPPED
                action = "conditional-start" if target is DesiredRuntimeState.RUNNING else "unchanged"
                reason = (
                    "guest launch checks remain conditional after VM activation"
                    if target is DesiredRuntimeState.RUNNING
                    else None
                )
                changed = target is DesiredRuntimeState.RUNNING
            elif power is VMStatus.RUNNING:
                status = sessions.observe_session_statuses(
                    [session], db=invocation.db, config=invocation.config, include_legacy=True
                ).get(name, SessionStatus.UNKNOWN)
                if status is SessionStatus.UNKNOWN:
                    raise StateError("guest session observation is unknown")
                if status is SessionStatus.BROKEN:
                    raise StateError("guest session requires explicit force recovery")
                changed = status is not (
                    SessionStatus.RUNNING if target is DesiredRuntimeState.RUNNING else SessionStatus.STOPPED
                )
                action = (
                    "start" if target is DesiredRuntimeState.RUNNING and changed else "stop" if changed else "unchanged"
                )
                reason = None
                if (
                    target is DesiredRuntimeState.RUNNING
                    and status is SessionStatus.RUNNING
                    and sessions._needs_repair(session)
                ):
                    changed, action = True, "recover-startup"
            else:
                raise StateError("provider power observation is unknown")
            invocation.results[("session", name)] = replace(
                invocation.results[("session", name)],
                observation=status,
                action=action,
                reason=reason,
                outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED,
            )
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("session", name, error, blocked=True)


def _preview_consoles(invocation: RuntimeInvocation) -> None:

    import agentworks.sessions.multi_console as consoles
    from agentworks.sessions.multi_console._status import classify_console_status
    from agentworks.transports import transport
    from agentworks.vms.manager import require_vm_ssh_boundary

    for name, console in invocation.consoles.items():
        if invocation.unavailable("console", name):
            continue
        try:
            context = invocation.contexts[console.vm_name]
            if context.observed_status in OFF:
                canonical, staging = ProbeStatus.ABSENT, ProbeStatus.ABSENT
            elif context.observed_status is VMStatus.RUNNING:
                require_vm_ssh_boundary(invocation.db, invocation.config, context.vm)
                canonical, staging = consoles._console_runtime_presence(transport(context.vm, invocation.config), name)
                if ProbeStatus.UNKNOWN in {canonical, staging}:
                    raise StateError("console runtime observation is unknown")
            else:
                raise StateError("provider power observation is unknown")
            running = console.desired_runtime_state is DesiredRuntimeState.RUNNING
            action = (
                (
                    "cleanup-staging"
                    if canonical is ProbeStatus.PRESENT and staging is ProbeStatus.PRESENT
                    else "unchanged"
                    if canonical is ProbeStatus.PRESENT
                    else "conditional-build"
                    if context.observed_status in OFF
                    else "build"
                )
                if running
                else ("stop" if ProbeStatus.PRESENT in {canonical, staging} else "unchanged")
            )
            invocation.results[("console", name)] = replace(
                invocation.results[("console", name)],
                observation=classify_console_status(
                    canonical_present=canonical is ProbeStatus.PRESENT, staging_present=staging is ProbeStatus.PRESENT
                ),
                action=action,
                outcome=Outcome.UNCHANGED if action == "unchanged" else Outcome.CHANGED,
            )
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("console", name, error, blocked=True)
