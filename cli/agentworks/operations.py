"""Core ownership for one durably coordinated resource operation."""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db.operations import (
    LifecycleObligation as PersistedLifecycleObligation,
)
from agentworks.db.operations import (
    LifecycleObligationState,
    OperationClaimState,
    OperationOwnership,
    OperationScope,
)
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody

if TYPE_CHECKING:
    from agentworks.db.database import Database
    from agentworks.db.operations import OperationRepository
    from agentworks.execution.carrier import Deadline


_recovery_owners_lock = threading.Lock()
_recovery_owners: weakref.WeakKeyDictionary[
    Database, weakref.WeakValueDictionary[OperationOwnership, OperationOwner]
] = weakref.WeakKeyDictionary()


class _PreRegistrationRefusal(StateError):
    """No registration occurred: closing refused it or exact fenced absence proved it."""


def _is_pre_registration_refusal(control: BaseException) -> bool:
    """Recognize core's direct refusal or immediate proven-absent cause."""
    return isinstance(control, _PreRegistrationRefusal) or isinstance(control.__cause__, _PreRegistrationRefusal)


class OperationOwner:
    """Own one coarse database claim and lend its serial-use boundary.

    Acquisition belongs to core orchestration. Consumers borrow the owner for
    one complete nested operation and settle each carrier attempt before
    another can begin. Closing never infers whole-operation resolution from
    settled child attempts.
    """

    def __init__(
        self,
        repository: OperationRepository,
        ownership: OperationOwnership,
    ) -> None:
        self._repository = repository
        self._ownership = ownership
        self._guard = threading.Lock()
        self._active_borrow: OperationBorrow | None = None
        self._active_recovery_dispatch: RecoveryDispatch | None = None
        self._outstanding_attempt: OperationAttempt | RecoveryAttempt | None = None
        self._durable_possible_dispatch = False
        self._effects_resolved = False
        self._obligations_sealed = False
        self._transition_uncertain = False
        self._close_requested = threading.Event()
        self._release_may_have_committed = False
        self._released = False
        self._recovery_owner = False

    @classmethod
    def acquire(
        cls,
        repository: OperationRepository,
        scope: OperationScope,
        operation_kind: str,
    ) -> OperationOwner:
        """Claim an exact core-selected scope before any workflow effects."""
        return cls(repository, repository.claim(scope, operation_kind))

    @classmethod
    def recover(
        cls,
        repository: OperationRepository,
        predecessor: OperationOwnership,
        generation_id: str,
    ) -> OperationOwner:
        """Take over one exact predecessor without inferring remote quiescence.

        Recovery seals ordinary ledger registrations and restores only durable claim
        facts. The caller still establishes every adapter and whole-operation
        no-further-effects fact before resolution or release.
        """
        claim = repository.recover_takeover(predecessor, generation_id)
        controller = repository._controller_identity  # noqa: SLF001
        with _recovery_owners_lock:
            owners = _recovery_owners.setdefault(controller, weakref.WeakValueDictionary())
            owner = owners.get(claim.ownership)
            if owner is None:
                owner = cls(repository, claim.ownership)
                owner._recovery_owner = True
                owner._durable_possible_dispatch = claim.state is not OperationClaimState.RESERVED
                owner._effects_resolved = claim.state is OperationClaimState.RESOLVED
                owner._obligations_sealed = claim.obligations_sealed_at is not None
                owners[claim.ownership] = owner
            return owner

    @property
    def ownership(self) -> OperationOwnership:
        return self._ownership

    def stop_admission(self) -> None:
        """Publish close intent without waiting for admitted work or proving quiescence."""
        self._close_requested.set()

    def close_local_delivery(self, deadline: Deadline) -> bool:
        """Drain the held attempt without reopening dispatch or resolving effects.

        This remains available after borrow handoff. The outstanding attempt
        and durable remote debt are unchanged, even when local cleanup succeeds.
        Plain delivery requires the caller to stop pipe use before cleanup.
        Coordinated delivery retains that borrower shutdown and subsequent
        resource cleanup with its exact local delivery coordinator.
        """
        with self._guard:
            attempt = self._outstanding_attempt
            return attempt is None or attempt.local_delivery.close(deadline)

    def list_pending_lifecycle_obligations(self) -> tuple[PersistedLifecycleObligation, ...]:
        """Read bounded unfinished lifecycle debt under current ownership."""
        with self._guard:
            return self._repository.list_pending_lifecycle_obligations(self._ownership)

    def inspect_lifecycle_obligation(self, obligation_id: str) -> PersistedLifecycleObligation | None:
        """Read one exact receipt; missing is meaningful only under current ownership."""
        with self._guard:
            return self._repository.inspect_lifecycle_obligation(self._ownership, obligation_id)

    def register_lifecycle_obligation(
        self,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
        obligation_id: str | None = None,
    ) -> LifecycleObligation:
        """Register one adapter-owned effect before it can be admitted."""
        with self._guard:
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            self._require_dispatch_admission_locked()
            if self._obligations_sealed:
                raise StateError(
                    "operation lifecycle obligations are sealed",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            obligation = self._register_lifecycle_obligation_locked(
                uuid4().hex if obligation_id is None else obligation_id, obligation_kind, payload_version, payload
            )
            return LifecycleObligation(self, obligation)

    def _register_lifecycle_obligation_locked(
        self, obligation_id: str, obligation_kind: str, payload_version: int, payload: bytes
    ) -> PersistedLifecycleObligation:
        try:
            return self._repository.register_lifecycle_obligation(
                self._ownership, obligation_kind, payload_version, payload, obligation_id=obligation_id
            )
        except Exception as cause:
            self._classify_registration_failure_locked(cause, obligation_id)
            raise

    def _classify_registration_failure_locked(self, cause: Exception, obligation_id: str) -> None:
        # A reused exception object must not carry an earlier attempt's proof.
        previous = cause.__cause__
        if isinstance(previous, _PreRegistrationRefusal):
            cause.__cause__ = previous.__cause__
        try:
            if self._repository.inspect_lifecycle_obligation(self._ownership, obligation_id) is not None:
                return
            if self._transition_uncertain:
                self._reconcile_transition_locked()
        except Exception:
            return
        # Controls and present, stale or unreadable receipts retain uncertainty.
        # A current-owner exact read alone proves this registration absent.
        refusal = _PreRegistrationRefusal(
            "operation lifecycle registration did not persist",
            entity_kind=self._ownership.scope.resource_kind,
            entity_name=self._ownership.scope.resource_name,
        )
        refusal.__cause__ = cause.__cause__
        cause.__cause__ = refusal

    def admit_recovery_support_obligation(
        self,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
        obligation_id: str,
    ) -> LifecycleObligation:
        """Retain a support effect without reopening ordinary admission.

        Exact persistence retries confirm custody, not permission to repeat
        an uncertain dispatch. Core chooses the support adapter and retains
        its identifier before calling.
        """
        with self._guard:
            if not self._recovery_owner:
                raise StateError(
                    "support admission requires recovery ownership",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            self._require_dispatch_admission_locked()
            self._transition_uncertain = True
            try:
                obligation = self._repository.admit_recovery_support_obligation(
                    self._ownership,
                    obligation_kind,
                    payload_version,
                    payload,
                    obligation_id=obligation_id,
                )
            except Exception as cause:
                self._classify_registration_failure_locked(cause, obligation_id)
                raise
            self._durable_possible_dispatch = True
            self._transition_uncertain = False
            return LifecycleObligation(self, obligation)

    def rebind_lifecycle_obligation(
        self,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        """Rebind one exact persisted adapter obligation during recovery."""
        with self._guard:
            if not self._recovery_owner:
                raise StateError(
                    "lifecycle obligations can only be rebound by a recovery owner",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            if self._released:
                raise StateError(
                    "operation ownership is already released",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            obligation = self._repository.rebind_lifecycle_obligation(
                self._ownership,
                obligation_id,
                obligation_kind,
                payload_version,
                payload,
            )
            return LifecycleObligation(self, obligation)

    def rebind_possible_effect_lifecycle_obligation(
        self,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
        payload_revision: int,
    ) -> RecoveredLifecycleObligation:
        """Bind one exact admitted effect for a recovery-only dispatch.

        The returned handle deliberately has no registration, publication, or
        resolution operations.  It only opens a serial recovery dispatch.
        Adapters establish their own drain proof before opening that dispatch.
        """
        with self._guard:
            if not self._recovery_owner:
                raise StateError(
                    "recovery dispatch requires recovery ownership",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            self._require_dispatch_admission_locked()
            obligation = self._repository.rebind_possible_effect_lifecycle_obligation(
                self._ownership,
                obligation_id,
                obligation_kind,
                payload_version,
                payload,
                payload_revision,
            )
            return RecoveredLifecycleObligation(self, obligation)

    def seal_lifecycle_obligations(self) -> None:
        """Forbid later effect registration after workflow construction ends."""
        with self._guard:
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            if self._released:
                raise StateError(
                    "operation ownership is already released",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._repository.seal_lifecycle_obligations(self._ownership)
            self._obligations_sealed = True

    def borrow(self) -> OperationBorrow:
        """Borrow the whole-operation serial guard without waiting."""
        with self._guard:
            if self._recovery_owner:
                raise StateError(
                    "recovery ownership cannot admit ordinary dispatch",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._require_dispatch_admission_locked()
            if self._active_borrow is not None:
                raise StateError(
                    "operation ownership is already borrowed",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            if self._outstanding_attempt is not None:
                raise StateError(
                    "operation ownership has an outstanding attempt",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            borrowed = OperationBorrow(self)
            self._active_borrow = borrowed
            return borrowed

    def record_effects_resolved(self) -> None:
        """Persist caller-established evidence that effects cannot remain.

        Resolution is whole-operation evidence, not an inference from settled
        child attempts. Retrying after an interrupted transition first reads
        the fenced claim to distinguish a committed resolution from a retry.
        """
        with self._guard:
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            if self._released:
                raise StateError(
                    "operation ownership is already released",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            if not self._obligations_sealed:
                raise StateError(
                    "operation lifecycle obligations must be sealed before resolution",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._transition_uncertain = True
            self._repository.record_effects_resolved(self._ownership)
            self._effects_resolved = True
            self._transition_uncertain = False

    def close(self) -> None:
        """Stop admission and release only after explicit effects resolution.

        A refused close stays closed to new borrowers. The current borrower
        may only record and settle its already-started attempt and relinquish
        the borrow. Later close calls require the caller to record
        whole-operation no-further-effects evidence before finalization.
        """
        self._close_requested.set()
        with self._guard:
            self._require_no_active_work_locked()
            self._finalize_close_locked()

    def _finalize_close_locked(self) -> None:
        if self._released:
            return
        if self._transition_uncertain:
            self._reconcile_transition_locked()
        if self._released:
            return
        if self._effects_resolved:
            self._release_resolved_locked()
            return
        if not self._durable_possible_dispatch and not self._obligations_sealed:
            self._transition_uncertain = True
            self._release_may_have_committed = True
            self._repository.abandon_reserved(self._ownership)
            self._released = True
            self._transition_uncertain = False
            return
        raise StateError(
            "operation ownership effects require explicit resolution",
            entity_kind=self._ownership.scope.resource_kind,
            entity_name=self._ownership.scope.resource_name,
        )

    def _reconcile_transition_locked(self) -> None:
        claim = self._repository.inspect(self._ownership.scope)
        if claim is None:
            if self._release_may_have_committed:
                self._transition_uncertain = False
                self._released = True
                return
            raise StateError(
                "operation ownership disappeared before safe release",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )
        if claim.ownership.operation_id != self._ownership.operation_id and self._release_may_have_committed:
            self._transition_uncertain = False
            self._released = True
            return
        if claim.ownership != self._ownership:
            raise StateError(
                "operation ownership is stale",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )
        self._durable_possible_dispatch = claim.state is not OperationClaimState.RESERVED
        self._effects_resolved = claim.state is OperationClaimState.RESOLVED
        self._obligations_sealed = claim.obligations_sealed_at is not None
        self._transition_uncertain = False

    def _require_no_active_work_locked(self) -> None:
        if (
            self._active_borrow is not None
            or self._active_recovery_dispatch is not None
            or self._outstanding_attempt is not None
        ):
            raise StateError(
                "operation ownership still has active or unresolved work",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )

    def _require_dispatch_admission_locked(self) -> None:
        if self._close_requested.is_set() or self._released:
            raise StateError(
                "operation ownership is closing",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )
        if self._transition_uncertain:
            raise StateError(
                "operation ownership transition is uncertain",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )
        if self._effects_resolved:
            raise StateError(
                "operation ownership effects are already resolved",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )

    def _release_resolved_locked(self) -> None:
        self._release_may_have_committed = True
        self._transition_uncertain = True
        self._repository.release_resolved(self._ownership)
        self._released = True
        self._transition_uncertain = False

    def _register_carrier_dispatch_locked(self, obligation_id: str) -> PersistedLifecycleObligation:
        """Register the one generic carrier effect for an active borrow."""
        if self._obligations_sealed:
            raise StateError(
                "operation lifecycle obligations are sealed",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )
        return self._repository.register_lifecycle_obligation(
            self._ownership,
            "carrier-dispatch",
            1,
            b"",
            obligation_id=obligation_id,
        )


@dataclass(slots=True, repr=False)
class LifecycleObligation:
    """Owner-bound handle for one independently discharged lifecycle effect."""

    _owner: OperationOwner
    _obligation: PersistedLifecycleObligation

    @property
    def obligation_id(self) -> str:
        return self._obligation.obligation_id

    @property
    def state(self) -> object:
        return self._obligation.state

    @property
    def payload_revision(self) -> int:
        return self._obligation.payload_revision

    def mark_possible_effect(self) -> None:
        """Commit admission before the owning adapter begins its effect."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            if owner._recovery_owner and self._obligation.state is LifecycleObligationState.REGISTERED:
                raise StateError(
                    "recovery ownership cannot admit a registered lifecycle obligation",
                    entity_kind=owner._ownership.scope.resource_kind,  # noqa: SLF001
                    entity_name=owner._ownership.scope.resource_name,  # noqa: SLF001
                )
            owner._require_dispatch_admission_locked()  # noqa: SLF001
            owner._transition_uncertain = True  # noqa: SLF001
            self._obligation = owner._repository.mark_lifecycle_obligation_possible_effect(  # noqa: SLF001
                owner._ownership,  # noqa: SLF001
                self.obligation_id,
            )
            owner._durable_possible_dispatch = True  # noqa: SLF001
            owner._transition_uncertain = False  # noqa: SLF001

    def publish_payload(
        self,
        *,
        expected_revision: int,
        payload_version: int,
        payload: bytes,
    ) -> PersistedLifecycleObligation:
        """CAS-publish adapter identity while this effect remains possible."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            if owner._released:  # noqa: SLF001
                raise StateError(
                    "operation ownership is already released",
                    entity_kind=owner._ownership.scope.resource_kind,  # noqa: SLF001
                    entity_name=owner._ownership.scope.resource_name,  # noqa: SLF001
                )
            if owner._active_recovery_dispatch is not None or isinstance(  # noqa: SLF001
                owner._outstanding_attempt,
                RecoveryAttempt,  # noqa: SLF001
            ):
                raise StateError(
                    "recovery dispatch prevents lifecycle payload publication",
                    entity_kind=owner._ownership.scope.resource_kind,  # noqa: SLF001
                    entity_name=owner._ownership.scope.resource_name,  # noqa: SLF001
                )
            self._obligation = owner._repository.publish_lifecycle_obligation_payload(  # noqa: SLF001
                owner._ownership,  # noqa: SLF001
                self.obligation_id,
                expected_revision=expected_revision,
                payload_version=payload_version,
                payload=payload,
            )
            return self._obligation

    @property
    def _persisted_obligation(self) -> PersistedLifecycleObligation:
        """Return the exact persisted row for private recovery adapters."""
        return self._obligation

    def resolve(self) -> None:
        """Persist adapter-established no-further-effects evidence."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            owner._require_no_active_work_locked()  # noqa: SLF001
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            if owner._released:  # noqa: SLF001
                raise StateError(
                    "operation ownership is already released",
                    entity_kind=owner._ownership.scope.resource_kind,  # noqa: SLF001
                    entity_name=owner._ownership.scope.resource_name,  # noqa: SLF001
                )
            self._obligation = owner._repository.resolve_lifecycle_obligation(  # noqa: SLF001
                owner._ownership,  # noqa: SLF001
                self.obligation_id,
            )


@dataclass(slots=True, repr=False)
class RecoveredLifecycleObligation:
    """One exact recovery-only binding for an already possible effect.

    This narrow handle has no generic durable mutation methods.  A caller can
    only open a :class:`RecoveryDispatch`, which rechecks the retained row for
    each concrete adapter attempt.
    """

    _owner: OperationOwner
    _obligation: PersistedLifecycleObligation
    _local_dispatch: RecoveryDispatch | None = field(default=None, init=False)

    def open_dispatch(self) -> RecoveryDispatch:
        """Reserve this owner for one recovery dispatcher without durable mutation."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            # A refused new opening must not retain an earlier caller's handle.
            self._local_dispatch = None
            owner._require_no_active_work_locked()  # noqa: SLF001
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            owner._require_dispatch_admission_locked()  # noqa: SLF001
            dispatch = RecoveryDispatch(owner, self._obligation)
            self._local_dispatch = dispatch
            owner._active_recovery_dispatch = dispatch  # noqa: SLF001
            return dispatch

    def _close_retained_dispatch(self) -> None:
        """Clean up a fresh, exclusively retained binding's unreturned open.

        No concrete dispatch may have escaped. Only this opening's retained
        candidate may close, with no outstanding attempt or conflicting
        dispatcher. This supplies no remote proof.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            dispatch = self._local_dispatch
            if dispatch is None:
                owner._require_no_active_work_locked()  # noqa: SLF001
                return
            if owner._active_recovery_dispatch is None:  # noqa: SLF001
                owner._require_no_active_work_locked()  # noqa: SLF001
                dispatch._closed = True  # noqa: SLF001
                return
        dispatch.close()


@dataclass(slots=True, repr=False)
class RecoveryDispatch:
    """Serial custody for adapter-specific recovery work.

    This type intentionally has no carrier, invocation, publication, or
    resolution API.  Its attempt only revalidates durable recovery admission;
    the adapter owns concrete dispatch and proof of its effects.
    """

    _owner: OperationOwner
    _obligation: PersistedLifecycleObligation
    _closed: bool = field(default=False, init=False)
    _close_started: bool = field(default=False, init=False)

    @property
    def ownership(self) -> OperationOwnership:
        return self._owner.ownership

    def begin_attempt(self) -> RecoveryAttempt:
        """Revalidate exact durable admission before one adapter dispatch."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "recovery dispatch already has an outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            owner._require_dispatch_admission_locked()  # noqa: SLF001
            self._obligation = owner._repository.rebind_possible_effect_lifecycle_obligation(  # noqa: SLF001
                owner._ownership,  # noqa: SLF001
                self._obligation.obligation_id,
                self._obligation.obligation_kind,
                self._obligation.payload_version,
                self._obligation.payload,
                self._obligation.payload_revision,
            )
            attempt = RecoveryAttempt(self)
            owner._outstanding_attempt = attempt  # noqa: SLF001
            return attempt

    def close(self) -> None:
        """Release settled local custody, retrying only this exact close."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            active = owner._active_recovery_dispatch  # noqa: SLF001
            if active is not self and (active is not None or not self._close_started):
                raise StateError(
                    "recovery dispatch has no exact local close custody",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            if owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "recovery dispatch has an outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            self._close_started = True
            owner._active_recovery_dispatch = None  # noqa: SLF001
            self._closed = True

    def _abort_unreturned_attempt(self) -> None:
        """Retry exact local teardown after begin fails before effect entry."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            active = owner._active_recovery_dispatch  # noqa: SLF001
            if active is not self and (active is not None or not self._close_started):
                raise StateError(
                    "recovery dispatch has no exact local abort custody",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            attempt = owner._outstanding_attempt  # noqa: SLF001
            if attempt is not None and (
                not isinstance(attempt, RecoveryAttempt) or attempt._dispatch is not self  # noqa: SLF001
            ):
                raise StateError(
                    "recovery dispatch has another outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            self._close_started = True
            owner._outstanding_attempt = None  # noqa: SLF001
            owner._active_recovery_dispatch = None  # noqa: SLF001
            self._closed = True

    def handoff_unresolved(self) -> None:
        """Relinquish local custody while retaining an uncertain effect."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            attempt = owner._outstanding_attempt  # noqa: SLF001
            if not isinstance(attempt, RecoveryAttempt) or attempt._dispatch is not self:  # noqa: SLF001
                raise StateError(
                    "recovery dispatch has no outstanding attempt to retain",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            owner._active_recovery_dispatch = None  # noqa: SLF001
            self._closed = True

    def _require_active_locked(self) -> None:
        if self._closed or self._owner._active_recovery_dispatch is not self:  # noqa: SLF001
            raise StateError(
                "recovery dispatch is no longer active",
                entity_kind=self.ownership.scope.resource_kind,
                entity_name=self.ownership.scope.resource_name,
            )


@dataclass(slots=True, repr=False)
class RecoveryAttempt:
    """One local recovery attempt that never settles a durable obligation."""

    _dispatch: RecoveryDispatch
    local_delivery: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody, init=False, repr=False)

    def settle(self) -> None:
        """Release only this adapter attempt after its own termination proof."""
        dispatch = self._dispatch
        owner = dispatch._owner  # noqa: SLF001
        with owner._guard:  # noqa: SLF001
            dispatch._require_active_locked()  # noqa: SLF001
            if not self.local_delivery.settled:
                raise StateError("recovery attempt retains unsettled local delivery")
            if owner._outstanding_attempt is not self:  # noqa: SLF001
                raise StateError(
                    "recovery attempt is no longer outstanding",
                    entity_kind=dispatch.ownership.scope.resource_kind,
                    entity_name=dispatch.ownership.scope.resource_name,
                )
            owner._outstanding_attempt = None  # noqa: SLF001


@dataclass(slots=True, repr=False)
class OperationBorrow:
    """One identity-bound serial borrow from an :class:`OperationOwner`."""

    _owner: OperationOwner
    _dispatch_obligation: PersistedLifecycleObligation | None = field(default=None, init=False)
    _dispatch_obligation_id: str | None = field(default=None, init=False)
    _supplied_dispatch: tuple[str, str, int, bytes] | None = field(default=None, init=False)
    _dispatch_armed: bool = field(default=False, init=False)
    _attempt_started: bool = field(default=False, init=False)
    _closing: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)

    @property
    def ownership(self) -> OperationOwnership:
        return self._owner.ownership

    @property
    def closed(self) -> bool:
        """Whether local borrow authority ended, including unresolved handoff.

        This does not prove an effect resolved or any adapter checkpoint.
        """
        with self._owner._guard:  # noqa: SLF001
            return self._closed

    @property
    def has_outstanding_attempt(self) -> bool:
        """Return whether this borrow originated the owner's current attempt."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            attempt = owner._outstanding_attempt  # noqa: SLF001
            return isinstance(attempt, OperationAttempt) and attempt._borrow is self  # noqa: SLF001

    @property
    def dispatch_obligation_may_be_armed(self) -> bool:
        """Return whether this borrow may have admitted its dispatch obligation."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            return self._dispatch_armed

    @property
    def has_installed_dispatch_obligation(self) -> bool:
        """Return whether this borrow received its durable obligation row."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            return self._dispatch_obligation is not None

    def install_dispatch_obligation(
        self,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        """Install this borrow's adapter-owned dispatch obligation.

        The caller retains the fresh identifier so an interrupted registration
        can repeat the exact durable request without creating another row.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            if self._closing or owner._close_requested.is_set():  # noqa: SLF001
                if (
                    self._supplied_dispatch is None
                    and self._dispatch_obligation is None
                    and self._dispatch_obligation_id is None
                ):
                    raise _PreRegistrationRefusal(
                        "operation borrow is closing",
                        entity_kind=self.ownership.scope.resource_kind,
                        entity_name=self.ownership.scope.resource_name,
                    )
                raise StateError(
                    "operation borrow is closing",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            if self._attempt_started:
                raise StateError(
                    "operation borrow dispatch obligation must be installed before its first attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            supplied = (obligation_id, obligation_kind, payload_version, payload)
            if self._supplied_dispatch is not None:
                if self._supplied_dispatch != supplied:
                    raise StateError(
                        "operation borrow already has a dispatch obligation",
                        entity_kind=self.ownership.scope.resource_kind,
                        entity_name=self.ownership.scope.resource_name,
                    )
                if self._dispatch_obligation is not None:
                    return LifecycleObligation(owner, self._dispatch_obligation)
            elif self._dispatch_obligation is not None:
                raise StateError(
                    "operation borrow already has a dispatch obligation",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            else:
                self._supplied_dispatch = supplied
                self._dispatch_obligation_id = obligation_id
            owner._transition_uncertain = True  # noqa: SLF001
            self._dispatch_obligation = owner._register_lifecycle_obligation_locked(  # noqa: SLF001
                obligation_id, obligation_kind, payload_version, payload
            )
            owner._transition_uncertain = False  # noqa: SLF001
            return LifecycleObligation(owner, self._dispatch_obligation)

    def arm_dispatch_obligation(self) -> None:
        """Durably admit an installed adapter effect before a later carrier attempt.

        Some adapters must commit their own possible-dispatch row between core
        admission and the carrier attempt. A lost database reply retains the
        borrow's handoff authority conservatively; a later attempt rechecks
        and repeats the idempotent durable admission before dispatch.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            owner._require_dispatch_admission_locked()  # noqa: SLF001
            if self._closing or self._attempt_started or owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "operation borrow cannot arm an active or closing attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            obligation = self._dispatch_obligation
            if obligation is None or self._supplied_dispatch is None:
                raise StateError(
                    "operation borrow has no installed adapter obligation",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            self._dispatch_armed = True
            owner._transition_uncertain = True  # noqa: SLF001
            owner._repository.mark_lifecycle_obligation_possible_effect(  # noqa: SLF001
                self.ownership,
                obligation.obligation_id,
            )
            owner._durable_possible_dispatch = True  # noqa: SLF001
            owner._transition_uncertain = False  # noqa: SLF001

    def begin_attempt(self) -> OperationAttempt:
        """Durably arm ownership before returning permission to dispatch.

        The unresolved marker is installed first. If the database transition
        is interrupted or fails, no attempt is returned and ownership remains
        conservatively active.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._transition_uncertain:  # noqa: SLF001
                owner._reconcile_transition_locked()  # noqa: SLF001
            if self._closing:
                raise StateError(
                    "operation borrow is closing",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            if owner._close_requested.is_set():  # noqa: SLF001
                raise StateError(
                    "operation ownership is closing",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            if owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "operation borrow already has an outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            self._attempt_started = True
            if self._dispatch_obligation is None:
                if self._dispatch_obligation_id is None:
                    self._dispatch_obligation_id = uuid4().hex
                owner._transition_uncertain = True  # noqa: SLF001
                if self._supplied_dispatch is None:
                    obligation = owner._register_carrier_dispatch_locked(self._dispatch_obligation_id)  # noqa: SLF001
                else:
                    obligation = owner._repository.register_lifecycle_obligation(  # noqa: SLF001
                        self.ownership,
                        self._supplied_dispatch[1],
                        self._supplied_dispatch[2],
                        self._supplied_dispatch[3],
                        obligation_id=self._dispatch_obligation_id,
                    )
                self._dispatch_obligation = obligation
            attempt = OperationAttempt(self)
            owner._outstanding_attempt = attempt  # noqa: SLF001
            obligation = self._dispatch_obligation
            assert obligation is not None
            self._dispatch_armed = True
            owner._transition_uncertain = True  # noqa: SLF001
            owner._repository.mark_lifecycle_obligation_possible_effect(  # noqa: SLF001
                self.ownership,
                obligation.obligation_id,
            )
            owner._durable_possible_dispatch = True  # noqa: SLF001
            owner._transition_uncertain = False  # noqa: SLF001
            return attempt

    def close(self) -> None:
        """Complete this borrow after all of its attempts are settled."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "operation borrow has an outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            self._closing = True
            if self._dispatch_obligation is not None:
                owner._transition_uncertain = True  # noqa: SLF001
                owner._repository.resolve_lifecycle_obligation(  # noqa: SLF001
                    self.ownership,
                    self._dispatch_obligation.obligation_id,
                )
                owner._transition_uncertain = False  # noqa: SLF001
            owner._active_borrow = None  # noqa: SLF001
            self._closed = True

    def handoff_retained_effect(self) -> None:
        """Relinquish a settled borrow while retaining its armed adapter effect."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._outstanding_attempt is not None:  # noqa: SLF001
                raise StateError(
                    "operation borrow has an outstanding attempt",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            obligation = self._dispatch_obligation
            if not self._dispatch_armed or obligation is None or self._supplied_dispatch is None:
                raise StateError(
                    "operation borrow has no armed adapter effect to retain",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            owner._active_borrow = None  # noqa: SLF001
            self._closed = True

    def handoff_unresolved(self) -> None:
        """Terminally retain this borrow's outstanding effect for recovery.

        The handoff only relinquishes in-memory borrow authority. It leaves
        both the outstanding attempt and its dispatch obligation unresolved,
        so the owner remains durably blocked from new work.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if self._closing:
                raise StateError(
                    "operation borrow is closing",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            attempt = owner._outstanding_attempt  # noqa: SLF001
            if not isinstance(attempt, OperationAttempt) or attempt._borrow is not self:  # noqa: SLF001
                raise StateError(
                    "operation borrow has no outstanding attempt to hand off",
                    entity_kind=self.ownership.scope.resource_kind,
                    entity_name=self.ownership.scope.resource_name,
                )
            owner._active_borrow = None  # noqa: SLF001
            self._closed = True

    def _require_active_locked(self) -> None:
        if self._closed or self._owner._active_borrow is not self:  # noqa: SLF001
            raise StateError(
                "operation borrow is no longer active",
                entity_kind=self.ownership.scope.resource_kind,
                entity_name=self.ownership.scope.resource_name,
            )


@dataclass(slots=True, repr=False)
class OperationAttempt:
    """The sole outstanding attempt originated by one serial borrow."""

    _borrow: OperationBorrow
    local_delivery: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody, init=False, repr=False)

    def settle(self) -> None:
        """Acknowledge caller-established no-further-effects evidence."""
        borrow = self._borrow
        owner = borrow._owner  # noqa: SLF001
        with owner._guard:  # noqa: SLF001
            borrow._require_active_locked()  # noqa: SLF001
            if not self.local_delivery.settled:
                raise StateError("operation attempt retains unsettled local delivery")
            if owner._outstanding_attempt is not self:  # noqa: SLF001
                raise StateError(
                    "operation attempt is no longer outstanding",
                    entity_kind=borrow.ownership.scope.resource_kind,
                    entity_name=borrow.ownership.scope.resource_name,
                )
            owner._outstanding_attempt = None  # noqa: SLF001


def release_borrow_after_custody(borrow: OperationBorrow, *, retain_effect: bool = False) -> None:
    """Complete a borrow or explicitly retain its outstanding custody or effect."""
    if borrow.has_outstanding_attempt:
        borrow.handoff_unresolved()
    elif retain_effect:
        borrow.handoff_retained_effect()
    else:
        borrow.close()
