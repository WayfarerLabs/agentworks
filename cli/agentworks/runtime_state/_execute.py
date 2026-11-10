"""The complete workload boundary, deferred actions, and bounded final checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import DesiredRuntimeState, SessionStatus, VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.sessions.multi_console._status import ConsoleStatus

from ._models import Outcome
from ._prepare import OFF, _guest_stop_failure, session_vm_name

if TYPE_CHECKING:
    from ._models import RuntimeInvocation


def resolve_workload_boundary(invocation: RuntimeInvocation) -> bool:
    import agentworks.sessions.manager as sessions
    import agentworks.sessions.multi_console as consoles
    from agentworks.orchestration.secrets import secret_union

    deferred_vm_stop = any(
        name in invocation.selection.explicit_vms
        and context.vm.desired_runtime_state is DesiredRuntimeState.STOPPED
        and name not in invocation.failed_families
        for name, context in invocation.contexts.items()
    )
    if not (
        invocation.launches
        or invocation.stops
        or invocation.console_starts
        or invocation.console_stops
        or deferred_vm_stop
    ):
        return True
    try:
        for prepared in invocation.launches.values():
            for name in secret_union(prepared.secret_nodes):
                invocation.resolver.register_name(name)
            invocation.resolver.register_targets(prepared.secret_targets, allow_transient_auto_declare=True)
        for console in invocation.console_starts.values():
            invocation.resolver.register_targets(console.pane_targets, allow_transient_auto_declare=True)
        invocation.resolver.resolve()
        invocation.values = invocation.resolver.values
        invocation.redactions.extend(invocation.values.values())
        for prepared in invocation.launches.values():
            sessions.validate_session_values(prepared, invocation.values)
        for console in invocation.console_starts.values():
            consoles.validate_console_values(console, invocation.values)
        return True
    except UserAbort:
        raise
    except Exception as error:
        for kind, name in tuple(invocation.results):
            if (kind, name) not in invocation.completed and not invocation.unavailable(kind, name):
                invocation.fail(kind, name, error, blocked=True)
        return False


def execute_deferred_work(invocation: RuntimeInvocation) -> None:
    import agentworks.sessions.manager as sessions
    import agentworks.sessions.multi_console as consoles
    import agentworks.vms.manager as vms

    for name, prepared in invocation.launches.items():
        try:
            changed = sessions.execute_session_start(
                prepared, secret_values=invocation.values, graph_secret_values=invocation.values
            )
            invocation.record(
                "session",
                name,
                outcome=Outcome.CHANGED if changed or name in invocation.accepted_changes else Outcome.UNCHANGED,
            )
            invocation.completed.add(("session", name))
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("session", name, error)
    for name, prepared_stop in invocation.stops.items():
        try:
            changed = sessions.execute_session_stop(prepared_stop)
            invocation.record(
                "session",
                name,
                outcome=Outcome.CHANGED if changed or name in invocation.accepted_changes else Outcome.UNCHANGED,
            )
            invocation.completed.add(("session", name))
        except UserAbort:
            raise
        except Exception as error:
            _guest_stop_failure(invocation, "session", name, prepared_stop.vm.name, error)
    for name, prepared_console in invocation.console_starts.items():
        try:
            changed = consoles.execute_console_reconcile(prepared_console, values=invocation.values)
            invocation.record("console", name, outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED)
            invocation.completed.add(("console", name))
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("console", name, error)
    for name, console_stop in invocation.console_stops.items():
        try:
            changed = consoles.execute_console_stop(console_stop)
            invocation.record("console", name, outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED)
            invocation.completed.add(("console", name))
        except UserAbort:
            raise
        except Exception as error:
            _guest_stop_failure(invocation, "console", name, console_stop.vm.name, error)
    for name, context in invocation.contexts.items():
        if (
            name not in invocation.selection.explicit_vms
            or name in invocation.failed_families
            or context.vm.desired_runtime_state is not DesiredRuntimeState.STOPPED
        ):
            continue
        try:
            changed = vms.execute_vm_stop(context)
            invocation.record("vm", name, action="stop", outcome=Outcome.CHANGED if changed else Outcome.UNCHANGED)
            invocation.completed.add(("vm", name))
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("vm", name, error)


def final_observations(invocation: RuntimeInvocation) -> None:
    import agentworks.sessions.manager as sessions
    import agentworks.sessions.multi_console as consoles
    import agentworks.vms.manager as vms

    powers = {}
    for name, context in invocation.contexts.items():
        if name in invocation.failed_families:
            continue
        try:
            power = vms.observe_vm_runtime(context)
            powers[name] = power
            if ("vm", name) in invocation.results:
                invocation.record("vm", name, observation=power)
                if power is VMStatus.UNKNOWN:
                    invocation.fail("vm", name, "final provider power observation is unknown", blocked=True)
                elif name in invocation.selection.explicit_vms and (
                    context.vm.desired_runtime_state is DesiredRuntimeState.RUNNING
                    and power is not VMStatus.RUNNING
                    or context.vm.desired_runtime_state is DesiredRuntimeState.STOPPED
                    and power not in OFF
                ):
                    invocation.fail("vm", name, "final provider observation does not satisfy desired state")
        except UserAbort:
            raise
        except Exception as error:
            invocation.ensure_vm_result(name)
            invocation.fail("vm", name, error, blocked=True)
    for name, selected in invocation.sessions.items():
        if invocation.results[("session", name)].outcome is Outcome.BLOCKED:
            continue
        was_failed = invocation.results[("session", name)].outcome is Outcome.FAILED
        try:
            current = invocation.db.get_session(name)
            if (
                current is None
                or current.session_uuid != selected.session_uuid
                or current.desired_runtime_state != selected.desired_runtime_state
            ):
                raise StateError("session identity or target changed before final observation")
            power = powers.get(session_vm_name(invocation, current), VMStatus.UNKNOWN)
            if power in OFF:
                observed = SessionStatus.STOPPED
            elif power is VMStatus.RUNNING:
                prepared = invocation.launches.get(name)
                stopped = invocation.stops.get(name)
                admin = (
                    prepared.admin_target
                    if prepared is not None
                    else stopped.admin_target
                    if stopped is not None
                    else None
                )
                if admin is None:
                    raise StateError("guest stop is awaiting parent shutdown")
                observed = sessions.observe_session_runtime(
                    current,
                    admin_target=admin,
                    session_target=prepared.session_target if prepared is not None and prepared.legacy else None,
                )
            else:
                observed = SessionStatus.UNKNOWN
            invocation.record("session", name, observation=observed)
            if was_failed:
                continue
            expected = (
                SessionStatus.RUNNING
                if current.desired_runtime_state is DesiredRuntimeState.RUNNING
                else SessionStatus.STOPPED
            )
            if observed is not expected:
                invocation.fail(
                    "session",
                    name,
                    "final session observation does not satisfy desired state",
                    blocked=observed is SessionStatus.UNKNOWN,
                )
            else:
                invocation.completed.add(("session", name))
                if (
                    name in invocation.accepted_changes
                    and invocation.results[("session", name)].outcome is Outcome.UNCHANGED
                ):
                    invocation.record("session", name, outcome=Outcome.CHANGED)
        except UserAbort:
            raise
        except Exception as error:
            if was_failed:
                invocation.record("session", name, observation=SessionStatus.UNKNOWN)
            else:
                invocation.fail("session", name, error, blocked=True)
    for name, console in invocation.consoles.items():
        if invocation.results[("console", name)].outcome is Outcome.BLOCKED:
            continue
        was_failed = invocation.results[("console", name)].outcome is Outcome.FAILED
        try:
            power = powers.get(console.vm_name, VMStatus.UNKNOWN)
            if power in OFF:
                observed_console = ConsoleStatus.STOPPED
            elif power is VMStatus.RUNNING:
                console_prepared = invocation.console_starts.get(name) or invocation.console_stops.get(name)
                if console_prepared is None:
                    raise StateError("console stop is awaiting parent shutdown")
                observed_console = consoles.observe_prepared_console(console_prepared)
            else:
                observed_console = ConsoleStatus.UNKNOWN
            invocation.record("console", name, observation=observed_console)
            if was_failed:
                continue
            expected_console = (
                ConsoleStatus.RUNNING
                if console.desired_runtime_state is DesiredRuntimeState.RUNNING
                else ConsoleStatus.STOPPED
            )
            if observed_console is not expected_console:
                invocation.fail(
                    "console",
                    name,
                    "final console observation does not satisfy desired state",
                    blocked=observed_console is ConsoleStatus.UNKNOWN,
                )
            else:
                invocation.completed.add(("console", name))
        except UserAbort:
            raise
        except Exception as error:
            if was_failed:
                invocation.record("console", name, observation=ConsoleStatus.UNKNOWN)
            else:
                invocation.fail("console", name, error, blocked=True)
