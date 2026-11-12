"""Explicit stop repairs and tears down only after the shared secret barrier."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import agentworks.sessions.manager as sessions
from agentworks import output
from agentworks.db import PID_STOPPED, DatabaseDriverError, SessionMode, SessionRuntimeSnapshot, SessionStatus
from agentworks.errors import StateConflictError, StateError, UserAbort
from agentworks.sessions.manager._teardown import (
    DEDICATED_TEARDOWN_ATTEMPTS,
    DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
    DedicatedTeardownPlan,
    execute_concurrent_dedicated_teardowns,
    has_complete_runtime_fields,
    prepare_dedicated_teardown,
)

from ._models import Outcome
from ._prepare import OFF, _guest_stop_failure

if TYPE_CHECKING:
    from agentworks.db import SessionRow

    from ._models import RuntimeInvocation


def _selected_session(invocation: RuntimeInvocation, expected: SessionRow) -> SessionRow:
    from agentworks.sessions.manager._lifecycle import _require_selected_session

    current = _require_selected_session(invocation.db, expected.name, expected.session_uuid)
    workspace = sessions._require_workspace(invocation.db, current.workspace_name)
    vm = sessions._require_vm_for_workspace(invocation.db, workspace)
    parent = dict(invocation.selection.session_routes)[expected.name]
    if (
        workspace.vm_name != parent
        or current.workspace_name != expected.workspace_name
        or vm.vm_uuid != invocation.vms[parent].vm_uuid
        or current.desired_runtime_state != expected.desired_runtime_state
        or SessionRuntimeSnapshot.from_session(current) != SessionRuntimeSnapshot.from_session(expected)
    ):
        raise StateConflictError(
            "session changed before bulk teardown", entity_kind="session", entity_name=expected.name
        )
    return current


def _completed(invocation: RuntimeInvocation, name: str, *, changed: bool) -> None:
    invocation.record(
        "session",
        name,
        observation=SessionStatus.STOPPED,
        observation_from_parent=invocation.stops[name].parent_off,
        outcome=Outcome.CHANGED if changed or name in invocation.accepted_changes else Outcome.UNCHANGED,
    )
    invocation.completed.add(("session", name))


def execute_public_stops(invocation: RuntimeInvocation) -> None:
    """Repair on the caller, then use named or bulk native teardown authority."""
    from agentworks.vms.manager import require_vm_ssh_boundary

    live = []
    vm_targets = {}
    unobserved: set[str] = set()
    for name, selected in invocation.sessions.items():
        parent = dict(invocation.selection.session_routes)[name]
        if invocation.unavailable("session", name):
            if parent in invocation.failed_families and selected.pid != PID_STOPPED:
                unobserved.add(name)
            continue
        context = invocation.contexts[parent]
        try:
            current = _selected_session(invocation, selected)
            if context.observed_status in OFF:
                invocation.stops[name] = sessions.prepare_session_stop(
                    invocation.db, invocation.config, current, parent_off=True, force=invocation.force
                )
                continue
            require_vm_ssh_boundary(invocation.db, invocation.config, context.vm)
            if parent not in vm_targets:
                vm_targets[parent] = sessions.transport(context.vm, invocation.config)
            workspace = sessions._require_workspace(invocation.db, current.workspace_name)
            invocation.stops[name] = sessions.PreparedSessionStop(
                db=invocation.db,
                config=invocation.config,
                session=current,
                workspace=workspace,
                vm=context.vm,
                admin_target=vm_targets[parent],
                session_target=None,
                observed_status=SessionStatus.UNKNOWN,
                parent_off=False,
                force=invocation.force,
            )
            live.append(current)
        except (UserAbort, StateConflictError, DatabaseDriverError):
            raise
        except Exception as error:
            _guest_stop_failure(invocation, "session", name, parent, error)
            if invocation.operation == "stop-all" and invocation.unavailable("session", name):
                output.warn(f"Session '{name}' failed stop preparation: {error}")
                if selected.pid != PID_STOPPED:
                    unobserved.add(name)

    # These safe writes remain on the caller thread, after shared secret success.
    # Repair conflicts/writer failures must escape before any teardown is submitted.
    if invocation.operation == "stop":
        repaired = []
        for session in live:
            try:
                target = invocation.stops[session.name].admin_target
                assert target is not None
                repaired.append(
                    sessions._ensure_pid(session, target=target, db=invocation.db)
                    if sessions._needs_repair(session)
                    else session
                )
            except (UserAbort, StateConflictError, DatabaseDriverError):
                raise
            except Exception as error:
                parent = invocation.stops[session.name].vm.name
                _guest_stop_failure(invocation, "session", session.name, parent, error)
        live = repaired
    elif live:
        live = sessions.ensure_pids_batch(live, db=invocation.db, config=invocation.config)
    for session in live:
        prepared_before_repair = invocation.stops[session.name]
        if (
            session.session_uuid != prepared_before_repair.session.session_uuid
            or session.run_id != prepared_before_repair.session.run_id
            or session.workspace_name != prepared_before_repair.session.workspace_name
            or session.desired_runtime_state != prepared_before_repair.session.desired_runtime_state
        ):
            raise StateConflictError(
                "session changed during bulk repair", entity_kind="session", entity_name=session.name
            )
        _selected_session(invocation, session)
        invocation.stops[session.name] = replace(prepared_before_repair, session=session)
    statuses = (
        sessions.observe_session_statuses(live, db=invocation.db, config=invocation.config)
        if live and invocation.operation == "stop-all"
        else {}
    )
    for session in live:
        invocation.stops[session.name] = replace(
            invocation.stops[session.name], observed_status=statuses.get(session.name, SessionStatus.UNKNOWN)
        )

    if invocation.operation == "stop":
        for name, prepared in tuple(invocation.stops.items()):
            if invocation.unavailable("session", name):
                continue
            try:
                current = _selected_session(invocation, prepared.session)
                if not prepared.parent_off:
                    prepared = sessions.prepare_session_stop(
                        invocation.db, invocation.config, current, force=invocation.force
                    )
                    invocation.stops[name] = prepared
            except (UserAbort, StateConflictError, DatabaseDriverError):
                raise
            except Exception as error:
                _guest_stop_failure(invocation, "session", name, prepared.vm.name, error)
                continue
            try:
                changed = sessions.execute_session_stop(prepared)
                _completed(invocation, name, changed=changed)
            except UserAbort:
                raise
            except Exception as error:
                invocation.fail("session", name, error)
        return

    legacy = {
        session.name for session in live if session.socket_path is None and session.pid not in {None, PID_STOPPED}
    }
    unknown = unobserved | {
        session.name
        for session in live
        if session.pid != PID_STOPPED
        and session.name not in legacy
        and statuses.get(session.name, SessionStatus.UNKNOWN) is SessionStatus.UNKNOWN
    }
    if unknown:
        for name in invocation.stops:
            invocation.fail(
                "session",
                name,
                "session runtime state is unknown"
                if name in unknown
                else "bulk stop blocked by unknown session runtime",
                blocked=True,
            )
        return

    plans = []
    serial = []
    active = {SessionStatus.RUNNING, SessionStatus.RESIDUAL}
    if invocation.force:
        active.add(SessionStatus.BROKEN)
    for name, prepared in invocation.stops.items():
        if invocation.unavailable("session", name):
            continue
        try:
            current = _selected_session(invocation, prepared.session)
            if prepared.parent_off:
                serial.append(prepared)
            elif current.name not in legacy and prepared.observed_status not in active:
                if prepared.observed_status is SessionStatus.BROKEN:
                    raise StateError("session runtime requires explicit force recovery")
                _completed(invocation, name, changed=False)
            elif current.pid == PID_STOPPED:
                raise StateError("session has live runtime evidence but is marked stopped")
            elif has_complete_runtime_fields(current):
                plans.append(
                    prepare_dedicated_teardown(
                        invocation.db,
                        current,
                        vm_name=prepared.vm.name,
                        target=sessions.transport(
                            prepared.vm,
                            invocation.config,
                            default_timeout=DEDICATED_TEARDOWN_TIMEOUT_SECONDS,
                            retries=DEDICATED_TEARDOWN_ATTEMPTS,
                        ),
                        target_owns_session=current.mode == SessionMode.ADMIN.value,
                        force=invocation.force,
                    )
                )
            else:
                serial.append(prepared)
        except (UserAbort, StateConflictError, DatabaseDriverError):
            raise
        except Exception as error:
            _guest_stop_failure(invocation, "session", name, prepared.vm.name, error)
            if invocation.operation == "stop-all" and invocation.unavailable("session", name):
                output.warn(f"Session '{name}' failed to stop: {error}")

    output.info(f"Stopping {len(plans) + len(serial)} session(s)...")

    def outcome(plan: DedicatedTeardownPlan, failure_reason: str | None) -> None:
        if failure_reason is None:
            _completed(invocation, plan.session_name, changed=True)
        else:
            invocation.fail("session", plan.session_name, failure_reason)

    execute_concurrent_dedicated_teardowns(plans, db=invocation.db, on_outcome=outcome)
    for prepared in serial:
        name = prepared.session.name
        current = _selected_session(invocation, prepared.session)
        try:
            if prepared.parent_off:
                changed = sessions.execute_session_stop(prepared)
            else:
                assert prepared.admin_target is not None
                sessions._teardown_session(
                    current,
                    target=prepared.admin_target,
                    target_owns_session=current.mode == SessionMode.ADMIN.value,
                    db=invocation.db,
                    force=invocation.force,
                    legacy_running_authorized=True,
                )
                changed = True
            _completed(invocation, name, changed=changed)
        except UserAbort:
            raise
        except Exception as error:
            invocation.fail("session", name, error)
            output.warn(f"Session '{name}' failed to stop: {error}")
