"""Retained new-only Proxmox creation, without resource rollback implementation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import LifecycleObligationState, OperationResourceKind, VMStatus
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution._proxmox_activation import ProxmoxActivation, TaskPhase
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import (
    ProxmoxCloneRequest,
    ProxmoxConfigurationRequest,
    ProxmoxConnection,
    _ProxmoxWire,
    _WireFailure,
)
from agentworks.operations import LifecycleObligation
from agentworks.plugins.proxmox._configuration import ProxmoxConfiguration
from agentworks.plugins.proxmox._creation import (
    CloneIntent,
    ProxmoxClone,
    _canonical_uuid,
    _smbios_uuid,
    encode_clone_intent,
)
from agentworks.plugins.proxmox._creation_bootstrap import ProxmoxCreationBootstrap
from agentworks.plugins.proxmox._native_access import _guest_info_responded
from agentworks.plugins.proxmox._native_binding import proxmox_native_binding
from agentworks.secrets.line_safety import LineOrientedSecretUse, require_line_safe_secret
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.operations import OperationOwner
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

RESOURCE_KIND = "proxmox-created-resource"
PAYLOAD_VERSION = 1


@dataclass(frozen=True, slots=True, repr=False)
class _ResourceIntent:
    origin: str
    node: str
    source_vmid: int
    vmid: int
    api_identity: str
    instance_marker: str
    vmgenid: str
    smbios_uuid: str | None = None
    provisioned: bool = False
    accepted: bool = False


def _encode(intent: _ResourceIntent) -> bytes:
    payload = json.dumps(
        {"version": PAYLOAD_VERSION, **asdict(intent)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox created-resource intent exceeds lifecycle bound")
    return payload


def _remaining(deadline: Deadline) -> float:
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("Proxmox creation requires a finite deadline")
    remaining = deadline.remaining()
    assert remaining is not None
    if remaining <= 0:
        raise TimeoutError("Proxmox creation deadline expired")
    return remaining


class ProxmoxOwnedCreationAccess:
    """One creation attempt and its unaccepted resource under the caller's owner.

    Only public inputs are copied. Secret delivery begins in provision, never
    construction. Settlement observes retained calls, never replays mutation.
    Requested rollback of a possible created resource remains unresolved here.
    """

    def __init__(
        self,
        request: ProvisionRequest,
        platform: ProxmoxPlatform,
        ctx: RunContext,
        owner: OperationOwner,
        custody: LocalDeliveryCustody,
    ) -> None:
        self._name = request.vm_name
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != self._name
        ):
            raise ValidationError("Proxmox creation requires exact VM ownership")
        if not platform.config.verify_ssl:
            raise ConfigError("Proxmox creation requires TLS certificate verification")
        source = platform._template_vmid(request.debian_release)
        self._authority = ProxmoxConnection(
            platform.config.api_url,
            platform.config.node,
            source,
            platform.config.token_id,
            "validation-only",
            ca_bundle=platform._ca_bundle(),
        )
        self._token_name = platform.config.token_secret
        self._marker = validate_vm_instance_marker(request.instance_marker)
        self._backend_name = f"{request.system_slug}-{self._name}" if request.system_slug else self._name
        self._storage, self._pool = platform.config.storage, platform.config.pool
        # Validate the public clone shape without treating a placeholder as a reservation.
        encode_clone_intent(
            CloneIntent(
                self._authority.api_url,
                self._authority.node,
                source,
                self._authority.token_id,
                self._clone_request(source + 1),
            )
        )
        self._generation = str(uuid4())
        if type(request.memory_gib) is not int or request.memory_gib <= 0:
            raise ValidationError("Proxmox creation requires positive integer memory size")
        self._settings = ProxmoxConfigurationRequest(
            request.cpus, request.memory_gib * 1024, request.admin_username, request.ssh_public_key, self._generation
        )
        if type(request.disk_gib) is not int or request.disk_gib <= 0:
            raise ValidationError("Proxmox creation requires positive integer disk size")
        self._disk_gib = request.disk_gib
        self._ctx, self._owner, self._custody = ctx, owner, custody
        self._lock = Lock()
        self._used = False
        self._rollback_requested = False
        self._accepted = False
        self._deadline: Deadline | None = None
        self._wire: _ProxmoxWire | None = None
        self._binding: NativeExecutionBinding | None = None
        self._locator: ProviderLocator | None = None
        self._result: VMCreationResult | None = None
        self.clone: ProxmoxClone | None = None
        self.configuration: ProxmoxConfiguration | None = None
        self.activation: ProxmoxActivation | None = None
        self.bootstrap: ProxmoxCreationBootstrap | None = None
        self._resource_id = uuid4().hex
        self._resource_history: list[_ResourceIntent] = []

    def _clone_request(self, vmid: int) -> ProxmoxCloneRequest:
        return ProxmoxCloneRequest(
            vmid, self._backend_name, f"agentworks-instance:{self._marker}", self._storage, self._pool
        )

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
        association = None if self.clone is None else self.clone.association
        if association is None:
            return MappingProxyType({})
        return MappingProxyType({"vmid": str(association.vmid), "node": association.node})

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(_remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Proxmox creation transition deadline expired")

    def provision(self, deadline: Deadline) -> VMCreationResult:
        self._acquire(deadline)
        try:
            if self._used or self._rollback_requested:
                raise StateError("Proxmox creation cannot be admitted again")
            self._used, self._deadline = True, deadline
            token = require_line_safe_secret(
                self._ctx.secret(self._token_name),
                use=LineOrientedSecretUse.PROXMOX_API,
                secret_name=self._token_name,
            )
            _remaining(deadline)
            source = replace(self._authority, token_secret=token)
            candidate = _ProxmoxWire(source).request_next_id(timeout=_remaining(deadline), custody=self._custody)
            _remaining(deadline)
            self.clone = ProxmoxClone(
                self._owner,
                self._name,
                source,
                self._clone_request(candidate),
                instance_marker=self._marker,
                custody=self._custody,
            )
            self._resource_history.append(
                _ResourceIntent(
                    source.api_url, source.node, source.vmid, candidate, source.token_id, self._marker, self._generation
                )
            )
            resource = self._owner.register_lifecycle_obligation(
                RESOURCE_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(self._resource_history[-1]),
                obligation_id=self._resource_id,
            )
            _remaining(deadline)
            resource.mark_possible_effect()
            _remaining(deadline)
            self.clone.start(deadline)
            association = self.clone.wait(deadline)
            self._refresh_resource()
            self._reconcile_resource()
            connection = replace(source, vmid=association.vmid)
            self._wire = _ProxmoxWire(connection)
            self.configuration = ProxmoxConfiguration(
                self._owner,
                self._name,
                connection,
                association,
                self._settings,
                instance_marker=self._marker,
                disk_gib=self._disk_gib,
                custody=self._custody,
            )
            self.configuration.configure(deadline)
            self.configuration.resize(deadline)
            self.configuration.wait(deadline)
            self._locator = self.observe_locator(deadline)
            self.activation = ProxmoxActivation(
                self._owner, self._name, connection, self._locator, custody=self._custody
            )
            self.activation.start(deadline)
            self._wait_activation(deadline)
            self._check_route(deadline)
            self._wait_guest_info(deadline)
            self._binding = proxmox_native_binding(connection)
            self.bootstrap = ProxmoxCreationBootstrap(
                self._owner,
                self._name,
                connection,
                association,
                expected_vmgenid=self._generation,
                instance_marker=self._marker,
                admin_username=self._settings.admin_username,
                custody=self._custody,
                deadline=deadline,
            )
            observation = self.bootstrap.start()
            if not observation.bootstrap_succeeded:
                raise StateError("Proxmox creation bootstrap completion is unproved")
            self._result = VMCreationResult(self.platform_metadata, self._binding, self._locator)
            self._resource_history.append(replace(self._resource_history[-1], provisioned=True))
            self._reconcile_resource()
            _remaining(deadline)
            return self._result
        finally:
            self._lock.release()

    def _current_config(self, deadline: Deadline) -> dict[str, object]:
        wire, clone = self._wire, self.clone
        association = None if clone is None else clone.association
        if wire is None or association is None:
            raise StateError("Proxmox creation has no associated route")
        self._owner.list_pending_lifecycle_obligations()
        config = wire.request_current_config(timeout=_remaining(deadline), custody=self._custody)
        _remaining(deadline)
        self._owner.list_pending_lifecycle_obligations()
        if (
            not self._custody.settled
            or "lock" in config
            or config.get("description") != f"agentworks-instance:{self._marker}"
            or _smbios_uuid(config) != association.smbios_uuid
            or config.get("vmgenid") != self._generation
        ):
            raise StateError("Proxmox creation association or generation changed")
        return config

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        config = self._current_config(deadline)
        assert self._wire is not None
        connection = self._wire._connection
        generation = _canonical_uuid(config.get("vmgenid"))
        framed = json.dumps(
            ("agentworks.proxmox.provider-locator.v1", connection.api_url, connection.vmid, generation),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        _remaining(deadline)
        return ProviderLocator("proxmox:v1:" + hashlib.sha256(framed).hexdigest())

    def observe_power(self, deadline: Deadline) -> VMStatus:
        self._current_config(deadline)
        assert self._wire is not None
        power = self._wire.request_power(timeout=_remaining(deadline), custody=self._custody)
        _remaining(deadline)
        if not self._custody.settled:
            raise StateError("Proxmox power observation retains local delivery")
        status = power.get("status")
        if type(status) is str:
            if status == "running":
                return VMStatus.RUNNING
            if status == "stopped":
                return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def _check_route(self, deadline: Deadline) -> None:
        if self.observe_locator(deadline) != self._locator or self.observe_power(deadline) is not VMStatus.RUNNING:
            raise StateError("Proxmox creation route is unavailable")

    def _wait_activation(self, deadline: Deadline) -> None:
        assert self.activation is not None
        while True:
            observation = self.activation.observe(deadline)
            if observation.request_settled:
                return
            if observation.ha_handoff or observation.phase is not TaskPhase.RUNNING:
                raise StateError("Proxmox creation activation remains unproved")
            time.sleep(min(0.1, _remaining(deadline)))

    def _wait_guest_info(self, deadline: Deadline) -> None:
        assert self._wire is not None
        while True:
            self._owner.list_pending_lifecycle_obligations()
            try:
                info = self._wire.request_guest_info(timeout=_remaining(deadline), custody=self._custody)
            except _WireFailure:
                info = {}
            _remaining(deadline)
            if not self._custody.settled:
                raise StateError("Proxmox guest information retains local delivery")
            self._owner.list_pending_lifecycle_obligations()
            if _guest_info_responded(info):
                return
            time.sleep(min(0.1, _remaining(deadline)))

    def _refresh_resource(self) -> None:
        association = None if self.clone is None else self.clone.association
        if association is not None and self._resource_history[-1].smbios_uuid is None:
            self._resource_history.append(replace(self._resource_history[-1], smbios_uuid=association.smbios_uuid))

    def _reconcile_resource(self) -> None:
        row = self._owner.inspect_lifecycle_obligation(self._resource_id)
        if row is None:
            if self.clone is None or not self.clone.wire_effect_possible:
                return
            raise StateError("Proxmox created-resource registration remains unavailable")
        revision = row.payload_revision
        if (
            row.obligation_kind != RESOURCE_KIND
            or row.payload_version != PAYLOAD_VERSION
            or not 0 <= revision < len(self._resource_history)
            or row.payload != _encode(self._resource_history[revision])
        ):
            raise StateError("Proxmox created-resource custody conflicts")
        unused = self.clone is None or not self.clone.wire_effect_possible
        can_resolve = self._custody.settled and (unused or self._accepted)
        if row.state is LifecycleObligationState.RESOLVED:
            if not can_resolve or revision != len(self._resource_history) - 1:
                raise StateError("Proxmox created-resource custody was prematurely resolved")
            return
        obligation = LifecycleObligation(self._owner, row)
        for next_revision in range(revision + 1, len(self._resource_history)):
            obligation.publish_payload(
                expected_revision=next_revision - 1,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(self._resource_history[next_revision]),
            )
        if not unused and row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("Proxmox created-resource effect has no durable arm")
        if can_resolve:
            obligation.resolve()

    def _settle_controllers(self, deadline: Deadline) -> bool:
        if not self._custody.close(deadline):
            return False
        ids: list[str] = []
        if self.clone is not None:
            row = self._owner.inspect_lifecycle_obligation(self.clone.obligation_id)
            if row is not None or self.clone.wire_effect_possible:
                self.clone.reconcile(deadline)
                if self.clone.intent.upid is not None and self.clone.association is None:
                    self.clone.wait(deadline)
            ids.append(self.clone.obligation_id)
        if self.configuration is not None:
            self.configuration.reconcile(deadline)
            resize = self._owner.inspect_lifecycle_obligation(self.configuration.resize_obligation_id)
            if resize is not None and resize.state is LifecycleObligationState.POSSIBLE_EFFECT:
                self.configuration.wait(deadline)
            ids.extend((self.configuration.configuration_obligation_id, self.configuration.resize_obligation_id))
        if self.activation is not None:
            row = self._owner.inspect_lifecycle_obligation(self.activation.obligation_id)
            if row is not None or self.activation.obligation is not None:
                self.activation.reconcile(deadline)
                if self.activation.payload.upid is not None:
                    self._wait_activation(deadline)
            ids.append(self.activation.obligation_id)
        if self.bootstrap is not None:
            if self.bootstrap.intent is not None:
                self.bootstrap.reconcile(deadline)
                self.bootstrap.observe(deadline)
            ids.append(self.bootstrap.obligation_id)
        _remaining(deadline)
        return self._custody.settled and all(
            (row := self._owner.inspect_lifecycle_obligation(identifier)) is None
            or row.state is LifecycleObligationState.RESOLVED
            for identifier in ids
        )

    def complete_bootstrap(self, deadline: Deadline) -> None:
        """Accept core's completion proof, then durably transfer the exact resource."""
        self._acquire(deadline)
        try:
            if self._rollback_requested or self._result is None or not self._settle_controllers(deadline):
                raise StateError("Proxmox bootstrap resource transfer is unproved")
            self._reconcile_resource()
            if not self._accepted:
                self._accepted = True
                self._resource_history.append(replace(self._resource_history[-1], accepted=True))
            self._reconcile_resource()
            _remaining(deadline)
        finally:
            self._lock.release()

    def request_rollback(self) -> None:
        with self._lock:
            if self._accepted:
                raise StateError("Proxmox bootstrap rollback window has closed")
            self._rollback_requested = True

    def settle(self, deadline: Deadline) -> bool:
        """Retire actual requests, retaining any untransferred created resource."""
        self._acquire(deadline)
        try:
            settled = self._settle_controllers(deadline)
            if self._resource_history:
                self._refresh_resource()
                self._reconcile_resource()
            row = self._owner.inspect_lifecycle_obligation(self._resource_id)
            return settled and (row is None or row.state is LifecycleObligationState.RESOLVED)
        finally:
            self._lock.release()
