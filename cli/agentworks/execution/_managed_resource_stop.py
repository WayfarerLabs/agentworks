"""Exact RESOURCE stop through an observing operation's ordinary helper custody."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._managed_bound_run import preflight_bound_run
from ._managed_runs import ManagedLaunchState, ManagedRunIdentity, ManagedRunOwner
from ._managed_stop_exchange import ManagedStopState
from .carrier import Deadline, Dispatch
from .jobs import JobStop
from .models import JobRef
from .result import ExecutionFailure

if TYPE_CHECKING:
    from ._execution_operation import ExecutionOperation


def stop_resource_job(operation: ExecutionOperation, reference: JobRef, deadline: Deadline) -> JobStop:
    """Admit one core-bound independent stop without adopting job or keeper custody."""
    repository, bootstrap, binding = operation._managed_repository, operation._bootstrap, operation._native_binding
    if type(reference) is not JobRef or repository is None or bootstrap is None or binding is None:
        raise ValidationError("Managed resource stop requires its bound operation")
    operation.require_job_binding(binding.carrier, binding.runtime_selection)
    identity = ManagedRunIdentity(reference.run_id)
    namespace = operation.require_managed_access(
        identity,
        repository=repository,
        owner=operation._owner,
        target=operation._target,
        guest=bootstrap.guest,
        root_plan=bootstrap.root_entry,
        runtime_selection=binding.runtime_selection,
    )
    if not isinstance(namespace, ManagedRunOwner):
        raise ValidationError("Managed resource stop cannot adopt an operation run")
    record, expected_launch = preflight_bound_run(
        repository,
        identity,
        target=operation._target,
        guest=bootstrap.guest,
        root_plan=bootstrap.root_entry,
        runtime_selection=binding.runtime_selection,
        deadline=deadline,
        owner=operation._owner,
        expected_resource_owner=namespace,
    )
    if record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED:
        raise ValidationError("Managed resource stop requires a confirmed launch receipt")
    outcome = operation._stop_managed(expected_launch, deadline)
    accepted = outcome.state in {ManagedStopState.ACCEPTED, ManagedStopState.TERMINATED}
    if not accepted:
        return JobStop(
            reference,
            False if outcome.candidate is not None and outcome.candidate.dispatch is Dispatch.NOT_SENT else None,
            False,
            ExecutionFailure.DEADLINE if deadline.expired else ExecutionFailure.OBSERVATION,
            deadline.expired,
        )
    if deadline.expired:
        return JobStop(reference, True, False, ExecutionFailure.DEADLINE, True)
    try:
        terminal = operation.observe_job(reference, binding.carrier, deadline)
    except (StateError, ValidationError):
        return JobStop(reference, True, False, ExecutionFailure.OBSERVATION, deadline.expired)
    proved = terminal.terminal_proved
    return JobStop(
        reference,
        True,
        proved,
        ExecutionFailure.DEADLINE if deadline.expired else None if proved else ExecutionFailure.OBSERVATION,
        deadline.expired,
    )
