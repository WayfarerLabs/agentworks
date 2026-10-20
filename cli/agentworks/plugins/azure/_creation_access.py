"""Owned Azure creation, public SSH admission and immutable resource transfer."""

from __future__ import annotations

import time
from contextlib import contextmanager
from ipaddress import IPv4Address
from threading import TIMEOUT_MAX, RLock
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
from agentworks.db import OperationResourceKind, VMRow, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.azure._creation_request import AzureCreationRequests, remaining
from agentworks.plugins.azure._creation_resources import planned_disk_id, resource_plan
from agentworks.plugins.azure._identity import canonical_vm_id
from agentworks.plugins.azure._native_route import AzureNativeRoute
from agentworks.plugins.azure._owned_auth import _OwnedAzureCredential
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms._creation_ssh import CLOUD_INIT_CHECK
from agentworks.vms._creation_trust import CreationSSHTrust
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_execution_binding, vm_ssh_connection
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.plugins.azure.platform import AzureVMPlatform
    from agentworks.vms._creation_ssh import SSHReadiness


class AzureOwnedCreationAccess:
    """Passive factory result retained by core before credential or provider work.

    Auxiliary paths are unpredictable and checked absent before each PUT. ARM
    network APIs provide no documented atomic create-only precondition; these
    checks do not contain a concurrent external creator using the same path.
    """

    def __init__(
        self,
        request: ProvisionRequest,
        platform: AzureVMPlatform,
        ctx: RunContext,
        *,
        owner: OperationOwner,
        custody: LocalDeliveryCustody,
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != request.vm_name
        ):
            raise ValidationError("Azure creation requires exact VM ownership")
        self._vm = VMRow(
            name=request.vm_name,
            site=platform.site_name,
            template=None,
            admin_template=None,
            extra_packages=[],
            provisioning_status="in-progress",
            init_status="pending",
            tailscale_host=None,
            cpus=request.cpus,
            memory_gib=request.memory_gib,
            disk_gib=request.disk_gib,
            swap_gib=request.swap_gib,
            admin_username=request.admin_username,
            hostname=request.hostname,
            created_at="",
            last_seen_at=None,
            instance_marker=request.instance_marker,
        )
        self._settings = require_ssh_settings(self._vm, ctx, ctx.config)
        self._marker = request.instance_marker
        plan = resource_plan(request, platform.config)
        self._requests = AzureCreationRequests(owner, platform.config.subscription_id, plan, planned_disk_id(plan))
        self._auth = _OwnedAzureCredential(platform.config.auth, ctx, platform.site_name, request.vm_name)
        self._owner, self._custody, self._ctx = owner, custody, ctx
        self._lock = RLock()
        self._used = self._closed = self._rollback = self._settled = False
        self._metadata: dict[str, str] = {}
        self._result: VMCreationResult | None = None
        self._binding: NativeExecutionBinding | None = None
        self._endpoint: str | None = None
        self._route: AzureNativeRoute | None = None
        self._trust: CreationSSHTrust | None = None
        self._readiness: SSHReadiness | None = None
        self._exposure = OwnedSSHExposure(request.vm_name)
        self._route_ready = False

    @property
    def binding(self) -> NativeExecutionBinding | None:
        return self._binding

    @property
    def independent_availability(self) -> _IndependentJobAvailability:
        return _IndependentJobAvailability.NO_IDLE_STOP

    @property
    def guest_check(self) -> None:
        return None

    @property
    def platform_metadata(self) -> Mapping[str, str]:
        keys = ("public_ip_resource_id", "nsg_resource_id", "vnet_resource_id", "nic_resource_id", "resource_id")
        for key, item in zip(keys, self._requests.resources, strict=True):
            if item.create_ack is not None and item.create_ack.status_code == 201:
                self._metadata[key] = item.resource_id
        vm = self._requests.resources[-1]
        if vm.identity is not None and vm.create_ack is not None and vm.create_ack.status_code == 201:
            self._metadata["vm_id"] = vm.identity
        if self._requests.observed_disk_id is not None:
            self._metadata["os_disk_resource_id"] = self._requests.disk_id
        return MappingProxyType(dict(self._metadata))

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self._route_ready and not self._closed else None

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        if not self._lock.acquire(timeout=min(remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Azure creation transition deadline expired")
        try:
            remaining(deadline)
            yield
        finally:
            self._lock.release()

    def _credential(self, deadline: Deadline) -> object:
        return self._auth.credential(deadline)

    def _read(self, resource_id: str, deadline: Deadline, *, expand: bool = False) -> dict[str, Any] | None:
        self._owner.list_pending_lifecycle_obligations()
        remaining(deadline)
        with self._requests.custody(deadline):
            result = self._requests.get(self._credential(deadline), resource_id, deadline, expand=expand)
        if self._requests.cleanup_incomplete:
            raise StateError("Azure creation read original remains unsettled")
        remaining(deadline)
        return result

    def _current_vm(self, deadline: Deadline) -> dict[str, Any]:
        if not self._metadata:
            raise StateError("Azure creation has no acknowledged VM identity")
        value = self._read(self._metadata["resource_id"], deadline, expand=True)
        if value is None:
            raise StateError("Azure created VM is absent")
        self._requests.verify(self._requests.resources[-1], value)
        return value

    @staticmethod
    def _power(value: dict[str, Any]) -> VMStatus:
        statuses = value.get("properties", {}).get("instanceView", {}).get("statuses", [])
        codes = [
            item.get("code")
            for item in statuses
            if type(item) is dict and str(item.get("code")).startswith("PowerState/")
        ]
        return VMStatus.RUNNING if codes == ["PowerState/running"] else VMStatus.UNKNOWN

    @staticmethod
    def _route_model(value: dict[str, Any]) -> object:
        props = value["properties"]
        refs = props.get("networkProfile", {}).get("networkInterfaces", [])
        return SimpleNamespace(
            id=value["id"],
            vm_id=canonical_vm_id(props.get("vmId")),
            network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=ref.get("id")) for ref in refs]),
        )

    def _assert_open(self) -> None:
        if self._closed or self._requests.cleanup_incomplete:
            raise StateError("Azure creation cannot admit work")

    def observe_power(self, deadline: Deadline) -> VMStatus:
        with self._transition(deadline):
            self._assert_open()
            return self._power(self._current_vm(deadline))

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        with self._transition(deadline):
            self._assert_open()
            if self._power(self._current_vm(deadline)) is not VMStatus.RUNNING:
                raise StateError("Azure created VM is not running")
            return self._locator()

    def _locator(self) -> ProviderLocator:
        return ProviderLocator(f"azure-vm:v2:{self._metadata['vm_id']}:{self._metadata['resource_id']}")

    def _check_route(self, deadline: Deadline) -> None:
        with self._transition(deadline):
            self._assert_open()
            value = self._current_vm(deadline)
            if self._power(value) is not VMStatus.RUNNING or self._route is None:
                raise StateError("Azure created VM route is unavailable")
            ip = self._read(self._requests.resources[0].resource_id, deadline)
            if ip is None:
                raise StateError("Azure created public IP is absent")
            self._requests.verify(self._requests.resources[0], ip)
            if ip.get("properties", {}).get("ipAddress") != self._endpoint:
                raise StateError("Azure creation public endpoint changed")
            nic = self._read(self._requests.resources[3].resource_id, deadline)
            if nic is None:
                raise StateError("Azure created network interface is absent")
            self._requests.verify(self._requests.resources[3], nic)
            links = nic.get("properties", {}).get("ipConfigurations", [])
            if (
                len(links) != 1
                or links[0].get("properties", {}).get("publicIPAddress", {}).get("id")
                != self._requests.resources[0].resource_id
            ):
                raise StateError("Azure created NIC public IP association changed")
            self._route.check(deadline, observed_vm=self._route_model(value))
            remaining(deadline)

    def provision(self, deadline: Deadline) -> VMCreationResult:
        from agentworks.vms._creation_ssh import SSHReadiness, observe_early_guest

        with self._transition(deadline):
            self._assert_open()
            if self._used:
                raise StateError("Azure creation is single use")
            self._used = True
            with self._owner._guard:  # noqa: SLF001
                self._owner._reconcile_transition_locked()  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
                self._owner._require_no_active_work_locked()  # noqa: SLF001
                if self._owner._obligations_sealed or self._owner._recovery_owner:  # noqa: SLF001
                    raise StateError("Azure creation requires an unsealed ordinary owner")
            self._owner.list_pending_lifecycle_obligations()
            # Existing VM and planned disk collisions refuse before auxiliaries.
            for resource_id in (self._requests.plan[-1].resource_id, self._requests.disk_id):
                if self._read(resource_id, deadline) is not None:
                    raise StateError("Azure creation refuses an existing resource")
            prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
            self._requests.register(deadline)
            with self._requests.custody(deadline):
                for index in range(len(self._requests.plan)):
                    self._requests.create(index, self._credential(deadline), deadline)
                    if index == len(self._requests.plan) - 1:
                        self._metadata = dict(self.platform_metadata)
                        self._vm.platform_metadata = dict(self._metadata)
            while True:
                current = self._current_vm(deadline)
                if self._power(current) is VMStatus.RUNNING:
                    break
                time.sleep(min(0.25, remaining(deadline)))
            ip = self._read(self._requests.resources[0].resource_id, deadline)
            if ip is None:
                raise StateError("Azure created public IP is unavailable")
            self._requests.verify(self._requests.resources[0], ip)
            try:
                self._endpoint = str(IPv4Address(ip["properties"]["ipAddress"]))
            except (KeyError, ValueError, TypeError):
                raise StateError("Azure creation requires one literal public IPv4 address") from None
            self._route = AzureNativeRoute(
                self._owner,
                self._vm.name,
                self._credential(deadline),
                self._metadata["resource_id"],
                self._metadata["vm_id"],
                self._locator(),
                self._route_model(current),
                prefixes,
            )
            self._route.open(deadline)
            self._check_route(deadline)
            self._readiness = SSHReadiness()
            self._readiness.wait(self._endpoint, deadline)
            self._check_route(deadline)
            self._trust = CreationSSHTrust(
                self._owner,
                vm_name=self._vm.name,
                admin_username=self._vm.admin_username,
                instance_marker=self._marker,
                endpoint=self._endpoint,
                settings=self._settings,
                deadline=deadline,
            )
            self._trust.prepare()
            self._check_route(deadline)
            observe_early_guest(
                vm_ssh_connection(self._vm.name, self._vm.admin_username, self._endpoint, self._settings),
                self._marker,
                deadline,
                self._custody,
                bootstrap_check=CLOUD_INIT_CHECK,
            )
            self._check_route(deadline)
            self._binding = ssh_execution_binding(
                self._vm,
                self._endpoint,
                self._settings,
                independent_availability=self.independent_availability,
            )
            self._route_ready = True
            self._result = VMCreationResult(self._metadata, self._binding, self._locator())
            return self._result

    def complete_bootstrap(self, deadline: Deadline) -> None:
        # Core may transfer known completion under a fresh cleanup budget.
        with self._transition(deadline):
            if self._rollback or self._result is None or self._closed:
                raise StateError("Azure bootstrap resource transfer is unproved")
            self._owner.list_pending_lifecycle_obligations()
            # Acceptance is immutable even if durable publication loses its reply.
            self._requests.accepted = True
            self._requests.reconcile(deadline)

    def request_rollback(self) -> None:
        with self._lock:
            if self._requests.accepted:
                raise StateError("Azure bootstrap rollback window has closed")
            self._rollback = True

    def settle(self, deadline: Deadline) -> bool:
        with self._transition(deadline):
            self._closed = True
            if self._settled:
                return True
            settled = True
            ordinary: Exception | None = None
            control: BaseException | None = None
            actions: list[Callable[[], bool]] = [
                lambda: self._requests.close(deadline),
                lambda: self._exposure.close(deadline),
            ]
            if self._readiness is not None:
                actions.append(self._readiness.close)
            trust, route = self._trust, self._route
            if trust is not None:
                actions.append(lambda: trust.close(deadline))
            if route is not None:
                actions.extend((lambda: route.close(deadline), lambda: route.settle(deadline)))
            for action in actions:
                try:
                    settled = action() and settled
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
            if not settled:
                return False
            resource_settled = self._requests.accepted or not any(item.sent for item in self._requests.resources)
            if self._rollback and not self._requests.accepted and not resource_settled:
                self._owner.list_pending_lifecycle_obligations()
                with self._requests.custody(deadline):
                    resource_settled = self._requests.rollback(self._credential(deadline), deadline)
            if not self._requests.close(deadline):
                return False
            reconciled = self._requests.reconcile(deadline, resolve=resource_settled)
            if resource_settled and reconciled:
                self._settled = self._auth.close()
                remaining(deadline)
            return self._settled
