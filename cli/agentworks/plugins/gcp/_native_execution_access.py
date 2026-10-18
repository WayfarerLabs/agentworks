"""Private owned GCE preparation and aggregate effect settlement."""

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
from agentworks.plugins.gcp._activation import ActivationState, GCEActivation
from agentworks.plugins.gcp._native_access import GCEOwnedRead
from agentworks.plugins.gcp._native_identity import NativeIdentity
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_native_binding
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from agentworks.vms.target_preparation import (
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocator
    from agentworks.db import VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp._native_access import GCEInstanceObservation
    from agentworks.plugins.gcp._native_route import GCENativeRoute
    from agentworks.plugins.gcp._owned_auth import _OwnedAuth
    from agentworks.plugins.gcp.platform import GCEPlatform
    from agentworks.vms.target_preparation import VMTargetPreparation


class GCEOwnedAccess:
    """Retain reads, start, route and partial guest facts under one VM claim.

    Construction is passive. The caller owns the outer operation and local
    delivery lifetime. The finite reentrant transition permits the managed
    guest preparation's same-thread fresh locator callback.
    """

    def __init__(self, vm: VMRow, platform: GCEPlatform, ctx: RunContext, *, owner: OperationOwner) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm.name
            or platform.site_name != vm.site
        ):
            raise ValidationError("GCE native access requires exact selected VM ownership")
        self._vm = vm
        self._platform = platform
        self._ctx = ctx
        self._owner = owner
        self._identity = NativeIdentity.from_row(vm)
        self._reader = GCEOwnedRead(vm, platform, ctx)
        self._auth: _OwnedAuth | None = None
        self._lock = RLock()
        self._used = False
        self._closed = False
        self._settled = False
        self._reader_settled = False
        self._exposure_settled = False
        self._start_settled = False
        self._route_settled = False
        self._start: GCEActivation | None = None
        self._route: GCENativeRoute | None = None
        self._exposure: OwnedSSHExposure | None = None
        self._endpoint: str | None = None
        self.binding: NativeExecutionBinding | None = None
        self.preparation: VMTargetPreparation | None = None

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise StateError("GCE native access transition exceeded its deadline")
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
            raise StateError("GCE native access cannot admit work")

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

    def _fresh_instance(self, deadline: Deadline, power: VMStatus) -> GCEInstanceObservation:
        current = self._reader.observe_instance(deadline, identity=self._identity)
        self._require_open()
        if current.power is not power:
            raise StateError("GCE native preparation power changed or is unavailable")
        return current

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        """Fresh exact running-incarnation callback for managed guest admission."""
        with self._transition(deadline):
            self._require_open()
            self._fresh_instance(deadline, VMStatus.RUNNING)
            return self._identity.locator

    def _pause(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        time.sleep(min(0.25, remaining))
        provider_locator_remaining(deadline, vm_name=self._vm.name)

    def _await_running(self, deadline: Deadline, stopped: str | None) -> GCEInstanceObservation:
        """Request success still needs fresh running power and allocated NAT."""
        running = False
        while True:
            current = self._reader.observe_instance(deadline, identity=self._identity)
            self._require_open()
            if current.power is VMStatus.RUNNING:
                running = True
                if current.endpoint is not None:
                    return current
            elif running or current.raw_power not in {"PROVISIONING", "STAGING", stopped}:
                raise StateError("GCE startup power observation is unknown or invalid")
            self._pause(deadline)

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        """Prepare once, retaining each effect before its admission boundary."""
        from agentworks.plugins.gcp._native_route import GCENativeRoute

        with self._transition(deadline):
            self._require_open()
            if self._used or type(power) is not VMStatus or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
                raise StateError("GCE native preparation cannot be admitted")
            self._used = True
            settings = require_ssh_settings(self._vm, self._ctx, self._ctx.config)
            current = self._fresh_instance(deadline, power)
            self._auth = self._reader.borrow_auth(deadline)
            if power is VMStatus.STOPPED:
                identity = self._identity
                self._start = GCEActivation(
                    self._owner,
                    self._vm.name,
                    self._auth,
                    identity.project_id,
                    identity.zone,
                    identity.instance_name,
                    identity.instance_id,
                    identity.locator,
                )
                self._start.start(deadline)
                while self._start.state is ActivationState.PENDING:
                    self._require_open()
                    self._start.observe(deadline)
                    if self._start.state is ActivationState.PENDING:
                        self._pause(deadline)
                self._require_open()
                if self._start.state is not ActivationState.SUCCEEDED:
                    raise StateError("GCE start did not establish request success")
                current = self._await_running(deadline, current.raw_power)
            self._endpoint = current.endpoint
            self.binding = ssh_native_binding(self._vm, self._endpoint, settings)
            self._exposure = OwnedSSHExposure(self._vm.name)
            prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()
            # Discovery can outlive a provider-side tag or account change.
            current = self._fresh_instance(deadline, VMStatus.RUNNING)
            if current.endpoint != self._endpoint:
                raise StateError("GCE native route endpoint changed or is unavailable")
            self._route = GCENativeRoute(self._owner, self._vm.name, self._auth, self._identity, prefixes)
            self._route.open(deadline, observed_instance=current)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()
            self._check_route(deadline)
            try:
                self.preparation = prepare_managed_vm_target_from_platform(
                    self._vm,
                    self._platform,
                    self._identity.locator,
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
            current = self._fresh_instance(deadline, VMStatus.RUNNING)
            if current.endpoint != self._endpoint or self._route is None:
                raise StateError("GCE native route endpoint changed or is unavailable")
            self._route.check(deadline, observed_instance=current)
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._require_open()

    def _settle_start(self, deadline: Deadline) -> bool:
        start = self._start
        if start is None or self._start_settled:
            return True
        # settle() retries local originals and ledger work, never an Operation GET.
        # Keep pending monitors open until their explicit cleanup-budget observation.
        start.settle(deadline)
        if start.cleanup_incomplete:
            return False
        while start.state is ActivationState.PENDING:
            start.observe(deadline)
            if start.cleanup_incomplete:
                return False
            if start.state is ActivationState.PENDING:
                self._pause(deadline)
        failure: BaseException | None = None
        try:
            start.close(deadline)
        except BaseException as error:
            failure = error
        try:
            settled = start.settle(deadline)
        except BaseException as error:
            if failure is not None and (not isinstance(failure, Exception) or isinstance(error, Exception)):
                raise failure from None
            raise
        self._start_settled = settled and not start.cleanup_incomplete
        if failure is not None:
            raise failure
        return self._start_settled

    def _settle_route(self, deadline: Deadline) -> bool:
        route = self._route
        if route is None or self._route_settled:
            return True
        failure: BaseException | None = None
        try:
            route.close(deadline)
        except BaseException as error:
            failure = error
        try:
            settled = route.settle(deadline)
        except BaseException as error:
            if failure is not None and (not isinstance(failure, Exception) or isinstance(error, Exception)):
                raise failure from None
            raise
        # Explicit settlement may retire an original that close() could not.
        self._route_settled = settled and not route.cleanup_incomplete
        if failure is not None:
            raise failure
        return self._route_settled

    def settle(self, deadline: Deadline) -> bool:
        """Bar body work and keep shared auth until every dependent settles."""
        self._closed = True
        with self._transition(deadline):
            if self._settled:
                return True
            settled = True
            ordinary: Exception | None = None
            control: BaseException | None = None
            actions: list[tuple[str, Callable[[Deadline], bool]]] = []
            if not self._reader_settled:
                actions.append(("_reader_settled", self._reader.close_read_client))
            if self._exposure is not None and not self._exposure_settled:
                actions.append(("_exposure_settled", self._exposure.close))
            actions.extend((("_route_settled", self._settle_route), ("_start_settled", self._settle_start)))
            for attribute, action in actions:
                try:
                    if action(deadline):
                        setattr(self, attribute, True)
                    else:
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
            if self.preparation is not None and self.preparation.requires_owner_retention:
                settled = False
            if settled:
                self._settled = self._reader.close(deadline)
            return self._settled
