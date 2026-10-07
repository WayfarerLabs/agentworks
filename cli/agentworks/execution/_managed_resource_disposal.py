"""Exact RESOURCE disposal and receipt-bound retry in existing helper custody."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation, release_borrow_after_custody

from ._managed_bound_run import preflight_bound_run
from ._managed_disposal_access import (
    MANAGED_DISPOSAL_OBLIGATION_KIND,
    MANAGED_DISPOSAL_PAYLOAD_VERSION,
    encode_managed_disposal_obligation,
)
from ._managed_disposal_exchange import DisposalCandidate, DisposalState, dispose_managed_run
from ._managed_runs import ManagedLaunchState, ManagedRunIdentity, ManagedRunOwner, ManagedRunReceipt
from .carrier import Dispatch, ExitStatus
from .jobs import JobDisposal
from .models import JobRef
from .result import ExecutionFailure

if TYPE_CHECKING:
    from agentworks.db import LifecycleObligation as PersistedLifecycleObligation

    from ._execution_operation import ExecutionOperation, OwnedInlineOutcome, _ActiveHelperCall
    from ._managed_observation_exchange import ManagedObservationCandidate
    from .carrier import Carrier, Deadline


@dataclass(frozen=True, slots=True, repr=False)
class ManagedDisposalBinding:
    """One exact action's non-secret receipt and validated positive terminal facts."""

    obligation_id: str
    receipt: ManagedRunReceipt
    terminal_observation: ManagedObservationCandidate
    prior_remote_action_uncertain: bool = False

    @property
    def registration(self) -> tuple[str, str, int, bytes]:
        return (
            self.obligation_id,
            MANAGED_DISPOSAL_OBLIGATION_KIND,
            MANAGED_DISPOSAL_PAYLOAD_VERSION,
            encode_managed_disposal_obligation(self.receipt.identity.run_id),
        )


def inspect_disposal_row(
    operation: ExecutionOperation, active: _ActiveHelperCall
) -> PersistedLifecycleObligation | None:
    """Require current ownership and exact immutable dedicated action metadata."""
    binding = active.disposal
    assert binding is not None
    identity, kind, version, payload = binding.registration
    row = operation._owner.inspect_lifecycle_obligation(identity)
    if row is None:
        if active.installed or active.armed or active.borrow.has_installed_dispatch_obligation:
            raise StateError("Managed disposal obligation disappeared")
        return None
    if (
        row.ownership != operation._owner.ownership
        or (row.obligation_kind, row.payload_version, row.payload) != (kind, version, payload)
        or row.payload_revision != 0
    ):
        raise StateError("Managed disposal obligation binding changed")
    return row


def disposal_action_proved(active: _ActiveHelperCall) -> bool:
    """Helper closure alone cannot establish settlement of this action."""
    assert active.disposal is not None
    candidate = active.candidate
    return isinstance(candidate, DisposalCandidate) and (
        (candidate.dispatch is Dispatch.NOT_SENT and not active.disposal.prior_remote_action_uncertain)
        or (
            candidate.dispatch is Dispatch.SENT
            and candidate.carrier_completion == ExitStatus(0)
            and candidate.observation is not None
            and candidate.observation.state in {DisposalState.NOT_READY, DisposalState.DISPOSED}
        )
    )


def capture_disposal_call(
    operation: ExecutionOperation, active: _ActiveHelperCall, outcome: OwnedInlineOutcome
) -> None:
    """Settle only a proved action, otherwise retain the actual entry for retry."""
    row = inspect_disposal_row(operation, active)
    proved = disposal_action_proved(active) and not active.operation.requires_owner_retention
    active.outcome = replace(
        outcome,
        pending_remote_effects=outcome.pending_remote_effects or (active.armed and not proved),
        requires_owner_retention=outcome.requires_owner_retention or not proved,
    )
    active.bookkeeping_retained = True
    if (
        active.operation.requires_owner_retention
        and active.candidate is not None
        and operation._known_termination(active.candidate)
    ):
        # Preserve the actual borrow until an interrupted settlement completes;
        # relinquishing it would make its matching attempt impossible to settle.
        return
    if not operation._borrow_closed(active):
        if active.candidate is None:
            return
        release_borrow_after_custody(active.borrow, retain_effect=not proved)
    if proved:
        if row is not None and row.state is not LifecycleObligationState.RESOLVED:
            LifecycleObligation(operation._owner, row).resolve()
        operation._active_inline_calls.pop(active.tracking_key, None)


