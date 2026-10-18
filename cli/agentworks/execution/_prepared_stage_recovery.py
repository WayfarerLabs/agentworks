"""Cleanup-only recovery of a sole prepared-stage predecessor obligation."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation
from agentworks.vms._recovery_vm_span import RecoveryVMPreparedContext, RecoveryVMSpan

from ._file_stage_exchange import (
    FileStageCandidateResult,
    FileStageObservationState,
    stage_cleanup,
    stage_reconcile,
)
from ._fixed_helper_operation import AttemptBoundHelperCarrier
from ._managed_operation_recovery import RecoveryDrain
from ._prepared_helper_obligation import (
    PREPARED_HELPER_OBLIGATION_KIND,
    PreparedHelperObligation,
    decode_prepared_helper_obligation,
)
from ._runtime_prerequisite import RuntimePrerequisiteState, _NumericGuestBootstrap
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation as PersistedLifecycleObligation
    from agentworks.operations import OperationOwner, RecoveredLifecycleObligation, RecoveryAttempt, RecoveryDispatch

    from ._scratch_receipt import ScratchCleanupDebt


class PreparedHelperStageRecovery:
    """Retain exact cleanup custody, never stage input or activation authority.

    Passive opening requires the stage to be the only predecessor debt before
    a new span admits support rows. Stage carrier and activation rows must have
    closed already; a token or historical receipt cannot discharge those rows.
    This ledger gate does not establish generic native predecessor quiescence.
    Each recovery helper first installs a durable in-flight marker. Only actual
    helper and local closure restore an idle row or permit cleanup resolution;
    later controllers refuse an unresolved marker even after support disposal.
    """

    def __init__(
        self,
        owner: OperationOwner,
        obligation: PersistedLifecycleObligation,
        record: PreparedHelperObligation,
        bound: RecoveredLifecycleObligation,
    ) -> None:
        self._owner, self._obligation, self._record, self._bound = owner, obligation, record, bound
        self._original_payload = obligation.payload
        self._marker = b'{"prepared":' + obligation.payload + b',"version":2}'
        if len(self._marker) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValidationError("Prepared stage recovery marker exceeds the durable payload bound")
        self._publication: tuple[PersistedLifecycleObligation, int, bytes] | None = None
        self._thread = threading.current_thread()
        self._span: RecoveryVMSpan | None = None
        self.dispatch: RecoveryDispatch | None = None
        self.attempt: RecoveryAttempt | None = None
        self._opening = False
        self._beginning = False
        self._helper_terminated = True
        self._cleanup_confirmed = False
        self.cleanup_debt: ScratchCleanupDebt | None = None
        self.last_candidate: FileStageCandidateResult | None = None
        self.cleaned = False
        self.resolution_pending = False

    @classmethod
    def open(cls, owner: OperationOwner, obligation: PersistedLifecycleObligation) -> PreparedHelperStageRecovery:
        """Bind only one exact sole stage row, using SQLite and no span action."""
        if obligation.payload_version != 1:
            raise StateError("Prepared stage recovery retains unresolved earlier recovery work")
        record = decode_prepared_helper_obligation(obligation.payload)
        scope = owner.ownership.scope
        if (
            obligation.ownership != owner.ownership
            or obligation.obligation_kind != PREPARED_HELPER_OBLIGATION_KIND
            or obligation.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or obligation.payload_version != 1
            or obligation.payload_revision < 0
            or scope.resource_kind is not OperationResourceKind.VM
            or scope.resource_name != record.target.name
        ):
            raise ValidationError("Prepared stage recovery requires an exact current stage row")
        with owner._guard:
            owner._reconcile_transition_locked()
            owner._require_no_active_work_locked()
            if not owner._recovery_owner or not owner._obligations_sealed:
                raise StateError("Prepared stage recovery requires sealed recovery ownership")
        if owner.list_pending_lifecycle_obligations() != (obligation,):
            raise StateError("Prepared stage recovery requires closure of every other predecessor obligation")
        bound = owner.rebind_possible_effect_lifecycle_obligation(
            obligation.obligation_id,
            PREPARED_HELPER_OBLIGATION_KIND,
            payload_version=1,
            payload=obligation.payload,
            payload_revision=obligation.payload_revision,
        )
        return cls(owner, obligation, record, bound)

    def _require_thread(self) -> None:
        if threading.current_thread() is not self._thread:
            raise StateError("Prepared stage recovery requires its originating caller thread")

    def _require_context(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> None:
        self._require_thread()
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("Prepared stage recovery requires a finite live budget")
        if type(context) is not RecoveryVMPreparedContext or type(context._span) is not RecoveryVMSpan:
            raise StateError("Prepared stage recovery requires a protected span action")
        context._span._require_prepared_action(context, self._owner, deadline)
        record = self._record
        if (
            (self._span is not None and context._span is not self._span)
            or context.target != record.target
            or context.guest != record.bootstrap.guest
            or context.runtime_selection != record.runtime
            or context.root_plan.expected != record.bootstrap.root_entry.expected
        ):
            raise StateError("Prepared stage recovery fresh authority differs from its record")
        self._span = context._span

    def _begin(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> RecoveryAttempt:
        self._require_context(context, deadline)
        if self.dispatch is not None or self._opening or self._beginning or self._cleanup_confirmed:
            raise StateError("Prepared stage recovery retains an earlier exchange or cleanup result")
        self._publish(2, self._marker)
        self._opening = True
        self.dispatch = self._bound.open_dispatch()
        self._opening = False
        self._beginning = True
        self.attempt = self.dispatch.begin_attempt()
        self._beginning = False
        self._helper_terminated = False
        return self.attempt

    def _publish(self, version: int, payload: bytes) -> None:
        """Retain one exact CAS before its reply can be lost, with no remote entry."""
        if self._publication is not None:
            self._confirm_publication()
        if (self._obligation.payload_version, self._obligation.payload) == (version, payload):
            return
        self._publication = (self._obligation, version, payload)
        self._confirm_publication()

    def _confirm_publication(self) -> None:
        pending = self._publication
        assert pending is not None
        base, version, payload = pending
        row = self._owner.inspect_lifecycle_obligation(base.obligation_id)
        if row == base:
            row = LifecycleObligation(self._owner, base).publish_payload(
                expected_revision=base.payload_revision, payload_version=version, payload=payload
            )
        elif (
            row is None
            or row.ownership != base.ownership
            or row.obligation_kind != base.obligation_kind
            or row.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or row.registered_at != base.registered_at
            or (row.payload_version, row.payload, row.payload_revision) != (version, payload, base.payload_revision + 1)
        ):
            raise StateError("Prepared stage recovery publication conflicts with its exact retained transition")
        self._obligation = row
        self._bound = self._owner.rebind_possible_effect_lifecycle_obligation(
            row.obligation_id,
            row.obligation_kind,
            payload_version=version,
            payload=payload,
            payload_revision=row.payload_revision,
        )
        self._publication = None

    def reconcile(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> FileStageCandidateResult:
        """Recover historical cleanup debt only; missing receipts remain debt."""
        attempt = self._begin(context, deadline)
        result = stage_reconcile(
            AttemptBoundHelperCarrier(context.carrier, attempt),
            trusted_root_path="/tmp",
            relative_path="prepared-input",
            token=self._record.token,
            plan=context.root_plan,
            deadline=deadline,
            runtime_selection=context.runtime_selection,
            bootstrap=_NumericGuestBootstrap(context.root_plan, context.guest),
            fixed_scratch_root=True,
        )
        self.last_candidate = result
        observation = result.observation
        if observation is not None and observation.state is FileStageObservationState.RECOVERED:
            self.cleanup_debt = observation.cleanup_debt
        self._complete(result, deadline)
        return result

    def cleanup(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> FileStageCandidateResult:
        """Remove only the exact retained debt, retaining positive cleanup before resolution."""
        self._require_context(context, deadline)
        if self.cleanup_debt is None:
            raise StateError("Prepared stage recovery has no historical cleanup debt")
        attempt = self._begin(context, deadline)
        result = stage_cleanup(
            AttemptBoundHelperCarrier(context.carrier, attempt),
            trusted_root_path="/tmp",
            relative_path="prepared-input",
            token=self._record.token,
            cleanup_debt=self.cleanup_debt,
            plan=context.root_plan,
            deadline=deadline,
            runtime_selection=context.runtime_selection,
            bootstrap=_NumericGuestBootstrap(context.root_plan, context.guest),
            fixed_scratch_root=True,
        )
        self.last_candidate = result
        observation = result.observation
        self._cleanup_confirmed = (
            result.dispatch is Dispatch.SENT
            and result.carrier_completion == ExitStatus(0)
            and result.carrier_failure is None
            and result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
            and observation is not None
            and observation.state is FileStageObservationState.CLEANED
            and observation.failure is None
            and observation.error is None
        )
        self._complete(result, deadline)
        if self.cleaned:
            self.retry_resolution(context, deadline)
        return result

    def _complete(self, result: FileStageCandidateResult, deadline: Deadline) -> None:
        self._helper_terminated = result.dispatch is Dispatch.NOT_SENT or (
            result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(0)
        )
        if self._helper_terminated:
            self.drain(deadline)

    def drain(self, deadline: Deadline) -> RecoveryDrain:
        """Drain actual retained local delivery without manufacturing helper termination."""
        self._require_thread()
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Prepared stage recovery drain requires a finite budget")
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
                raise StateError("Prepared stage recovery has conflicting local settlement custody")
            self.attempt = None
        if self.dispatch is not None:
            self.dispatch.close()
            self.dispatch = None
        if self._cleanup_confirmed and not self.cleaned:
            self.resolution_pending = True
            self.cleaned = True
        if self._publication is not None:
            self._confirm_publication()
        if not self._cleanup_confirmed and self._obligation.payload_version == 2:
            self._publish(1, self._original_payload)
        return RecoveryDrain(True, self._helper_terminated, False)

    def retry_resolution(self, context: RecoveryVMPreparedContext, deadline: Deadline) -> None:
        """Retry exact row bookkeeping only, never remote removal."""
        self._require_context(context, deadline)
        if not self.cleaned or self.dispatch is not None or self._opening or self._beginning:
            raise StateError("Prepared stage recovery requires completed cleanup and closed local custody")
        with self._owner._guard:
            self._owner._require_no_active_work_locked()
            row = self._owner._repository.inspect_lifecycle_obligation(
                self._owner.ownership, self._obligation.obligation_id
            )
            if (
                row is None
                or row.state not in {LifecycleObligationState.POSSIBLE_EFFECT, LifecycleObligationState.RESOLVED}
                or replace(row, state=self._obligation.state, updated_at=self._obligation.updated_at)
                != self._obligation
            ):
                raise StateError("Prepared stage recovery resolution binding changed")
            self._owner._repository.resolve_lifecycle_obligation(self._owner.ownership, row.obligation_id)
        self.resolution_pending = False
