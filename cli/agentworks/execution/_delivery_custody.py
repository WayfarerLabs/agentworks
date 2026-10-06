"""Caller-held custody for one serialized local delivery attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import LocalProcessOwner

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline


class LocalDeliveryCustody:
    """Retain the exact owner until its latest cleanup is proved complete.

    Construction and observation are passive. The caller serializes dispatch
    and close, and stops all pipe borrowing before requesting closure.
    """

    def __init__(self) -> None:
        self._owner: LocalProcessOwner | None = None

    @property
    def settled(self) -> bool:
        if self._owner is None:
            return True
        terminal = self._owner.snapshot().terminal
        return terminal is not None and terminal.cleaned

    def begin_process(self) -> LocalProcessOwner:
        """Hold an inert owner before its caller can admit native dispatch."""
        if not self.settled:
            raise StateError("Previous local delivery cleanup remains unsettled")
        owner = LocalProcessOwner()
        self._owner = owner
        return owner

    def close(self, deadline: Deadline) -> bool:
        """Explicitly request bounded cleanup, retaining pending or lost custody."""
        if deadline.expires_at is None:
            raise ValidationError("Local delivery cleanup requires a finite deadline")
        if self._owner is None:
            return True
        self._owner.close_bounded(ProcessDeadline(deadline.expires_at))
        return self.settled
