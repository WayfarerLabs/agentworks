"""Retained one-shot clone custody for the new Proxmox creation path.

This controller establishes clone association only. It does not provision a
guest, authorize rollback, produce a provider locator or finalize its owner.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _valid_task_id
from agentworks.execution.carriers.proxmox import ProxmoxCloneRequest, ProxmoxConnection, _ProxmoxWire
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

OBLIGATION_KIND = "proxmox-clone"
PAYLOAD_VERSION = 1
_UPID = re.compile(
    r"UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):([0-9A-Fa-f]{8}):"
    r"([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):qmclone:([1-9][0-9]*):([^:\s/]+):"
)


@dataclass(frozen=True, slots=True, repr=False)
class CloneIntent:
    """Exact public submission and retained evidence, without credentials."""

    origin: str
    node: str
    source_vmid: int
    api_identity: str
    request: ProxmoxCloneRequest
    source_smbios_uuid: str | None = None
    upid: str | None = None
    destination_smbios_uuid: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class CloneReceipt:
    """A clone worker identifies its source, independently of its destination."""

    upid: str
    node: str
    pid: int
    pstart: int
    starttime: int
    source_vmid: str
    api_identity: str


@dataclass(frozen=True, slots=True)
class CloneAssociation:
    """Observed destination incarnation after successful exact clone completion.

    This is neither guest identity nor the existing vmgenid provider locator.
    A future mutation must independently recheck this association.
    """

    node: str
    vmid: int
    smbios_uuid: str


def decode_clone_receipt(value: object, intent: CloneIntent) -> CloneReceipt:
    """Bind the external UPID to the submitted source, node and API identity."""
    if not _valid_task_id(value):
        raise ValidationError("Proxmox clone receipt is invalid")
    assert isinstance(value, str)
    match = _UPID.fullmatch(value)
    if match is None:
        raise ValidationError("Proxmox clone receipt is invalid")
    node, pid, pstart, starttime, source, identity = match.groups()
    if node != intent.node or source != str(intent.source_vmid) or identity != intent.api_identity:
        raise ValidationError("Proxmox clone receipt does not match its submitted source")
    return CloneReceipt(value, node, int(pid, 16), int(pstart, 16), int(starttime, 16), source, identity)


def _canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise ValidationError("Proxmox clone requires a canonical nonzero SMBIOS UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValidationError("Proxmox clone requires a canonical nonzero SMBIOS UUID") from None
    if parsed.int == 0 or str(parsed) != value.lower():
        raise ValidationError("Proxmox clone requires a canonical nonzero SMBIOS UUID")
    return str(parsed)


def _smbios_uuid(config: dict[str, object]) -> str | None:
    """Read a unique UUID from the provider's external SMBIOS property string."""
    value = config.get("smbios1")
    if value is None:
        return None
    if type(value) is not str or len(value) > 8192:
        raise ValidationError("Proxmox SMBIOS configuration is invalid")
    values = [item.removeprefix("uuid=") for item in value.split(",") if item.startswith("uuid=")]
    if not values:
        return None
    if len(values) != 1:
        raise ValidationError("Proxmox SMBIOS configuration has ambiguous UUIDs")
    return _canonical_uuid(values[0])


