"""Platform-owned native carrier and delivery identity facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._helper_closure import HelperClosureDelivery
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import HelperClosureExpectation
    from agentworks.execution.carrier import Carrier


class _IndependentJobAvailability(StrEnum):
    """Core-selected platform guarantee, separate from current guest readiness."""

    NO_IDLE_STOP = "no-idle-stop"


@dataclass(frozen=True, slots=True)
class _EarlyGuestFactsRoute:
    """Core-only root entry and named body for the initial fixed guest probe."""

    carrier: Carrier = field(repr=False)
    root_entry: IdentityPlan
    account: str


@dataclass(frozen=True, slots=True)
class NativeExecutionBinding:
    """A passive native route binding, before target identity composition."""

    carrier: Carrier = field(repr=False)
    delivery_account: str
    runtime_selection: RuntimeSelection
    _early_guest_facts_route: _EarlyGuestFactsRoute | None = field(default=None, repr=False)
    _new_managed_delivery: Callable[[], Carrier] | None = field(default=None, repr=False)
    _independent_availability: _IndependentJobAvailability | None = field(default=None, repr=False)
    _new_helper_delivery: Callable[[HelperClosureExpectation], HelperClosureDelivery] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_account, str) or not self.delivery_account or "\0" in self.delivery_account:
            raise ValidationError("Native execution binding requires a literal delivery account")
        if type(self.runtime_selection) is not RuntimeSelection:
            raise ValidationError("Native execution binding requires an explicit runtime selection")
        if (
            self._independent_availability is not None
            and type(self._independent_availability) is not _IndependentJobAvailability
        ):
            raise ValidationError("Native independent availability requires a core-selected fact")
