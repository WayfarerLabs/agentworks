"""Private platform-owned access retained by the native composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import VMStatus
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.vms.target_preparation import VMTargetPreparation


class OwnedNativePlatformAccess(Protocol):
    """Retain partial platform progress without finalizing the operation owner."""

    @property
    def binding(self) -> NativeExecutionBinding | None: ...

    @property
    def preparation(self) -> VMTargetPreparation | None: ...

    @property
    def route_check(self) -> Callable[[Deadline], None] | None: ...

    def prepare(self, power: VMStatus, deadline: Deadline) -> None: ...

    def settle(self, deadline: Deadline) -> bool: ...
