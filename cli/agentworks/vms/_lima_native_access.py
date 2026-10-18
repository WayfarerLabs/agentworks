"""Owned local Lima selection, startup, and managed guest preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution._lima_activation import LimaActivation
from agentworks.execution._lima_binding import lima_native_binding
from agentworks.execution._lima_selection import LimaLocalSelection
from agentworks.vms.target_preparation import VMTargetPreparationControlFact, prepare_managed_vm_target_from_platform

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import VMRow
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._lima_selection import LimaSelection
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.vms.target_preparation import VMTargetPreparation


@dataclass(slots=True)
class LimaOwnedNativePlatformAccess:
    """Passive construction; the core retains this before its first observation."""

    vm: VMRow
    platform: LimaPlatform
    ctx: RunContext
    owner: OperationOwner
    custody: LocalDeliveryCustody
    binding: NativeExecutionBinding | None = field(default=None, init=False)
    preparation: VMTargetPreparation | None = field(default=None, init=False)
    route: LimaLocalSelection | None = field(default=None, init=False, repr=False)
    selected: LimaSelection | None = field(default=None, init=False, repr=False)
    activation: LimaActivation | None = field(default=None, init=False, repr=False)
    _used: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self.selected is not None else None

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
        return self.route.observe(deadline, self.custody)

    def observe_power(self, deadline: Deadline) -> VMStatus:
        if self.selected is not None:
            raise StateError("Lima original power observation is single use")
        self.selected = self._observe(deadline)
        return self.selected.power

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        current = self._observe(deadline)
        if self.selected is None:
            raise StateError("Lima native selection is unavailable")
        self.selected.require_same_route(current)
        return current.locator

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        if self._used or self._closed:
            raise StateError("Lima native access preparation is single use")
        self._used = True
        selected, route = self.selected, self.route
        if selected is None or route is None or power is not selected.power:
            raise StateError("Lima preparation requires the original selected power")
        current = self._observe(deadline)
        selected.require_same_route(current)
        if current.power is not power or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
            raise StateError("Lima selected power is unavailable or changed")
        if power is VMStatus.STOPPED:
            self.activation = LimaActivation(self.owner, selected, route, self.custody)
            self.activation.start(deadline)
        current = self._observe(deadline)
        selected.require_same_route(current)
        if current.power is not VMStatus.RUNNING:
            raise StateError("Lima native preparation requires fresh running readiness")
        if self.activation is not None:
            self.activation.confirm_running(current, deadline)
        self.binding = lima_native_binding(selected)
        try:
            self.preparation = prepare_managed_vm_target_from_platform(
                self.vm,
                self.platform,
                selected.locator,
                self.binding,
                deadline=deadline,
                owner=self.owner,
                observe_locator=self.observe_locator,
            )
        except BaseException as control:
            if isinstance(control.__cause__, VMTargetPreparationControlFact):
                self.preparation = control.__cause__.preparation
            raise

    def _check_route(self, deadline: Deadline) -> None:
        current = self._observe(deadline)
        assert self.selected is not None
        self.selected.require_same_route(current)
        if current.power is not VMStatus.RUNNING:
            raise StateError("Lima selected native route is no longer running")

    def settle(self, deadline: Deadline) -> bool:
        """Retire local custody and exact bookkeeping without stopping the VM."""
        provider_locator_remaining(deadline, vm_name=self.vm.name)
        self._closed = True
        if not self.custody.close(deadline):
            return False
        try:
            return self.activation is None or self.activation.reconcile(deadline)
        finally:
            if self.route is not None:
                self.route.close()
