"""Retained exact-run recovery facts, never aggregate disposition or renewal."""

from __future__ import annotations

import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.vms._recovery_vm_span import RecoveryVMPreparedContext, RecoveryVMSpan

from ._fixed_helper_operation import AttemptBoundHelperCarrier
from ._helper_launcher import _validate_plan
from ._managed_job_protocol import encode_managed_job_fact
from ._managed_lease_exchange import observe_operation_clock
from ._managed_lease_protocol import ClockObservation
from ._managed_lease_wire import LeaseError, sampled_lease
from ._managed_observation_exchange import observe_managed_run
from ._managed_run_obligation import decode_managed_run_obligation
from ._managed_runs import ManagedRunLifetime, ManagedRunOwnerKind, ManagedRunReceipt, ManagedTargetKind
from ._managed_stop_exchange import stop_managed_run
from ._runtime_prerequisite import RuntimeTargetOS
from ._vm_guest_identity_protocol import vm_guest_boot_id
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.operations import OperationOwner, RecoveryAttempt, RecoveryDispatch

    from ._managed_lease_exchange import ManagedLeaseCandidate
    from ._managed_observation_exchange import ManagedObservationCandidate
    from ._managed_runs import ManagedRunRepository
    from ._managed_stop_exchange import ManagedStopCandidate


@dataclass(frozen=True, slots=True)
class RecoveryDrain:
    """Local delivery and helper termination, not predecessor or body drain."""

    local_settled: bool
    helper_termination_known: bool
    dispatch_retained: bool


