"""Owned WSL access with exact hold evidence and selected-route custody."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence, HostClientStatus
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorObservation
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
    from agentworks.db import VMRow, VMStatus
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner


@dataclass
class WSL2OwnedNativePlatformAccess:
    """Passive construction, followed by one retained route and hold attempt."""

    vm: VMRow
    platform: WSL2Platform
    ctx: RunContext
    owner: OperationOwner
    custody: LocalDeliveryCustody
    selected: WSL2OwnedOperation | None = None
    native_configuration_check: Callable[[], None] | None = None
    _used: bool = field(default=False, init=False, repr=False)
    _available: bool = field(default=False, init=False, repr=False)
    _route_used: bool = field(default=False, init=False, repr=False)

    @property
    def binding(self) -> NativeExecutionBinding | None:
        return self.selected.binding if self.selected is not None else None

    @property
    def guest_check(self) -> Callable[[VMGuestIdentity], None] | None:
        return self.selected.require_ready_guest if self.selected is not None else None

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self.selected.require_selected_route if self.selected is not None else None

    def observe_power(self, deadline: Deadline) -> VMStatus:
        return self.platform.observe_execution_power(self.vm, self.ctx, deadline=deadline, custody=self.custody)

    def ensure_available(self, power: VMStatus, deadline: Deadline) -> ProviderLocator:
        if self._used:
            raise StateError("Owned WSL native access preparation is single use")
        self._used = True
        selected = WSL2OwnedOperation.from_platform(
            self.vm,
            self.platform,
            self.ctx,
            owner=self.owner,
            deadline=deadline,
            config=self.ctx.config,
            provider_custody=self.custody,
        )
        if selected is None:
            raise StateError("Native VM route is unavailable", entity_kind="vm", entity_name=self.vm.name)
        # Retain the actual control object before its first hold effect.
        self.selected = selected
        if not selected.start_available(deadline):
            raise StateError(
                "Native VM target preparation did not establish an exact guest",
                entity_kind="vm",
                entity_name=self.vm.name,
            )
        self._available = True
        return selected._selected_locator

    def prepare_native_route(self, deadline: Deadline) -> NativeExecutionBinding:
        selected = self.selected
        if not self._available or self._route_used or selected is None or deadline.expired:
            raise StateError("Native WSL route is unavailable")
        self._route_used = True
        return selected.binding

    def observe_locator(self, deadline: Deadline) -> ProviderLocatorObservation:
        if self.selected is None:
            raise StateError("Native WSL selection is unavailable")
        return self.selected.observe_locator(deadline)

    def settle(self, deadline: Deadline) -> bool:
        selected = self.selected
        if selected is None:
            return True
        hold = selected.hold
        if hold.registration_uncertain:
            return False
        obligation = hold.obligation
        held_row = obligation._persisted_obligation if obligation is not None else None  # noqa: SLF001
        if any(row != held_row for row in self.owner.list_pending_lifecycle_obligations()):
            return False
        if hold.payload is None:
            evidence = hold.evidence
        else:
            try:
                evidence = hold.release(deadline)
            except Exception:
                return False
        if selected.ready is not None and evidence.identity != selected.ready.identity:
            return False
        if not evidence.local.settled:
            return False
        if evidence.local.host_client_status is HostClientStatus.NOT_CREATED:
            return True
        return evidence.identity is not None and evidence.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED
