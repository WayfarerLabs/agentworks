"""Core ownership for one durably coordinated resource operation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db.operations import OperationClaimState, OperationOwnership, OperationScope
from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.db.operations import OperationRepository


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
        self._outstanding_attempt: OperationAttempt | None = None
        self._durable_possible_dispatch = False
        self._effects_resolved = False
        self._transition_uncertain = False
        self._close_requested = False
        self._release_may_have_committed = False
        self._released = False

    @classmethod
    def acquire(
        cls,
        repository: OperationRepository,
        scope: OperationScope,
        operation_kind: str,
    ) -> OperationOwner:
        """Claim an exact core-selected scope before any workflow effects."""
        return cls(repository, repository.claim(scope, operation_kind))

    @property
    def ownership(self) -> OperationOwnership:
        return self._ownership

    def arm(self) -> None:
        """Durably permit a whole-operation lifecycle effect.

        Unlike a borrowed attempt, successful arming leaves no in-memory
        attempt active. An interrupted transition retains ownership until its
        durable state is reconciled.
        """
        with self._guard:
            self._require_no_active_work_locked()
            if self._transition_uncertain:
                self._reconcile_transition_locked()
            self._require_dispatch_admission_locked()
            if self._durable_possible_dispatch:
                return
            self._transition_uncertain = True
            self._repository.mark_possible_dispatch(self._ownership)
            self._durable_possible_dispatch = True
            self._transition_uncertain = False

    def borrow(self) -> OperationBorrow:
        """Borrow the whole-operation serial guard without waiting."""
        with self._guard:
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
            if self._effects_resolved:
                return
            if not self._durable_possible_dispatch:
                raise StateError(
                    "operation ownership was not armed for dispatch",
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
        with self._guard:
            self._close_requested = True
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
        if not self._durable_possible_dispatch:
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
        if claim.ownership != self._ownership and self._release_may_have_committed:
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
        self._transition_uncertain = False

    def _require_no_active_work_locked(self) -> None:
        if self._active_borrow is not None or self._outstanding_attempt is not None:
            raise StateError(
                "operation ownership still has active or unresolved work",
                entity_kind=self._ownership.scope.resource_kind,
                entity_name=self._ownership.scope.resource_name,
            )

    def _require_dispatch_admission_locked(self) -> None:
        if self._close_requested or self._released:
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


@dataclass(slots=True, repr=False)
class OperationBorrow:
    """One identity-bound serial borrow from an :class:`OperationOwner`."""

    _owner: OperationOwner
    _closed: bool = field(default=False, init=False)

    @property
    def ownership(self) -> OperationOwnership:
        return self._owner.ownership

    @property
    def has_outstanding_attempt(self) -> bool:
        """Return whether this borrow originated the owner's current attempt."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            attempt = owner._outstanding_attempt  # noqa: SLF001
            return attempt is not None and attempt._borrow is self  # noqa: SLF001

    def begin_attempt(self) -> OperationAttempt:
        """Durably arm ownership before returning permission to dispatch.

        The unresolved marker is installed first. If the database transition
        is interrupted or fails, no attempt is returned and ownership remains
        conservatively active.
        """
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            if owner._close_requested:  # noqa: SLF001
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
            attempt = OperationAttempt(self)
            owner._outstanding_attempt = attempt  # noqa: SLF001
            if not owner._durable_possible_dispatch:  # noqa: SLF001
                owner._repository.mark_possible_dispatch(self.ownership)  # noqa: SLF001
                owner._durable_possible_dispatch = True  # noqa: SLF001
            return attempt

    def close(self) -> None:
        """Relinquish this borrow without releasing the durable owner."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            self._require_active_locked()
            self._closed = True
            owner._active_borrow = None  # noqa: SLF001

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
    _settled: bool = field(default=False, init=False)

    def settle(self) -> None:
        """Acknowledge caller-established no-further-effects evidence."""
        borrow = self._borrow
        owner = borrow._owner  # noqa: SLF001
        with owner._guard:  # noqa: SLF001
            borrow._require_active_locked()  # noqa: SLF001
            if self._settled or owner._outstanding_attempt is not self:  # noqa: SLF001
                raise StateError(
                    "operation attempt is no longer outstanding",
                    entity_kind=borrow.ownership.scope.resource_kind,
                    entity_name=borrow.ownership.scope.resource_name,
                )
            self._settled = True
            owner._outstanding_attempt = None  # noqa: SLF001
