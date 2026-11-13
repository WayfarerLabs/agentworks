"""Read-only previews use provider credentials and direct bounded observers."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.db import DesiredRuntimeState, SessionStatus, VMStatus
from agentworks.errors import AgentworksError, StateError, UserAbort
from agentworks.sessions.tmux import ProbeStatus

from ._models import Outcome, RuntimeInvocation, RuntimeResult, render_results, require_success
from ._prepare import (
    OFF,
    _block_family,
    _needs_running_vm,
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
                try:
                    current = db.get_vm(selected.name)
                    if current is None or current.vm_uuid != selected.vm_uuid:
                        raise StateError(
                            "VM changed during preview selection", entity_kind="vm", entity_name=selected.name
                        )
                    invocation.vms[current.name] = current
                    if ("vm", current.name) in invocation.results:
                        invocation.results[("vm", current.name)] = replace(
                            invocation.results[("vm", current.name)],
                            desired_runtime_state=current.desired_runtime_state,
                        )
                    invocation.nodes[current.name] = live_vm_node(db, config, registry, current, site_nodes=site_nodes)
                except UserAbort:
                    raise
                except AgentworksError as error:
                    invocation.failed_families.add(selected.name)
                    invocation.results.setdefault(
                        ("vm", selected.name),
                        RuntimeResult(
                            kind="vm", name=selected.name, desired_runtime_state=selected.desired_runtime_state
                        ),
                    )
                    invocation.fail("vm", selected.name, error, blocked=True)
                    for name, parent in selection.session_routes:
                        if parent == selected.name:
                            invocation.fail("session", name, "parent VM definition is unavailable", blocked=True)
            for selected_session in selection.sessions:
                if invocation.unavailable("session", selected_session.name):
                    continue
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
                    invocation.results[("console", name)] = RuntimeResult(
                        kind="console", name=name, desired_runtime_state=None, target_known=False
                    )
                    invocation.fail("console", name, "console changed during preview selection", blocked=True)
                    continue
                invocation.consoles[name] = console
                invocation.results[("console", name)] = RuntimeResult(
                    kind="console", name=name, desired_runtime_state=console.desired_runtime_state
                )
                if parent in invocation.failed_families:
                    invocation.fail("console", name, "parent VM definition is unavailable", blocked=True)
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
    from agentworks.execution import trust

    for name, vm in invocation.vms.items():
        if name in invocation.failed_families:
            continue
        try:
            wants_running = _needs_running_vm(invocation, name)
            if wants_running:
                trust.require_record(name)
            context = vms.prepare_vm_runtime(
                invocation.db,
                invocation.config,
                invocation.registry,
                vm,
                resolve_secret=invocation.gate_value,
                vm_node=invocation.nodes[name],
            )
            invocation.contexts[name] = context
            power = context.observed_status
            if power is VMStatus.UNKNOWN:
                _block_family(invocation, name, "provider power observation is unknown")
                continue
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

    candidates = []
    for name, session in invocation.sessions.items():
        if invocation.unavailable("session", name):
            continue
        try:
            context = invocation.contexts[session_vm_name(invocation, session)]
            if context.observed_status is VMStatus.RUNNING:
                candidates.append(session)
        except UserAbort:
            raise
        except AgentworksError as error:
            invocation.fail("session", name, error, blocked=True)
    observations = (
        sessions.observe_session_statuses(candidates, db=invocation.db, config=invocation.config, include_legacy=True)
        if candidates
        else {}
    )
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
                status = observations.get(name, SessionStatus.UNKNOWN)
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
                observation_from_parent=power in OFF,
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
                observation_from_parent=context.observed_status in OFF,
                action=action,
                outcome=Outcome.UNCHANGED if action == "unchanged" else Outcome.CHANGED,
            )
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("console", name, error, blocked=True)
