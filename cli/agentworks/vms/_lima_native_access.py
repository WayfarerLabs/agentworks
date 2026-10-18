"""Owned local Lima availability and native route preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution._lima_activation import LimaActivation
from agentworks.execution._lima_binding import lima_independent_availability, lima_native_binding
from agentworks.execution._lima_selection import LimaLocalSelection
from agentworks.execution._lima_wsl_hold import LimaWSLHold

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import VMRow
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._lima_selection import LimaNativeSelection, LimaSelection
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding, _IndependentJobAvailability
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner


@dataclass(slots=True)
class LimaOwnedNativePlatformAccess:
    """Passive construction; the core retains this before its first observation."""

    vm: VMRow
    platform: LimaPlatform
    ctx: RunContext
    owner: OperationOwner
    custody: LocalDeliveryCustody
    binding: NativeExecutionBinding | None = field(default=None, init=False)
    route: LimaLocalSelection | None = field(default=None, init=False, repr=False)
    selected: LimaSelection | None = field(default=None, init=False, repr=False)
    _native_selected: LimaNativeSelection | None = field(default=None, init=False, repr=False)
    activation: LimaActivation | None = field(default=None, init=False, repr=False)
    wsl_hold: LimaWSLHold | None = field(default=None, init=False, repr=False)
    _wsl_release_confirmed: bool = field(default=False, init=False, repr=False)
    _used: bool = field(default=False, init=False, repr=False)
    _available_selection: LimaSelection | None = field(default=None, init=False, repr=False)
    _native_used: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self.binding is not None else None

    @property
    def independent_availability(self) -> _IndependentJobAvailability | None:
        """Return the selected driver's fact without constructing a native route."""
        selected = self._available_selection
        return None if selected is None or self._closed else lima_independent_availability(selected.vm_type)

    @property
    def native_configuration_check(self) -> Callable[[], None]:
        return self._validate_native_configuration

    @property
    def guest_check(self) -> Callable[[VMGuestIdentity], None] | None:
        return None if self.wsl_hold is None else self.wsl_hold.require_guest

    def _validate_native_configuration(self) -> None:
        self.platform._require_local_native()  # noqa: SLF001
        self.platform._instance_name(self.vm)  # noqa: SLF001

    def _observe(self, deadline: Deadline) -> LimaSelection:
        if self._closed:
            raise StateError("Lima native access is closed")
        if self.route is None:
            self.platform._require_local_native()  # noqa: SLF001
            self.route = LimaLocalSelection.resolve(
                self.platform.site_name,
                self.platform._instance_name(self.vm),
                self.vm.name,
                deadline,  # noqa: SLF001
            )
        try:
            current = self.route.observe(deadline, self.custody)
        except BaseException:
            self._available_selection = None
            raise
        available = self._available_selection
        if available is not None:
            self._available_selection = (
                current if current.power is VMStatus.RUNNING and current.locator == available.locator else None
            )
            if self.wsl_hold is not None:
                self.wsl_hold.require_ready()
        return current

    def observe_power(self, deadline: Deadline) -> VMStatus:
        selected = self._available_selection or self.selected
        current = self._observe(deadline)
        if self.selected is None:
            self.selected = current
        elif selected is not None and current.locator != selected.locator:
            raise StateError("Lima selected provider locator changed")
        return current.power

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        selected = self._available_selection or self.selected
        current = self._observe(deadline)
        if selected is None:
            raise StateError("Lima native selection is unavailable")
        if current.locator != selected.locator:
            raise StateError("Lima selected provider locator changed")
        return current.locator

    def ensure_available(self, power: VMStatus, deadline: Deadline) -> ProviderLocator:
        if self._used or self._closed:
            raise StateError("Lima native access preparation is single use")
        self._used = True
        selected, route = self.selected, self.route
        if selected is None or route is None or power is not selected.power:
            raise StateError("Lima preparation requires the original selected power")
        current = self._observe(deadline)
        if current.locator != selected.locator:
            raise StateError("Lima selected provider locator changed")
        if current.power is not power or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
            raise StateError("Lima selected power is unavailable or changed")
        if power is VMStatus.STOPPED:
            self.activation = LimaActivation(self.owner, selected, route, self.custody)
            self.activation.start(deadline)
        current = self._observe(deadline)
        if current.power is not VMStatus.RUNNING:
            raise StateError("Lima native preparation requires fresh running readiness")
        if self.activation is not None:
            self.activation.confirm_running(current, deadline)
        else:
            selected.require_same_route(current)
        if current.vm_type == "wsl2":
            self.wsl_hold = LimaWSLHold(self.vm, current, self.owner)
            self.wsl_hold.start(deadline)
            confirmed = self._observe(deadline)
            current.require_same_route(confirmed)
            if confirmed.power is not VMStatus.RUNNING:
                raise StateError("Lima WSL hold requires fresh running readiness")
            self.wsl_hold.require_ready()
            current = confirmed
        self._available_selection = current
        return current.locator

    def prepare_native_route(self, deadline: Deadline) -> NativeExecutionBinding:
        if self._native_used or self._closed or self._available_selection is None:
            raise StateError("Lima native route requires available single-use access")
        self._native_used = True
        available = self._available_selection
        current = self._observe(deadline)
        if current.locator != available.locator or current.power is not VMStatus.RUNNING:
            raise StateError("Lima selected native route changed or is no longer running")
        if self.wsl_hold is not None:
            self.wsl_hold.require_ready()
        assert self.route is not None
        self._native_selected = self.route.resolve_native(current, deadline, self.custody)
        self.binding = lima_native_binding(self._native_selected)
        return self.binding

    def _check_route(self, deadline: Deadline) -> None:
        if self._native_selected is None or self.route is None:
            raise StateError("Lima native route is not selected")
        self.route.check_native(self._native_selected, deadline, self.custody)
        if self.wsl_hold is not None:
            self.wsl_hold.require_ready()

    def settle(self, deadline: Deadline) -> bool:
        """Retire local custody and exact bookkeeping without stopping the VM."""
        provider_locator_remaining(deadline, vm_name=self.vm.name)
        self._closed = True
        if not self.custody.close(deadline):
            return False
        if self.wsl_hold is not None:
            assert self.route is not None
            if self._wsl_release_confirmed:
                return self.route.close()
            try:
                if self.activation is not None and not self.activation.reconcile(deadline):
                    return False
                selected = self.wsl_hold.selected
                selected.require_same_route(self.route.observe(deadline, self.custody))
                if not self.wsl_hold.release(deadline):
                    return False
                selected.require_same_route(self.route.observe(deadline, self.custody))
            except Exception:
                return False
            self._wsl_release_confirmed = True
            return self.route.close()
        directory_settled = False
        try:
            settled = self.activation is None or self.activation.reconcile(deadline)
        finally:
            directory_settled = self.route is None or self.route.close()
        return settled and directory_settled