class ManagedOperationRecovery:
    """One exact admitted row, fixed ceiling, and serial protected exchanges.

    Construction performs SQLite reads/rebinding only. Span membership and
    native availability are authoritative only within each fresh protected action.
    Caller retains this object before dispatch; no action carrier is retained.
    """

    def __init__(
        self,
        repository: ManagedRunRepository,
        owner: OperationOwner,
        obligation: LifecycleObligation,
        prepared: RecoveryVMPreparedContext,
    ) -> None:
        if (
            type(prepared) is not RecoveryVMPreparedContext
            or type(prepared._span) is not RecoveryVMSpan
            or obligation.ownership != owner.ownership
            or obligation.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or obligation.obligation_kind not in {"managed-start", "managed-operation-keeper"}
            or obligation.payload_version != 1
            or obligation.payload_revision != 0
            or repository._database is not owner._repository._controller_identity
        ):
            raise ValidationError("Operation recovery requires an exact current admitted row and prepared span")
        identity = decode_managed_run_obligation(obligation.payload)
        record = repository.inspect(identity)
        scope = owner.ownership.scope
        if (
            record is None
            or record.spec.lifetime is not ManagedRunLifetime.OPERATION
            or record.spec.owner.kind is not ManagedRunOwnerKind.OPERATION
            or record.spec.owner.owner_id != owner.ownership.operation_id
            or record.spec.target != prepared.target
            or prepared.target.kind is not ManagedTargetKind.VM
            or scope.resource_kind is not OperationResourceKind.VM
            or scope.resource_name != prepared.target.name
            or prepared.target.boot_id != vm_guest_boot_id(prepared.guest)
            or _validate_plan(prepared.root_plan).euid != 0
            or prepared.runtime_selection.target_os is not RuntimeTargetOS.LINUX
        ):
            raise ValidationError("Operation recovery persisted run and guest authority differ")
        self.receipt = ManagedRunReceipt(identity, identity.unit_name, record.spec)
        self._launch = encode_managed_job_fact(self.receipt)
        self._repository = repository
        self._owner = owner
        self._thread = threading.current_thread()
        self._span = prepared._span
        self._guest = prepared.guest
        self._root = prepared.root_plan
        self._runtime = prepared.runtime_selection
        self._bound = owner.rebind_possible_effect_lifecycle_obligation(
            obligation.obligation_id,
            obligation.obligation_kind,
            payload_version=1,
            payload=obligation.payload,
            payload_revision=0,
        )
        self.dispatch: RecoveryDispatch | None = None
        self.attempt: RecoveryAttempt | None = None
        self._opening = False
        self._beginning = False
        self._helper_terminated = True
        self.failed = False
        self.last_clock: ManagedLeaseCandidate | None = None
        self._accepted_sample_ns: int | None = None
        self.ceiling_ns: int | None = None
        self.authority_elapsed = False
        self.last_stop: ManagedStopCandidate | None = None
        self.last_observation: ManagedObservationCandidate | None = None

    def _require_thread(self) -> None:
        if threading.current_thread() is not self._thread:
            raise StateError("Operation recovery requires its originating caller thread")

    def _begin(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> RecoveryAttempt:
        self._require_thread()
        if self.dispatch is not None or self._opening or self._beginning:
            raise StateError("Operation recovery retains an earlier exchange")
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("Operation recovery requires a live finite budget")
        if (
            type(context) is not RecoveryVMPreparedContext
            or context._span is not self._span
            or context.target != self.receipt.spec.target
            or context.guest != self._guest
            or context.root_plan != self._root
            or context.runtime_selection != self._runtime
        ):
            raise ValidationError("Operation recovery requires its exact fresh span facts")
        context._span._require_prepared_action(context, self._owner, deadline)
        record = self._repository.inspect(self.receipt.identity)
        if record is None or record.spec != self.receipt.spec:
            raise StateError("Operation recovery persisted run changed")
        self._opening = True
        self.dispatch = self._bound.open_dispatch()
        self._opening = False
        self._beginning = True
        self.attempt = self.dispatch.begin_attempt()
        self._beginning = False
        self._helper_terminated = False
        return self.attempt

    def _complete(self, dispatch: Dispatch, completion: ExitStatus | None, deadline: Deadline) -> None:
        self._helper_terminated = dispatch is Dispatch.NOT_SENT or (
            dispatch is Dispatch.SENT and completion == ExitStatus(0)
        )
        if self._helper_terminated:
            self.drain(deadline)

    def observe_clock(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> ManagedLeaseCandidate:
        """Fix one fresh same-boot ceiling; later polls never raise it."""
        self.authority_elapsed = False
        try:
            attempt = self._begin(context, deadline)
            candidate = observe_operation_clock(
                context.carrier,
                plan=self._root,
                guest=self._guest,
                runtime_selection=self._runtime,
                deadline=deadline,
                custody=attempt.local_delivery,
            )
            self.last_clock = candidate
            if isinstance(candidate.result, ClockObservation) and (
                self._accepted_sample_ns is None or candidate.result.sampled_ns >= self._accepted_sample_ns
            ):
                self._accepted_sample_ns = candidate.result.sampled_ns
                if self.ceiling_ns is None:
                    with suppress(LeaseError):
                        self.ceiling_ns = sampled_lease(self._launch, candidate.result.sampled_ns).expires_ns
                if self.ceiling_ns is not None:
                    self.authority_elapsed = candidate.result.sampled_ns >= self.ceiling_ns
            self._complete(candidate.dispatch, candidate.carrier_completion, deadline)
            return candidate
        except BaseException:
            self.failed = True
            raise

    def request_stop(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> ManagedStopCandidate:
        """Stop this exact run without waiting for any clock or expiry ceiling."""
        try:
            attempt = self._begin(context, deadline)
            candidate = stop_managed_run(
                AttemptBoundHelperCarrier(context.carrier, attempt),
                expected_launch=self._launch,
                plan=self._root,
                deadline=deadline,
                runtime_selection=self._runtime,
                guest=self._guest,
            )
            self.last_stop = candidate
            self._complete(candidate.dispatch, candidate.carrier_completion, deadline)
            return candidate
        except BaseException:
            self.failed = True
            raise

    def observe_cleanup(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> ManagedObservationCandidate:
        """Retain raw workload/controller facts without one-start reconciliation."""
        try:
            attempt = self._begin(context, deadline)
            candidate = observe_managed_run(
                AttemptBoundHelperCarrier(context.carrier, attempt),
                expected_launch=self._launch,
                plan=self._root,
                deadline=deadline,
                runtime_selection=self._runtime,
                guest=self._guest,
            )
            self.last_observation = candidate
            self._complete(candidate.dispatch, candidate.carrier_completion, deadline)
            return candidate
        except BaseException:
            self.failed = True
            raise

    def drain(self, deadline: Deadline) -> RecoveryDrain:
        """Drain exact local pipes; unknown remote termination remains blocking.

        Interrupted open/begin preceded exchange entry. Interrupted settlement
        can be reconciled from this exact dispatch's local outstanding cell.
        Deadlines do not preempt SQLite guards or native system calls.
        """
        self._require_thread()
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Operation recovery drain requires a finite budget")
        if self._opening:
            self._bound._close_retained_dispatch()
            self.dispatch = None
            self._opening = False
        if self._beginning:
            assert self.dispatch is not None
            self.dispatch._abort_unreturned_attempt()
            self.attempt = None
            self._beginning = False
            self.dispatch = None
        attempt = self.attempt
        if attempt is not None:
            local = attempt.local_delivery.close(deadline)
            if not local or not self._helper_terminated:
                return RecoveryDrain(local, self._helper_terminated, True)
            with self._owner._guard:
                outstanding = self._owner._outstanding_attempt
            if outstanding is attempt:
                attempt.settle()
            elif outstanding is not None:
                raise StateError("Operation recovery has conflicting local settlement custody")
            self.attempt = None
        if self.dispatch is not None:
            self.dispatch.close()
            self.dispatch = None
        return RecoveryDrain(True, self._helper_terminated, False)
