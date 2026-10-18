"""Optional per-call native delivery with later exact-helper closure observation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.execution.carrier import Carrier

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline


class HelperClosureDelivery(Carrier, Protocol):
    """Retain no caller payload; observation never dispatches another helper."""

    @property
    def closure_proven(self) -> bool: ...

    def observe_closure(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> bool: ...