def settle_unused_disposal_call(operation: ExecutionOperation, active: _ActiveHelperCall) -> None:
    """Close candidate-less custody only in the real never-delivered control path."""
    wrapper = active.operation
    if not active.bookkeeping_retained or active.candidate is not None or wrapper.requires_owner_retention:
        raise StateError("Managed disposal retains unknown delivery")
    row = inspect_disposal_row(operation, active)
    if not operation._borrow_closed(active):
        assert active.disposal is not None
        if not active.installed:
            if (
                active.disposal.prior_remote_action_uncertain
                and row is not None
                and row.state is LifecycleObligationState.POSSIBLE_EFFECT
            ):
                # Before-install interruption has no local supplied binding.
                # Reinstall this exact existing receipt before generic recovery
                # can attach the row; no helper or attempt is created here.
                identity, kind, version, payload = active.disposal.registration
                active.borrow.install_dispatch_obligation(identity, kind, payload_version=version, payload=payload)
                active.installed = True
            else:
                operation._recover_registration(active)
        if (
            active.disposal.prior_remote_action_uncertain
            and row is not None
            and row.state is not LifecycleObligationState.RESOLVED
        ):
            if not active.borrow.dispatch_obligation_may_be_armed:
                active.borrow.arm_dispatch_obligation()
            active.armed = True
            release_borrow_after_custody(active.borrow, retain_effect=True)
        else:
            active.borrow.close()
    assert active.disposal is not None
    if active.disposal.prior_remote_action_uncertain:
        return
    if row is not None and row.state is not LifecycleObligationState.RESOLVED:
        LifecycleObligation(operation._owner, row).resolve()
    operation._active_inline_calls.pop(active.tracking_key, None)


def dispose_resource_job(
    operation: ExecutionOperation, reference: JobRef, carrier: Carrier, deadline: Deadline
) -> JobDisposal:
    """Use fresh positive terminal proof or the exact pending disposal receipt."""
    repository, bootstrap, binding = operation._managed_repository, operation._bootstrap, operation._native_binding
    if type(reference) is not JobRef or repository is None or bootstrap is None or binding is None:
        raise ValidationError("Managed resource disposal requires its bound operation")
    operation.require_job_binding(carrier, binding.runtime_selection)
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
        raise ValidationError("Managed resource disposal cannot adopt an operation run")
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
        raise ValidationError("Managed resource disposal requires a confirmed launch receipt")
    active: _ActiveHelperCall | None
    with operation._admission_guard:
        retained = tuple(operation._active_inline_calls.values())
        if retained:
            if (
                len(retained) != 1
                or retained[0].disposal is None
                or retained[0].disposal.receipt != ManagedRunReceipt(identity, identity.unit_name, record.spec)
            ):
                raise StateError("Managed disposal cannot overlap another retained helper")
            active = retained[0]
            operation._settle_retained_helper(active)
            if active.candidate is None:
                settle_unused_disposal_call(operation, active)
                if active.tracking_key not in operation._active_inline_calls:
                    active = None
            else:
                operation._capture(
                    active, operation._outcome(active, active.operation, deadline, include_candidate=False)
                )
                if active.tracking_key not in operation._active_inline_calls:
                    return _result(reference, active, deadline)
        else:
            active = None
    if active is None:
        observed = operation.observe_job(reference, carrier, deadline)
        if not observed.terminal_proved:
            return JobDisposal(
                reference,
                None if observed.requires_owner_retention else False,
                ExecutionFailure.DEADLINE
                if deadline.expired
                else ExecutionFailure.OBSERVATION
                if observed.requires_owner_retention
                else None,
                deadline.expired,
            )
        assert observed.candidate is not None
        action = ManagedDisposalBinding(
            uuid4().hex, ManagedRunReceipt(identity, identity.unit_name, record.spec), observed.candidate
        )
        if deadline.expired:
            return JobDisposal(reference, False, ExecutionFailure.DEADLINE, True)
        with operation._admission_guard:
            if operation._active_inline_calls or operation._unfinished_inline_executions:
                raise StateError("Managed disposal cannot overlap unfinished ordinary delivery")
            active = operation._borrow_helper_call(carrier, None, disposal=action)
    else:
        with operation._admission_guard:
            row = inspect_disposal_row(operation, active)
            assert active.disposal is not None
            action = active.disposal
            action = replace(action, prior_remote_action_uncertain=True)
            if row is not None and row.state is LifecycleObligationState.RESOLVED:
                action = replace(action, obligation_id=uuid4().hex)
            if not operation._borrow_closed(active) or active.operation.requires_owner_retention:
                raise StateError("Managed disposal retry retains actual helper custody")
            active = operation._borrow_helper_call(carrier, None, disposal=action, replacing=active)
    helper = active.operation
    try:
        operation._admit(active)
        if operation._wsl2_route is not None:
            operation._wsl2_route.require_selected_route(deadline)
        candidate = dispose_managed_run(
            helper,
            expected_launch=expected_launch,
            plan=bootstrap.root_entry,
            deadline=deadline,
            runtime_selection=binding.runtime_selection,
            guest=bootstrap.guest,
        )
        active.candidate = candidate
        helper.settle(candidate.dispatch, candidate.carrier_completion)
        with operation._admission_guard:
            operation._capture(active, operation._outcome(active, helper, deadline, include_candidate=False))
        return _result(reference, active, deadline)
    except BaseException as control:
        operation._raise_control(control, active, helper, deadline)
        raise AssertionError("Unreachable managed disposal control return") from None


def _result(reference: JobRef, active: _ActiveHelperCall, deadline: Deadline) -> JobDisposal:
    settled = active.outcome is not None and not active.outcome.requires_owner_retention
    candidate = active.candidate
    disposed = (
        settled
        and isinstance(candidate, DisposalCandidate)
        and candidate.observation is not None
        and candidate.observation.state is DisposalState.DISPOSED
    )
    return JobDisposal(
        reference,
        disposed if settled else None,
        ExecutionFailure.DEADLINE if deadline.expired else None if settled else ExecutionFailure.OBSERVATION,
        deadline.expired,
    )