def encode_clone_intent(intent: CloneIntent) -> bytes:
    """Validate the bounded persisted adapter boundary, never secret fields."""
    if type(intent) is not CloneIntent or type(intent.request) is not ProxmoxCloneRequest:
        raise ValidationError("Proxmox clone intent is invalid")
    ProxmoxConnection(intent.origin, intent.node, intent.source_vmid, intent.api_identity, "validation-only")
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", intent.node) is None:
        raise ValidationError("Proxmox clone node is invalid")
    if re.fullmatch(r"[^:\s/]+", intent.api_identity) is None:
        raise ValidationError("Proxmox clone API identity is invalid")
    if intent.source_vmid == intent.request.newid:
        raise ValidationError("Proxmox clone destination must differ from its source")
    for value in (intent.source_smbios_uuid, intent.destination_smbios_uuid):
        if value is not None and _canonical_uuid(value) != value:
            raise ValidationError("Proxmox clone intent UUID is not canonical")
    if intent.upid is not None:
        decode_clone_receipt(intent.upid, intent)
    if intent.destination_smbios_uuid is not None and (
        intent.upid is None or intent.destination_smbios_uuid == intent.source_smbios_uuid
    ):
        raise ValidationError("Proxmox clone association lacks fresh acknowledged evidence")
    encoded = json.dumps(
        {"version": PAYLOAD_VERSION, **asdict(intent)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox clone intent exceeds lifecycle bound")
    return encoded


def _task_state(data: dict[str, object], receipt: CloneReceipt) -> str:
    """Accept only exact provider task identity and literal successful completion."""
    base, separator, token = receipt.api_identity.partition("!")
    expected: dict[str, object] = {
        "upid": receipt.upid,
        "node": receipt.node,
        "pid": receipt.pid,
        "pstart": receipt.pstart,
        "starttime": receipt.starttime,
        "type": "qmclone",
        "id": receipt.source_vmid,
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
    if data.get("status") == "stopped" and data.get("exitstatus") == "OK":
        return "succeeded"
    return "unknown"


class ProxmoxClone:
    """Retain before start; reconcile exact ledger replies without clone replay.

    Successful completion discharges only the clone request. The caller still
    owns the associated resource and every later provisioning obligation.
    No process-loss clone recovery or resource deletion is implemented here.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        connection: ProxmoxConnection,
        request: ProxmoxCloneRequest,
        *,
        instance_marker: str,
        custody: LocalDeliveryCustody,
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
        ):
            raise ValidationError("Proxmox clone requires exact VM ownership")
        marker = validate_vm_instance_marker(instance_marker)
        if request.description != f"agentworks-instance:{marker}":
            raise ValidationError("Proxmox clone description must bind the core instance marker")
        self._history = [
            CloneIntent(connection.api_url, connection.node, connection.vmid, connection.token_id, request)
        ]
        encode_clone_intent(self.intent)
        self._owner = owner
        self._custody = custody
        self._source = _ProxmoxWire(connection)
        self._destination = _ProxmoxWire(replace(connection, vmid=request.newid))
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._post_entered = False
        self._succeeded = False

    @property
    def intent(self) -> CloneIntent:
        return self._history[-1]

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def association(self) -> CloneAssociation | None:
        intent = self.intent
        uuid = intent.destination_smbios_uuid
        return None if uuid is None else CloneAssociation(intent.node, intent.request.newid, uuid)

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Proxmox clone requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Proxmox clone deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Proxmox clone transition deadline expired")

    def start(self, deadline: Deadline) -> CloneReceipt:
        """Observe the source, arm the exact intent, then submit one clone POST."""
        self._acquire(deadline)
        try:
            if self._attempted:
                raise StateError("Proxmox clone was already attempted")
            self._attempted = True
            config = self._source.request_current_config(timeout=self._remaining(deadline), custody=self._custody)
            self._remaining(deadline)
            self._history[0] = replace(self.intent, source_smbios_uuid=_smbios_uuid(config))
            self._remaining(deadline)
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_clone_intent(self.intent),
                obligation_id=self._obligation_id,
            )
            self._remaining(deadline)
            self._obligation.mark_possible_effect()
            remaining = self._remaining(deadline)
            self._post_entered = True
            upid = self._source.request_vm_clone(self.intent.request, timeout=remaining, custody=self._custody)
            receipt = decode_clone_receipt(upid, self.intent)
            self._history.append(replace(self.intent, upid=receipt.upid))
            self._reconcile_locked()
            self._remaining(deadline)
            return receipt
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Retry only bookkeeping of this retained attempt, never provider effects."""
        self._acquire(deadline)
        try:
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("Proxmox clone has no registration attempt")
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            if not self._post_entered and self._custody.settled:
                return
            raise StateError("Proxmox clone registration remains unavailable")
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("Proxmox clone custody conflicts")
        revision = row.payload_revision
        if not 0 <= revision < len(self._history) or row.payload != encode_clone_intent(self._history[revision]):
            raise StateError("Proxmox clone custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if not self._post_entered:
            # The retained call never crossed the wire boundary. Even a lost
            # arm reply leaves no clone effect after actual local delivery settles.
            if row.state is not LifecycleObligationState.RESOLVED and self._custody.settled:
                self._obligation.resolve()
        elif row.state is LifecycleObligationState.REGISTERED:
            raise StateError("Proxmox clone POST has no durable arm")
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT:
            for next_revision in range(revision + 1, len(self._history)):
                self._obligation.publish_payload(
                    expected_revision=next_revision - 1,
                    payload_version=PAYLOAD_VERSION,
                    payload=encode_clone_intent(self._history[next_revision]),
                )
            if self.association is not None and self._custody.settled:
                self._obligation.resolve()
        elif row.state is LifecycleObligationState.RESOLVED and (
            self.association is None or revision != len(self._history) - 1
        ):
            raise StateError("Proxmox clone custody was prematurely resolved")

    def wait(self, deadline: Deadline) -> CloneAssociation:
        """Observe the original exact task, then freshly bind its unlocked destination."""
        self._acquire(deadline)
        try:
            self._reconcile_locked()
            if self.intent.upid is None:
                raise StateError("Proxmox clone has no matching acknowledgment")
            receipt = decode_clone_receipt(self.intent.upid, self.intent)
            while not self._succeeded:
                data = self._source.request_task_status(
                    receipt.upid, timeout=self._remaining(deadline), custody=self._custody
                )
                self._remaining(deadline)
                self._reconcile_locked()
                state = _task_state(data, receipt)
                if state == "succeeded":
                    self._succeeded = True
                elif state == "running":
                    time.sleep(min(0.1, self._remaining(deadline)))
                else:
                    raise StateError("Proxmox clone task completion is unproved")
            if self.association is None:
                config = self._destination.request_current_config(
                    timeout=self._remaining(deadline), custody=self._custody
                )
                self._remaining(deadline)
                self._reconcile_locked()
                uuid = _smbios_uuid(config)
                if (
                    "lock" in config
                    or config.get("description") != self.intent.request.description
                    or uuid is None
                    or uuid == self.intent.source_smbios_uuid
                    or not self._custody.settled
                ):
                    raise StateError("Proxmox clone destination association is unproved")
                self._history.append(replace(self.intent, destination_smbios_uuid=uuid))
            self._reconcile_locked()
            self._remaining(deadline)
            association = self.association
            assert association is not None
            return association
        finally:
            self._lock.release()
