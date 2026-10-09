"""Caller-held custody for one serialized local delivery attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.errors import StateError, ValidationError
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import LocalProcessOwner

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline


class LocalDeliveryCleanup(Protocol):
    """Ordered local cleanup retained before resources or native dispatch."""

    @property
    def settled(self) -> bool:
        """Passively report whether all coordinated cleanup is complete."""
        ...

    def close(self, deadline: Deadline) -> None:
        """Stop borrowers, close the held owner, and restore resources in order."""
        ...


class LocalDeliveryCustody:
    """Retain the exact owner until its latest cleanup is proved complete.

    Construction and observation are passive. The caller serializes dispatch
    and close. Without a coordinator, it stops pipe borrowing before closure;
    a retained coordinator owns that ordering and subsequent resource cleanup.
    """

    def __init__(self) -> None:
        self._owner: LocalProcessOwner | None = None
        self._cleanup: LocalDeliveryCleanup | None = None

    @property
    def settled(self) -> bool:
        if self._owner is None:
            return True
        terminal = self._owner.snapshot().terminal
        process_settled = terminal is not None and terminal.cleaned
        cleanup_settled = self._cleanup is None or self._cleanup.settled
        return process_settled and cleanup_settled

    def retain_cleanup(self, owner: LocalProcessOwner, cleanup: LocalDeliveryCleanup) -> None:
        """Bind one inert cleanup coordinator to the exact currently held owner."""
        if owner is not self._owner:
            raise StateError("Local delivery cleanup requires the currently held owner")
        if self._cleanup is not None:
            raise StateError("Local delivery cleanup is already retained")
        self._cleanup = cleanup

    def begin_process(self) -> LocalProcessOwner:
        """Hold an inert owner before its caller can admit native dispatch."""
        if not self.settled:
            raise StateError("Previous local delivery cleanup remains unsettled")
        owner = LocalProcessOwner()
        self._owner = owner
        self._cleanup = None
        return owner

    def close(self, deadline: Deadline) -> bool:
        """Explicitly request bounded cleanup, retaining pending or lost custody."""
        if deadline.expires_at is None:
            raise ValidationError("Local delivery cleanup requires a finite deadline")
        if self._owner is None:
            return True
        if self._cleanup is None:
            self._owner.close_bounded(ProcessDeadline(deadline.expires_at))
        else:
            self._cleanup.close(deadline)
        return self.settled
