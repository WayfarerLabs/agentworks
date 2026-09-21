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
    another can begin. Closing never guesses that an outstanding attempt has
    stopped.
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
        self._close_requested = False
        self._close_transition_uncertain = False
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

    def borrow(self) -> OperationBorrow:
        """Borrow the whole-operation serial guard without waiting."""
        with self._guard:
            if self._close_requested or self._released:
                raise StateError(
                    "operation ownership is closing",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
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

    def close(self) -> None:
        """Stop admission and release only after all borrowed work settled.

        A refused close stays closed to new borrowers. The current borrower
        may only record and settle its already-started attempt and relinquish
        the borrow. Calling ``close`` again performs explicit finalization.
        """
        with self._guard:
            self._close_requested = True
            if self._active_borrow is not None or self._outstanding_attempt is not None:
                raise StateError(
                    "operation ownership still has active or unresolved work",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._finalize_close_locked()

    def _finalize_close_locked(self) -> None:
        if self._released:
            return
        if self._close_transition_uncertain:
            claim = self._repository.inspect(self._ownership.scope)
            if claim is None:
                if self._release_may_have_committed:
                    self._released = True
                    return
                raise StateError(
                    "operation ownership disappeared before safe release",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            if claim.ownership != self._ownership:
                raise StateError(
                    "operation ownership is stale",
                    entity_kind=self._ownership.scope.resource_kind,
                    entity_name=self._ownership.scope.resource_name,
                )
            self._durable_possible_dispatch = claim.state is not OperationClaimState.RESERVED
            if claim.state is OperationClaimState.RESOLVED:
                self._release_resolved_locked()
                return
            self._close_transition_uncertain = False
        if not self._durable_possible_dispatch:
            try:
                self._repository.abandon_reserved(self._ownership)
            except BaseException:
                self._close_transition_uncertain = True
                self._release_may_have_committed = True
                raise
            self._released = True
            return
        try:
            self._repository.record_effects_resolved(self._ownership)
        except BaseException:
            self._close_transition_uncertain = True
            raise
        self._release_resolved_locked()

    def _release_resolved_locked(self) -> None:
        self._release_may_have_committed = True
        try:
            self._repository.release_resolved(self._ownership)
        except BaseException:
            self._close_transition_uncertain = True
            raise
        self._released = True


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
            if self._settled or owner._outstanding_attempt is not self:  # noqa: SLF001
                raise StateError(
                    "operation attempt is no longer outstanding",
                    entity_kind=borrow.ownership.scope.resource_kind,
                    entity_name=borrow.ownership.scope.resource_name,
                )
            self._settled = True
            owner._outstanding_attempt = None  # noqa: SLF001
