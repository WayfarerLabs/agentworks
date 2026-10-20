"""Private platform custody retained across selected execution routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorObservation
    from agentworks.db import VMStatus
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding, _IndependentJobAvailability
    from agentworks.execution.carrier import Deadline


class OwnedVMPlatformAccess(Protocol):
    """Share retained availability and cleanup without acquiring or closing an owner."""

    @property
    def binding(self) -> NativeExecutionBinding | None: ...

    @property
    def independent_availability(self) -> _IndependentJobAvailability | None: ...

    @property
    def guest_check(self) -> Callable[[VMGuestIdentity], None] | None: ...

    @property
    def route_check(self) -> Callable[[Deadline], None] | None: ...

    def observe_power(self, deadline: Deadline) -> VMStatus: ...

    def observe_locator(self, deadline: Deadline) -> ProviderLocatorObservation: ...

    def settle(self, deadline: Deadline) -> bool: ...


class OwnedNativePlatformAccess(OwnedVMPlatformAccess, Protocol):
    """Prepare an existing VM without finalizing its operation owner."""

    @property
    def native_configuration_check(self) -> Callable[[], None] | None: ...

    def ensure_available(self, power: VMStatus, deadline: Deadline) -> ProviderLocator: ...

    def prepare_native_route(self, deadline: Deadline) -> NativeExecutionBinding: ...
