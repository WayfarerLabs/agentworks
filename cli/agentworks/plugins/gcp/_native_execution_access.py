"""Private owned GCE preparation and aggregate effect settlement."""

from __future__ import annotations

import time
from contextlib import contextmanager
from copy import copy
from threading import TIMEOUT_MAX, RLock
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
from agentworks.db import OperationResourceKind, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp._activation import ActivationState, GCEActivation
from agentworks.plugins.gcp._native_access import GCEOwnedRead
from agentworks.plugins.gcp._native_identity import NativeIdentity
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_native_binding
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocator
    from agentworks.db import VMRow
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh import SSHSettings
    from agentworks.plugins.gcp._native_access import GCEInstanceObservation
    from agentworks.plugins.gcp._native_route import GCENativeRoute
    from agentworks.plugins.gcp._owned_auth import _OwnedAuth
    from agentworks.plugins.gcp.platform import GCEPlatform


class GCEOwnedAccess:
    """Retain reads, start and native route under one VM claim.

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
        self._native_vm = copy(vm)
        self._native_vm.platform_metadata = dict(vm.platform_metadata)
        self._provider_identity = platform._locator_metadata(vm)
        self._identity: NativeIdentity | None = None
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
        self._available = False
        self._route_used = False
        self._route_ready = False
        self.binding: NativeExecutionBinding | None = None
        self.guest_check: Callable[[VMGuestIdentity], None] | None = None
        self._settings: SSHSettings | None = None

    @property
    def native_configuration_check(self) -> Callable[[], None]:
        return self._validate_native_configuration

    def _validate_native_configuration(self) -> None:
        if self._settings is None:
            self._settings = require_ssh_settings(self._native_vm, self._ctx, self._ctx.config)
        if self._identity is None:
            self._identity = NativeIdentity.from_row(self._native_vm)

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
        return self._check_route if not self._closed and self._route_ready else None

    def observe_power(self, deadline: Deadline) -> VMStatus:
        with self._transition(deadline):
            self._require_open()
            self._auth = self._reader.borrow_auth(deadline)
            power = self._reader.observe_power(deadline)
            self._require_open()
            return power

    def _fresh_instance(self, deadline: Deadline, power: VMStatus) -> GCEInstanceObservation:
        assert self._identity is not None
        current = self._reader.observe_instance(deadline, identity=self._identity)
        self._require_open()
        if current.power is not power:
            raise StateError("GCE native preparation power changed or is unavailable")
        return current

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        """Fresh exact running-incarnation callback for managed guest admission."""
        with self._transition(deadline):
            self._require_open()
            current = self._reader.observe_availability(deadline)
            self._require_open()
            if current.power is not VMStatus.RUNNING:
                raise StateError("GCE running provider locator is unavailable")
            return current.locator

    def _pause(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        time.sleep(min(0.25, remaining))
        provider_locator_remaining(deadline, vm_name=self._vm.name)

    def _await_running(self, deadline: Deadline, stopped: str | None) -> GCEInstanceObservation:
        """Request success still needs fresh running provider power."""
        while True:
            current = self._reader.observe_availability(deadline)
            self._require_open()
            if current.power is VMStatus.RUNNING:
                return current
            elif current.raw_power not in {"PROVISIONING", "STAGING", stopped}:
                raise StateError("GCE startup power observation is unknown or invalid")
            self._pause(deadline)

    def ensure_available(self, power: VMStatus, deadline: Deadline) -> ProviderLocator:
        """Retain activation without selecting native ingress or guest delivery."""
        with self._transition(deadline):
            self._require_open()
            if self._used or type(power) is not VMStatus or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
                raise StateError("GCE native preparation cannot be admitted")
            self._used = True
            self._auth = self._reader.borrow_auth(deadline)
            current = self._reader.observe_availability(deadline)
            self._require_open()
            if current.power is not power:
                raise StateError("GCE availability power changed or is unavailable")
            if power is VMStatus.STOPPED:
                project, zone, name, incarnation = self._provider_identity
                self._start = GCEActivation(
                    self._owner,
                    self._vm.name,
                    self._auth,
                    project,
                    zone,
                    name,
                    incarnation,
                    current.locator,
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
            self._available = True
            return current.locator

    def prepare_native_route(self, deadline: Deadline) -> NativeExecutionBinding:
        """Select native delivery under the retained available VM selection."""
        from agentworks.plugins.gcp._native_route import GCENativeRoute

        with self._transition(deadline):
            self._require_open()
            if not self._available or self._route_used:
                raise StateError("GCE native route cannot be admitted")
            self._route_used = True
            self._validate_native_configuration()
            settings = self._settings
            assert settings is not None and self._identity is not None and self._auth is not None
            current = self._fresh_instance(deadline, VMStatus.RUNNING)
            if self._start is not None:
                while current.endpoint is None:
                    self._pause(deadline)
                    current = self._fresh_instance(deadline, VMStatus.RUNNING)
            self._endpoint = current.endpoint
            self.binding = ssh_native_binding(
                self._vm, self._endpoint, settings, independent_availability=_IndependentJobAvailability.NO_IDLE_STOP
            )
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
            self._route_ready = True
            return self.binding

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
            actions: list[tuple[str | None, Callable[[Deadline], bool]]] = []
            if not self._reader_settled:
                actions.append(("_reader_settled", self._reader.close_read_client))
            if self._exposure is not None and not self._exposure_settled:
                actions.append(("_exposure_settled", self._exposure.close))
            if self._auth is not None and not self._auth.closed:
                # Dependent provider cleanup may create fresh credential debt.
                actions.append((None, self._auth.retry_cleanup))
            actions.extend((("_route_settled", self._settle_route), ("_start_settled", self._settle_start)))
            for attribute, action in actions:
                try:
                    if action(deadline):
                        if attribute is not None:
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
            if settled:
                self._settled = self._reader.close(deadline)
            return self._settled
