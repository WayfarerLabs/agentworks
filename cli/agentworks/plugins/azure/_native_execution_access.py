"""Private owned Azure native preparation and aggregate effect settlement."""

from __future__ import annotations

import time
from contextlib import contextmanager
from threading import TIMEOUT_MAX, RLock
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
from agentworks.db import OperationResourceKind, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.operations import OperationOwner
from agentworks.plugins.azure._incarnation_start import AzureIncarnationStart
from agentworks.plugins.azure._incarnation_start_payload import TERMINAL_STATES, StartState
from agentworks.plugins.azure._native_access import AzureOwnedReadAccess
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_native_binding
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from agentworks.vms.target_preparation import (
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from azure.mgmt.compute.models import VirtualMachine

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocator
    from agentworks.db import VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.azure._native_route import AzureNativeRoute
    from agentworks.plugins.azure.platform import AzureVMPlatform
    from agentworks.vms.target_preparation import VMTargetPreparation


class AzureOwnedAccess:
    """Retain reads, start, route and partial guest facts under one VM owner.

    Construction is passive. The caller retains this object before work and
    owns outer operation and local delivery lifetime. One reentrant lock spans
    credential borrowing, including same-thread guest locator callbacks.
    """

    def __init__(self, vm: VMRow, platform: AzureVMPlatform, ctx: RunContext, *, owner: OperationOwner) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm.name
            or platform.site_name != vm.site
        ):
            raise ValidationError("Azure native access requires exact selected VM ownership")
        self._vm = vm
        self._platform = platform
        self._ctx = ctx
        self._owner = owner
        self._reader = AzureOwnedReadAccess(vm, platform, ctx)
        self._lock = RLock()
        self._used = False
        self._closed = False
        self._settled = False
        self._start_settled = False
        self._route_settled = False
        self._start: AzureIncarnationStart | None = None
        self._route: AzureNativeRoute | None = None
        self._exposure: OwnedSSHExposure | None = None
        self._endpoint: object = None
        self.binding: NativeExecutionBinding | None = None
        self.preparation: VMTargetPreparation | None = None

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise StateError("Azure native access transition exceeded its deadline")
        control: BaseException | None = None
        try:
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            yield
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise

    @property
    def cleanup_incomplete(self) -> bool:
        """Whether an actual retained local original still requires retirement."""
        return (
            self._reader.cleanup_incomplete
            or (self._start is not None and self._start.cleanup_incomplete)
            or (self._route is not None and self._route.cleanup_incomplete)
            or (self._exposure is not None and self._exposure.cleanup_incomplete)
        )

    def _require_open(self) -> None:
        if self._closed or self.cleanup_incomplete:
            raise StateError("Azure native access cannot admit work")

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        prepared = self.preparation
        return (
            self._check_route
            if not self._closed
            and self.binding is not None
            and prepared is not None
            and prepared.status is VMTargetPreparationStatus.PREPARED
            else None
        )

    def observe_power(self, deadline: Deadline) -> VMStatus:
        with self._transition(deadline):
            self._require_open()
            require_ssh_settings(self._vm, self._ctx, self._ctx.config)
            power = self._reader.observe_power(deadline)
            self._require_open()
            return power

    def _fresh_vm(self, deadline: Deadline, power: VMStatus) -> VirtualMachine:
        current = self._reader.observe_vm(deadline)
        self._require_open()
        if self._reader._power(current) is not power:
            raise StateError("Azure native preparation power changed or is unavailable")
        return current

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        """Fresh exact running-incarnation callback for managed guest admission."""
        with self._transition(deadline):
            self._require_open()
            self._fresh_vm(deadline, VMStatus.RUNNING)
            return self._reader.recorded_locator

    def _pause(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        time.sleep(min(0.25, remaining))
        provider_locator_remaining(deadline, vm_name=self._vm.name)

    def _await_running(self, deadline: Deadline) -> VirtualMachine:
        """After request success, observe only documented startup power states."""
        while True:
            current = self._reader.observe_vm(deadline)
            self._require_open()
            code = self._reader._power_code(current)
            if code == "PowerState/running":
                return current
            # Azure documents Starting and briefly Stopped during startup.
            # Deallocated does not establish progress after a successful start.
            if code not in {"PowerState/starting", "PowerState/stopped"}:
                raise StateError("Azure startup power observation is unknown or invalid")
            self._pause(deadline)

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        """Prepare once, retaining each effect before its admission boundary."""
        from agentworks.plugins.azure._native_route import AzureNativeRoute

        with self._transition(deadline):
            self._require_open()
            if (
                self._used
                or type(power) is not VMStatus
                or power not in (VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED)
            ):
                raise StateError("Azure native preparation cannot be admitted")
            self._used = True
            settings = require_ssh_settings(self._vm, self._ctx, self._ctx.config)
            self._fresh_vm(deadline, power)
            if power in {VMStatus.STOPPED, VMStatus.DEALLOCATED}:
                self._start = AzureIncarnationStart(
                    self._owner,
                    self._vm.name,
                    self._reader.borrow_credential(deadline),
                    self._reader.recorded_resource_id,
                    self._reader.recorded_vm_id,
                    self._reader.recorded_locator,
                )
                self._start.start(deadline)
                while self._start.state is StartState.PENDING:
                    self._require_open()
                    self._start.observe(deadline)
                    if self._start.state is StartState.PENDING:
                        self._pause(deadline)
                self._require_open()
                if self._start.state is not StartState.SUCCEEDED:
                    raise StateError("Azure start did not establish request success")
                current = self._await_running(deadline)
            else:
                current = self._fresh_vm(deadline, VMStatus.RUNNING)
            self._endpoint = self._reader.observe_public_endpoint(deadline)
            self._require_open()
            self.binding = ssh_native_binding(self._vm, self._endpoint, settings)
            self._exposure = OwnedSSHExposure(self._vm.name)
            prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()
            self._route = AzureNativeRoute(
                self._owner,
                self._vm.name,
                self._reader.borrow_credential(deadline),
                self._reader.recorded_resource_id,
                self._reader.recorded_vm_id,
                self._reader.recorded_locator,
                current,
                prefixes,
            )
            self._route.open(deadline)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()
            self._check_route(deadline)
            try:
                self.preparation = prepare_managed_vm_target_from_platform(
                    self._vm,
                    self._platform,
                    self._reader.recorded_locator,
                    self.binding,
                    deadline=deadline,
                    owner=self._owner,
                    observe_locator=self.observe_locator,
                )
            except BaseException as control:
                if isinstance(control.__cause__, VMTargetPreparationControlFact):
                    self.preparation = control.__cause__.preparation
                raise
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()

    def _check_route(self, deadline: Deadline) -> None:
        with self._transition(deadline):
            self._require_open()
            current = self._fresh_vm(deadline, VMStatus.RUNNING)
            endpoint = self._reader.observe_public_endpoint(deadline)
            self._require_open()
            if endpoint != self._endpoint or self._route is None:
                raise StateError("Azure native route endpoint changed or is unavailable")
            self._route.check(deadline, observed_vm=current)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()

    def _settle_start(self, deadline: Deadline) -> bool:
        start = self._start
        if start is None or self._start_settled:
            return True
        # Pending monitors remain observable until terminal evidence. close()
        # permanently bars observe(), so retire only local handles first.
        start.settle(deadline)
        if start.cleanup_incomplete:
            return False
        while start.state is StartState.PENDING:
            start.observe(deadline)
            if start.cleanup_incomplete:
                return False
            if start.state is StartState.PENDING:
                self._pause(deadline)
        if start.state not in (*TERMINAL_STATES, StartState.UNKNOWN):
            raise StateError("Azure start settlement state is invalid")
        closed = start.close(deadline)
        settled = start.settle(deadline)
        self._start_settled = closed and settled and not start.cleanup_incomplete
        return self._start_settled

    def _settle_route(self, deadline: Deadline) -> bool:
        route = self._route
        if route is None or self._route_settled:
            return True
        closed = route.close(deadline)
        settled = route.settle(deadline)
        self._route_settled = closed and settled and not route.cleanup_incomplete
        return self._route_settled

    def settle(self, deadline: Deadline) -> bool:
        """Terminal cleanup keeps credentials until all dependent custody settles."""
        with self._transition(deadline):
            if self._settled:
                return True
            self._closed = True
            settled = True
            ordinary: Exception | None = None
            control: BaseException | None = None
            actions = [self._reader.close_read_client]
            if self._exposure is not None:
                actions.append(self._exposure.close)
            actions.extend((self._settle_route, self._settle_start))
            for action in actions:
                try:
                    if not action(deadline):
                        settled = False
                except Exception as error:
                    settled = False
                    if ordinary is None:
                        ordinary = error
                except BaseException as error:
                    settled = False
                    if control is None:
                        control = error
            if control is not None:
                raise control
            if ordinary is not None:
                raise ordinary
            preparation = self.preparation
            if preparation is not None and preparation.requires_owner_retention:
                settled = False
            if settled:
                self._settled = self._reader.close(deadline)
            return self._settled
