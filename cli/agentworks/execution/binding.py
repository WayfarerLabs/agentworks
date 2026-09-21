"""Platform-owned native carrier and delivery identity facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection

if TYPE_CHECKING:
    from agentworks.execution.carrier import Carrier


@dataclass(frozen=True, slots=True)
class NativeExecutionBinding:
    """A passive native route binding, before target identity composition."""

    carrier: Carrier = field(repr=False)
    delivery_account: str
    runtime_selection: RuntimeSelection

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_account, str) or not self.delivery_account or "\0" in self.delivery_account:
            raise ValidationError("Native execution binding requires a literal delivery account")
        if type(self.runtime_selection) is not RuntimeSelection:
            raise ValidationError("Native execution binding requires an explicit runtime selection")
