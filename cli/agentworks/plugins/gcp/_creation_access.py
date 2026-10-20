"""Independent GCE creation, public prerequisites and retained resource custody."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace
from threading import TIMEOUT_MAX, RLock
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
from agentworks.db import OperationResourceKind, VMStatus
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute, _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp._creation_api import CreationAPI, remaining
from agentworks.plugins.gcp._creation_network import check_policy
from agentworks.plugins.gcp._creation_preflight import CreationSelection, select
from agentworks.plugins.gcp._creation_resources import CreationResources, Resource
from agentworks.plugins.gcp._creation_shapes import (
    ACCESS_CONFIG,
    STARTUP_MARKER,
    firewall_body,
    instance_body,
    instance_endpoint,
)
from agentworks.plugins.gcp.names import derive_names
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms._creation_ssh import SSHReadiness, observe_early_guest
from agentworks.vms._creation_trust import CreationSSHTrust
from agentworks.vms._ssh_native_binding import vm_ssh_connection
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.plugins.gcp.platform import GCEPlatform


class GCEOwnedCreationAccess:
    """One provider creation under the caller's original exact VM owner.

    Construction is passive. The resource obligation retains every mutation
    intent/receipt and authorizes rollback after ordinary admission closes.
    Unknown requests never replay; accepted resources never select rollback.
    """

    def __init__(
        self,
        request: ProvisionRequest,
        platform: GCEPlatform,
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
            raise ValidationError("GCE creation requires exact VM ownership")
        settings = ctx.config.operator.ssh if ctx.config is not None else None
        if settings is None:
            raise ConfigError("GCE creation requires explicit operator SSH settings")
        self._marker = validate_vm_instance_marker(request.instance_marker)
        self._request = replace(request, tailscale_auth_key="")
        self._platform, self._ctx = platform, ctx
        self._owner, self._custody, self._settings = owner, custody, settings
        self._names = derive_names(request.hostname)
        self._disk_name = f"agw-boot-{uuid4().hex}"
        self._lock = RLock()
        self._api = CreationAPI(request.vm_name, platform.config, ctx, platform.site_name)
        self._exposure = OwnedSSHExposure(request.vm_name)
        self._readiness: SSHReadiness | None = None
        self._trust: CreationSSHTrust | None = None
        self._resources: CreationResources | None = None
        self._selection: CreationSelection | None = None
        self._prefixes: tuple[str, ...] = ()
        self._deadline: Deadline | None = None
        self._binding: NativeExecutionBinding | None = None
        self._locator: ProviderLocator | None = None
        self._endpoint: str | None = None
        self._used = self._closed = self._rollback = self._settled = False
        self._metadata: dict[str, str] = {}

    @property
    def binding(self) -> NativeExecutionBinding | None:
        return self._binding

    @property
    def independent_availability(self) -> _IndependentJobAvailability:
        return _IndependentJobAvailability.NO_IDLE_STOP

    @property
    def guest_check(self) -> Callable[[VMGuestIdentity], None] | None:
        return None

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self._locator is not None else None

    @property
    def platform_metadata(self) -> Mapping[str, str]:
        self._publish_metadata()
        return MappingProxyType(dict(self._metadata))

    def _budget(self, deadline: Deadline) -> Deadline:
        remaining(deadline)
        if self._deadline is None:
            raise StateError("GCE creation has no original finite budget")
        assert deadline.expires_at is not None and self._deadline.expires_at is not None
        selected = Deadline(min(deadline.expires_at, self._deadline.expires_at))
        remaining(selected)
        return selected

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        if not self._lock.acquire(timeout=min(remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("GCE creation transition deadline expired")
        control: BaseException | None = None
        try:
            remaining(deadline)
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

    def _open(self) -> None:
        if self._closed or self._api.cleanup_incomplete:
            raise StateError("GCE creation access cannot admit work")

    def _observe(self, deadline: Deadline) -> tuple[VMStatus, str | None]:
        assert self._resources is not None and self._selection is not None
        resource = self._resources.resources["instance"]
        if resource.resource_id is None:
            raise StateError("GCE creation has no associated VM numeric identity")
        value = self._api.get(resource.path, deadline)
        assert value is not None
        selected = self._selection
        endpoint, disk_path = instance_endpoint(
            value,
            project=selected.project,
            zone=selected.zone,
            name=self._names.backend_name,
            resource_id=resource.resource_id,
            network=selected.network,
            subnet=selected.subnet,
            tag=self._names.network_tag,
            disk_path=selected.disk_path,
        )
        if self._resources.resources["disk"].resource_id is None:
            self._resources.capture_disk(disk_path, deadline)
        self._publish_metadata()
        power = value.get("status")
        if type(power) is not str or power not in {
            "PROVISIONING",
            "STAGING",
            "RUNNING",
            "STOPPING",
            "TERMINATED",
            "SUSPENDING",
            "SUSPENDED",
            "REPAIRING",
        }:
            raise ValidationError("GCE created VM power observation is invalid")
        return (
            VMStatus.RUNNING
            if power == "RUNNING"
            else VMStatus.STOPPED
            if power in {"TERMINATED", "SUSPENDED"}
            else VMStatus.UNKNOWN
        ), endpoint

    def _publish_metadata(self) -> None:
        if self._resources is None or self._selection is None:
            return
        resources, selected, names = self._resources.resources, self._selection, self._names
        if not any(resource.resource_id is not None for resource in resources.values()):
            return
        self._metadata.update(project_id=selected.project, zone=selected.zone, network_url=selected.network)
        for key in ("allow", "deny"):
            if resources[key].resource_id is not None:
                self._metadata.update(
                    {
                        f"{key}_rule": resources[key].body["name"],
                        f"{key}_rule_id": resources[key].resource_id or "",
                    }
                )
        if resources["allow"].resource_id is not None:
            self._metadata["allow_source_ranges"] = ",".join(self._prefixes)
        if resources["instance"].resource_id is not None:
            self._metadata.update(
                instance_name=names.backend_name,
                instance_id=resources["instance"].resource_id or "",
                network_tag=names.network_tag,
                access_config_name=ACCESS_CONFIG,
            )
        if resources["disk"].resource_id is not None:
            self._metadata.update(boot_disk=resources["disk"].path, boot_disk_id=resources["disk"].resource_id or "")
        if selected.subnet is not None:
            self._metadata["subnet_url"] = selected.subnet

    def provision(self, deadline: Deadline) -> VMCreationResult:
        with self._transition(deadline):
            self._open()
            if self._used or self._rollback:
                raise StateError("GCE creation provisioning is single use")
            self._used, self._deadline = True, deadline
            with self._owner._guard:  # noqa: SLF001
                self._owner._reconcile_transition_locked()  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
                self._owner._require_no_active_work_locked()  # noqa: SLF001
                if self._owner._obligations_sealed or self._owner._recovery_owner:  # noqa: SLF001
                    raise StateError("GCE creation requires an unsealed ordinary owner")
            self._prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
            selected = select(
                self._api,
                self._platform.config,
                self._request,
                self._names,
                self._prefixes,
                deadline,
                disk_name=self._disk_name,
            )
            self._selection = selected
            global_scope = f"projects/{selected.project}/global"
            zone_scope = f"projects/{selected.project}/zones/{selected.zone}"
            names = self._names
            resources = {
                "deny": Resource(
                    f"{global_scope}/firewalls/{names.deny_rule}",
                    "firewall",
                    firewall_body(
                        names,
                        selected.network,
                        self._prefixes,
                        allow=False,
                    ),
                ),
                "allow": Resource(
                    f"{global_scope}/firewalls/{names.allow_rule}",
                    "firewall",
                    firewall_body(
                        names,
                        selected.network,
                        self._prefixes,
                        allow=True,
                    ),
                ),
                "instance": Resource(
                    f"{zone_scope}/instances/{names.backend_name}",
                    "instance",
                    instance_body(
                        self._request,
                        names,
                        machine=selected.machine,
                        image=selected.image,
                        disk_type=selected.disk_type,
                        network=selected.network,
                        subnet=selected.subnet,
                        disk_name=self._disk_name,
                    ),
                ),
                "disk": Resource(selected.disk_path, "disk", {}),
            }
            self._resources = CreationResources(self._owner, self._api, resources)
            self._resources.register(deadline)
            for key in ("deny", "allow", "instance"):
                self._resources.insert(key, deadline)
            while True:
                power, endpoint = self._observe(deadline)
                if power is VMStatus.RUNNING and endpoint is not None:
                    break
                if power is VMStatus.STOPPED:
                    raise StateError("GCE created VM stopped before public prerequisite admission")
                time.sleep(min(0.1, remaining(deadline)))
            self._endpoint = endpoint
            self._locator = ProviderLocator(
                f"gcp-gce:{selected.project}:{selected.zone}:{resources['instance'].resource_id}"
            )
            self._check_route(deadline)
            self._readiness = SSHReadiness()
            self._readiness.wait(endpoint, deadline)
            self._trust = CreationSSHTrust(
                self._owner,
                vm_name=self._request.vm_name,
                admin_username=self._request.admin_username,
                instance_marker=self._marker,
                endpoint=endpoint,
                settings=self._settings,
                deadline=deadline,
            )
            self._trust.prepare()
            self._owner.list_pending_lifecycle_obligations()
            self._check_route(deadline)
            connection = vm_ssh_connection(
                self._request.vm_name, self._request.admin_username, endpoint, self._settings
            )
            observe_early_guest(
                connection,
                self._marker,
                deadline,
                self._custody,
                bootstrap_check=(
                    "attempt=0; until test -f " + STARTUP_MARKER + "; do "
                    'attempt=$((attempt + 1)); test "$attempt" -le 240 || exit 1; sleep 1; done'
                ),
            )
            self._check_route(deadline)
            carrier = SSHCarrier(connection)
            root = IdentityPlan(
                IdentityExpectation(0, 0, (0,)),
                IdentityMode.DIRECT if self._request.admin_username == "root" else IdentityMode.SUDO_ROOT,
            )
            self._binding = NativeExecutionBinding(
                carrier,
                self._request.admin_username,
                RuntimeSelection(RuntimeTargetOS.LINUX),
                _early_guest_facts_route=_EarlyGuestFactsRoute(carrier, root, self._request.admin_username),
                _new_managed_delivery=lambda: SSHCarrier(connection),
                _independent_availability=self.independent_availability,
            )
            return VMCreationResult(self.platform_metadata, self._binding, self._locator)

    def observe_power(self, deadline: Deadline) -> VMStatus:
        with self._transition(deadline):
            self._open()
            return self._observe(self._budget(deadline))[0]

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        with self._transition(deadline):
            self._open()
            power, _endpoint = self._observe(self._budget(deadline))
            if power is not VMStatus.RUNNING or self._locator is None:
                raise StateError("GCE created VM running locator is unavailable")
            return self._locator

    def _check_route(self, deadline: Deadline) -> None:
        with self._transition(deadline):
            budget = self._budget(deadline)
            self._open()
            power, endpoint = self._observe(budget)
            if power is not VMStatus.RUNNING or endpoint != self._endpoint:
                raise StateError("GCE created VM public route changed")
            assert self._selection is not None and self._resources is not None
            owned = {
                resource.body["name"]: (resource.resource_id, resource.body)
                for key, resource in self._resources.resources.items()
                if key in ("deny", "allow") and resource.resource_id is not None
            }
            check_policy(
                self._api,
                self._selection.project,
                self._selection.network,
                self._names.network_tag,
                self._prefixes,
                budget,
                owned=owned,
            )

    def complete_bootstrap(self, deadline: Deadline) -> None:
        with self._transition(deadline):
            self._open()
            if self._resources is None or self._locator is None or self._rollback:
                raise StateError("GCE creation cannot transfer an unproved resource")
            self.observe_locator(deadline)
            self._resources.accept(self._budget(deadline))

    def request_rollback(self) -> None:
        if self._resources is None or not self._resources.accepted:
            self._rollback = True

    def settle(self, deadline: Deadline) -> bool:
        with self._transition(deadline):
            self._closed = True
            if self._settled:
                return True
            if not self._custody.close(deadline):
                return False
            if self._readiness is not None and not self._readiness.close():
                return False
            if self._trust is not None and not self._trust.close(deadline):
                return False
            if not self._exposure.close(deadline) or not self._api.settle(deadline, final=False):
                return False
            resources = self._resources
            if resources is not None:
                # Cleanup uses this supplied finite budget to observe original
                # receipts and dispose exact resources, without replaying creation.
                resources.reconcile(deadline)
                if not resources.disposition_complete:
                    budget = deadline
                    if resources.accepted:
                        resources.accept(deadline)
                        if not resources.delete("allow", budget):
                            return False
                    elif self._rollback:
                        instance = resources.resources["instance"]
                        if instance.create_entered:
                            resources.wait(instance, budget)
                            if resources.resources["disk"].resource_id is None:
                                self._observe(budget)
                        for key in ("instance", "disk", "allow", "deny"):
                            if not resources.delete(key, budget):
                                return False
                    else:
                        return False
                if not self._api.settle(deadline, final=True) or not resources.resolve(deadline):
                    return False
            elif not self._api.settle(deadline, final=True):
                return False
            self._settled = True
            return True
