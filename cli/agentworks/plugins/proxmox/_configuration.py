"""Retained configuration and resize of one newly associated stopped clone.

This leaf settles only its two requests. It does not start a VM, authorize
deletion, establish guest identity, provision a guest or finalize its owner.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _valid_digest, _valid_task_id
from agentworks.execution.carriers.proxmox import ProxmoxConfigurationRequest, ProxmoxConnection, _ProxmoxWire
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.proxmox._creation import CloneAssociation, _canonical_uuid, _smbios_uuid
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

CONFIGURATION_KIND = "proxmox-configuration"
RESIZE_KIND = "proxmox-resize"
PAYLOAD_VERSION = 1
_UPID = re.compile(
    r"UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):([0-9A-Fa-f]{8}):"
    r"([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):resize:([1-9][0-9]*):([^:\s/]+):"
)


@dataclass(frozen=True, slots=True, repr=False)
class ConfigurationIntent:
    """Exact public configuration and explicit synchronous acknowledgment."""

    origin: str
    node: str
    vmid: int
    api_identity: str
    instance_marker: str
    smbios_uuid: str
    request: ProxmoxConfigurationRequest
    digest: str
    disk: DiskObservation
    acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class DiskObservation:
    """Current scsi0 property string and its volume and byte-size meaning."""

    raw: str
    volume: str
    size_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class ResizeIntent:
    """The exact absolute resize and positively observed completion evidence."""

    configuration: ConfigurationIntent
    disk: DiskObservation
    size_gib: int
    digest: str
    upid: str | None = None
    task_succeeded: bool = False
    verified_disk: DiskObservation | None = None
    verified_digest: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ResizeReceipt:
    """One exact resize worker, independently of disk completion evidence."""

    upid: str
    node: str
    pid: int
    pstart: int
    starttime: int
    vmid: str
    api_identity: str


def _encode(intent: ConfigurationIntent | ResizeIntent) -> bytes:
    """Bound canonical adapter evidence before crossing the persisted boundary."""
    encoded = json.dumps(
        {"version": PAYLOAD_VERSION, **asdict(intent)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox configuration evidence exceeds lifecycle bound")
    return encoded


def _disk(config: dict[str, object]) -> DiskObservation:
    """Decode the provider's bounded current drive, refusing ambiguous properties."""
    raw = config.get("scsi0")
    if type(raw) is not str or not raw or len(raw) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise StateError("Proxmox creation disk is unavailable")
    properties: dict[str, str] = {}
    for index, part in enumerate(raw.split(",")):
        key, separator, value = part.partition("=")
        if not separator and index == 0:
            key, value = "file", part
        elif not separator:
            raise StateError("Proxmox creation disk is ambiguous")
        if key in properties:
            raise StateError("Proxmox creation disk is ambiguous")
        properties[key] = value
    volume = properties.get("file")
    size = properties.get("size", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", size)
    if not volume or volume == "none" or properties.get("media") == "cdrom" or match is None:
        raise StateError("Proxmox creation disk lacks volume and size evidence")
    number, unit = match.groups()
    try:
        byte_size = Fraction(number) * (1024 ** {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[unit])
    except ValueError:
        raise StateError("Proxmox creation disk size is invalid") from None
    if byte_size <= 0 or byte_size.denominator != 1:
        raise StateError("Proxmox creation disk size is invalid")
    return DiskObservation(raw, volume, byte_size.numerator)


def decode_resize_receipt(value: object, intent: ResizeIntent) -> ResizeReceipt:
    """Bind the external UPID to the destination, node and submitted API identity."""
    if not _valid_task_id(value):
        raise ValidationError("Proxmox resize receipt is invalid")
    assert isinstance(value, str)
    match = _UPID.fullmatch(value)
    if match is None:
        raise ValidationError("Proxmox resize receipt is invalid")
    node, pid, pstart, starttime, vmid, identity = match.groups()
    config = intent.configuration
    if node != config.node or vmid != str(config.vmid) or identity != config.api_identity:
        raise ValidationError("Proxmox resize receipt does not match its submitted destination")
    return ResizeReceipt(value, node, int(pid, 16), int(pstart, 16), int(starttime, 16), vmid, identity)


def _task_state(data: dict[str, object], receipt: ResizeReceipt) -> str:
    """Require exact task fields and literal successful resize completion."""
    base, separator, token = receipt.api_identity.partition("!")
    expected: dict[str, object] = {
        "upid": receipt.upid,
        "node": receipt.node,
        "pid": receipt.pid,
        "pstart": receipt.pstart,
        "starttime": receipt.starttime,
        "type": "resize",
        "id": receipt.vmid,
        "user": base,
    }
    if separator:
        expected["tokenid"] = token
    elif "tokenid" in data:
        return "unknown"
    if any(type(data.get(key)) is not type(value) or data[key] != value for key, value in expected.items()):
        return "unknown"
    if data.get("status") == "running":
        return "running"
    return "succeeded" if data.get("status") == "stopped" and data.get("exitstatus") == "OK" else "unknown"


def _remaining(deadline: Deadline) -> float:
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("Proxmox configuration requires a finite deadline")
    remaining = deadline.remaining()
    assert remaining is not None
    if remaining <= 0:
        raise TimeoutError("Proxmox configuration deadline expired")
    return remaining


class ProxmoxConfiguration:
    """Caller-retained two-effect leaf, with no replay or owner finalization.

    Call configure, resize and wait under the creation operation's original
    deadline. After interruption, reconciliation retries only bookkeeping;
    wait observes an already acknowledged resize under a finite cleanup budget.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        connection: ProxmoxConnection,
        association: CloneAssociation,
        request: ProxmoxConfigurationRequest,
        *,
        instance_marker: str,
        disk_gib: int,
        custody: LocalDeliveryCustody,
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(connection) is not ProxmoxConnection
            or type(association) is not CloneAssociation
            or (connection.node, connection.vmid) != (association.node, association.vmid)
            or type(request) is not ProxmoxConfigurationRequest
            or type(disk_gib) is not int
            or disk_gib <= 0
        ):
            raise ValidationError("Proxmox configuration requires exact newly associated VM ownership")
        self._marker = validate_vm_instance_marker(instance_marker)
        if _canonical_uuid(association.smbios_uuid) != association.smbios_uuid:
            raise ValidationError("Proxmox clone association UUID is not canonical")
        self._connection = connection
        self._association = association
        self._request = request
        self._owner = owner
        self._wire = _ProxmoxWire(connection)
        self._custody = custody
        self._disk_gib = disk_gib
        self._lock = Lock()
        self.configuration_obligation_id = uuid4().hex
        self.resize_obligation_id = uuid4().hex
        self._configuration_history: list[ConfigurationIntent] = []
        self._resize_history: list[ResizeIntent] = []
        self._configuration_attempted = False
        self._configuration_entered = False
        self._resize_attempted = False
        self._resize_entered = False

    @property
    def acknowledged_generation(self) -> str | None:
        """Return the applied generation only after the exact config acknowledgment."""
        if not self._configuration_history or not self._configuration_history[-1].acknowledged:
            return None
        return self._configuration_history[-1].request.vmgenid

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(_remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Proxmox configuration transition deadline expired")

    def _observe(self, deadline: Deadline, *, configured: bool) -> tuple[dict[str, object], str]:
        self._owner.list_pending_lifecycle_obligations()
        config = self._wire.request_current_config(timeout=_remaining(deadline), custody=self._custody)
        power = self._wire.request_power(timeout=_remaining(deadline), custody=self._custody)
        _remaining(deadline)
        self._owner.list_pending_lifecycle_obligations()
        if (
            not self._custody.settled
            or "lock" in config
            or power.get("status") != "stopped"
            or config.get("description") != f"agentworks-instance:{self._marker}"
            or _smbios_uuid(config) != self._association.smbios_uuid
            or not _valid_digest(config.get("digest"))
        ):
            raise StateError("Proxmox stopped clone association is unproved")
        digest = config["digest"]
        assert isinstance(digest, str)
        if configured:
            expected = self._request.body(digest)
            for key, value in expected.items():
                actual = config.get(key)
                if key in {"memory", "cores"}:
                    if type(actual) is int:
                        actual = str(actual)
                    if type(actual) is not str or actual != str(value):
                        raise StateError("Proxmox current configuration does not match its acknowledged intent")
                elif type(actual) is not type(value) or actual != value:
                    raise StateError("Proxmox current configuration does not match its acknowledged intent")
        return config, digest

    def configure(self, deadline: Deadline) -> None:
        """Retain and arm one guarded synchronous PUT to the newly stopped clone."""
        self._acquire(deadline)
        try:
            if self._configuration_attempted:
                raise StateError("Proxmox configuration was already attempted")
            self._configuration_attempted = True
            config, digest = self._observe(deadline, configured=False)
            disk = _disk(config)
            if disk.size_bytes > self._disk_gib * 1024**3:
                raise StateError("Proxmox creation disk cannot shrink")
            connection = self._connection
            intent = ConfigurationIntent(
                connection.api_url,
                connection.node,
                connection.vmid,
                connection.token_id,
                self._marker,
                self._association.smbios_uuid,
                self._request,
                digest,
                disk,
            )
            payload = _encode(intent)
            self._configuration_history.append(intent)
            obligation = self._owner.register_lifecycle_obligation(
                CONFIGURATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=payload,
                obligation_id=self.configuration_obligation_id,
            )
            _remaining(deadline)
            obligation.mark_possible_effect()
            remaining = _remaining(deadline)
            self._configuration_entered = True
            self._wire.request_vm_configure(
                self._request,
                digest=digest,
                timeout=remaining,
                custody=self._custody,
            )
            # A true fact distinguishes a returned null acknowledgment from no reply.
            self._configuration_history.append(replace(self._configuration_history[-1], acknowledged=True))
            self._reconcile_configuration()
            _remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_configuration(self) -> None:
        row = self._owner.inspect_lifecycle_obligation(self.configuration_obligation_id)
        if row is None:
            if not self._configuration_entered and self._custody.settled:
                return
            raise StateError("Proxmox configuration registration remains unavailable")
        revision = row.payload_revision
        history = self._configuration_history
        if (
            row.obligation_kind != CONFIGURATION_KIND
            or row.payload_version != PAYLOAD_VERSION
            or not 0 <= revision < len(history)
            or row.payload != _encode(history[revision])
        ):
            raise StateError("Proxmox configuration custody conflicts")
        obligation = LifecycleObligation(self._owner, row)
        if not self._configuration_entered:
            if row.state is not LifecycleObligationState.RESOLVED and self._custody.settled:
                obligation.resolve()
            return
        if row.state is LifecycleObligationState.REGISTERED:
            raise StateError("Proxmox configuration PUT has no durable arm")
        if row.state is LifecycleObligationState.RESOLVED:
            if revision != len(history) - 1 or not history[-1].acknowledged:
                raise StateError("Proxmox configuration custody was prematurely resolved")
            return
        for next_revision in range(revision + 1, len(history)):
            obligation.publish_payload(
                expected_revision=next_revision - 1,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(history[next_revision]),
            )
        if history[-1].acknowledged and self._custody.settled:
            durable = self._owner.inspect_lifecycle_obligation(self.configuration_obligation_id)
            if (
                durable is None
                or durable.payload_revision != len(history) - 1
                or durable.payload != _encode(history[-1])
            ):
                raise StateError("Proxmox configuration acknowledgment is not durably exact")
            obligation.resolve()

    def resize(self, deadline: Deadline) -> ResizeReceipt:
        """Recheck all current settings and retain one guarded absolute disk resize."""
        self._acquire(deadline)
        try:
            if self._resize_attempted:
                raise StateError("Proxmox resize was already attempted")
            self._resize_attempted = True
            self._reconcile_configuration()
            if not self._configuration_history or not self._configuration_history[-1].acknowledged:
                raise StateError("Proxmox resize requires acknowledged configuration")
            config, digest = self._observe(deadline, configured=True)
            disk = _disk(config)
            if disk.volume != self._configuration_history[-1].disk.volume:
                raise StateError("Proxmox creation disk volume changed")
            if disk.size_bytes > self._disk_gib * 1024**3:
                raise StateError("Proxmox creation disk cannot shrink")
            intent = ResizeIntent(self._configuration_history[-1], disk, self._disk_gib, digest)
            self._resize_history.append(intent)
            obligation = self._owner.register_lifecycle_obligation(
                RESIZE_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(intent),
                obligation_id=self.resize_obligation_id,
            )
            _remaining(deadline)
            obligation.mark_possible_effect()
            remaining = _remaining(deadline)
            self._resize_entered = True
            upid = self._wire.request_vm_resize(
                size_gib=self._disk_gib,
                digest=digest,
                timeout=remaining,
                custody=self._custody,
            )
            receipt = decode_resize_receipt(upid, intent)
            self._resize_history.append(replace(intent, upid=receipt.upid))
            self._reconcile_resize()
            _remaining(deadline)
            return receipt
        finally:
            self._lock.release()

    def _reconcile_resize(self) -> None:
        row = self._owner.inspect_lifecycle_obligation(self.resize_obligation_id)
        if row is None:
            if not self._resize_entered and self._custody.settled:
                return
            raise StateError("Proxmox resize registration remains unavailable")
        revision = row.payload_revision
        history = self._resize_history
        if (
            row.obligation_kind != RESIZE_KIND
            or row.payload_version != PAYLOAD_VERSION
            or not 0 <= revision < len(history)
            or row.payload != _encode(history[revision])
        ):
            raise StateError("Proxmox resize custody conflicts")
        obligation = LifecycleObligation(self._owner, row)
        if not self._resize_entered:
            if row.state is not LifecycleObligationState.RESOLVED and self._custody.settled:
                obligation.resolve()
            return
        if row.state is LifecycleObligationState.REGISTERED:
            raise StateError("Proxmox resize PUT has no durable arm")
        if row.state is LifecycleObligationState.RESOLVED:
            if revision != len(history) - 1 or history[-1].verified_disk is None:
                raise StateError("Proxmox resize custody was prematurely resolved")
            return
        for next_revision in range(revision + 1, len(history)):
            obligation.publish_payload(
                expected_revision=next_revision - 1,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(history[next_revision]),
            )
        if history[-1].verified_disk is not None and self._custody.settled:
            durable = self._owner.inspect_lifecycle_obligation(self.resize_obligation_id)
            if (
                durable is None
                or durable.payload_revision != len(history) - 1
                or durable.payload != _encode(history[-1])
            ):
                raise StateError("Proxmox resize completion is not durably exact")
            obligation.resolve()

    def reconcile(self, deadline: Deadline) -> None:
        """Retry exact retained bookkeeping without starting any provider request."""
        self._acquire(deadline)
        try:
            self._reconcile_configuration()
            self._reconcile_resize()
            _remaining(deadline)
        finally:
            self._lock.release()

    def wait(self, deadline: Deadline) -> ResizeIntent:
        """Observe the original resize task and freshly verify its resulting disk."""
        self._acquire(deadline)
        try:
            self._reconcile_configuration()
            self._reconcile_resize()
            if not self._resize_history or self._resize_history[-1].upid is None:
                raise StateError("Proxmox resize has no matching acknowledgment")
            intent = self._resize_history[-1]
            receipt = decode_resize_receipt(intent.upid, intent)
            while not self._resize_history[-1].task_succeeded:
                data = self._wire.request_task_status(receipt.upid, timeout=_remaining(deadline), custody=self._custody)
                _remaining(deadline)
                self._reconcile_resize()
                state = _task_state(data, receipt)
                if state == "succeeded":
                    self._resize_history.append(replace(self._resize_history[-1], task_succeeded=True))
                    self._reconcile_resize()
                elif state == "running":
                    time.sleep(min(0.1, _remaining(deadline)))
                else:
                    raise StateError("Proxmox resize task completion is unproved")
            if self._resize_history[-1].verified_disk is None:
                config, digest = self._observe(deadline, configured=True)
                disk = _disk(config)
                if disk.volume != intent.disk.volume or disk.size_bytes != intent.size_gib * 1024**3:
                    raise StateError("Proxmox resized disk does not match its exact volume and target")
                self._resize_history.append(
                    replace(self._resize_history[-1], verified_disk=disk, verified_digest=digest)
                )
            self._reconcile_resize()
            _remaining(deadline)
            return self._resize_history[-1]
        finally:
            self._lock.release()
