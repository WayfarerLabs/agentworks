"""Private single-attempt Proxmox activation custody and exact task observation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from threading import TIMEOUT_MAX, Lock
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire
from agentworks.operations import LifecycleObligation, OperationOwner

OBLIGATION_KIND = "proxmox-activation"
PAYLOAD_VERSION = 1
_UPID = re.compile(
    r"UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):([0-9A-Fa-f]{8}):"
    r"([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):([^:\s/]+):([^:\s/]*):([^:\s/]+):"
)


def _bounded(value: object, limit: int) -> bool:
    if type(value) is not str or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class ActivationPayload:
    """Non-secret exact route and retained acknowledgment, without credentials."""

    origin: str
    node: str
    vmid: int
    api_identity: str
    locator_sha256: str
    upid: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class StartReceipt:
    """Literal acknowledgment identity; never startup or drain evidence."""

    upid: str
    node: str
    pid: int
    pstart: int
    starttime: int
    task_type: str
    vmid: str
    api_identity: str


def decode_receipt(upid: object, payload: ActivationPayload) -> StartReceipt:
    """Validate bounded external UPID syntax and the complete selected identity."""
    if not _bounded(upid, 255):
        raise ValidationError("Proxmox activation receipt is invalid")
    assert isinstance(upid, str)
    match = _UPID.fullmatch(upid)
    if match is None:
        raise ValidationError("Proxmox activation receipt is invalid")
    node, pid, pstart, starttime, task_type, vmid, user = match.groups()
    if node != payload.node or vmid != str(payload.vmid) or task_type not in {"qmstart", "hastart"}:
        raise ValidationError("Proxmox activation receipt does not match the selected VM")
    if user != payload.api_identity:
        raise ValidationError("Proxmox activation receipt does not match the selected API identity")
    return StartReceipt(upid, node, int(pid, 16), int(pstart, 16), int(starttime, 16), task_type, vmid, user)


def encode_activation_payload(payload: ActivationPayload) -> bytes:
    """Validate and encode the bounded persisted adapter boundary."""
    if (
        type(payload) is not ActivationPayload
        or not _bounded(payload.origin, MAX_LIFECYCLE_PAYLOAD_BYTES)
        or not _bounded(payload.node, MAX_LIFECYCLE_PAYLOAD_BYTES)
        or type(payload.vmid) is not int
        or payload.vmid <= 0
        or not _bounded(payload.api_identity, MAX_LIFECYCLE_PAYLOAD_BYTES)
        or type(payload.locator_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", payload.locator_sha256) is None
    ):
        raise ValidationError("Proxmox activation payload is invalid")
    # Reuse the actual connection boundary without retaining a credential.
    ProxmoxConnection(payload.origin, payload.node, payload.vmid, payload.api_identity, "validation-only")
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", payload.node) is None:
        raise ValidationError("Proxmox activation node is invalid")
    if re.fullmatch(r"[^:\s/]+", payload.api_identity) is None:
        raise ValidationError("Proxmox activation API identity is invalid")
    if payload.upid is not None:
        decode_receipt(payload.upid, payload)
    try:
        encoded = json.dumps(
            {"version": PAYLOAD_VERSION, **asdict(payload)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValidationError("Proxmox activation payload is invalid") from error
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox activation payload exceeds lifecycle bound")
    return encoded


def decode_activation_payload(data: bytes) -> ActivationPayload:
    """Reject corrupt, foreign-version or noncanonical persisted recovery data."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox activation payload is invalid")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or type(value.get("version")) is not int or value["version"] != PAYLOAD_VERSION:
            raise ValueError("invalid version")
        if set(value) != {"version", "origin", "node", "vmid", "api_identity", "locator_sha256", "upid"}:
            raise ValueError("invalid keys")
        payload = ActivationPayload(
            value["origin"], value["node"], value["vmid"], value["api_identity"], value["locator_sha256"], value["upid"]
        )
        if encode_activation_payload(payload) != data:
            raise ValueError("noncanonical payload")
        return payload
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("Proxmox activation payload is invalid") from None


class TaskPhase(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"


class TaskOutcome(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    WARNINGS = "warnings"


@dataclass(frozen=True, slots=True)
class ActivationObservation:
    """Worker facts, independently of startup success or provider freshness.

    Successful ordinary worker completion settles only its activation request.
    A stopped worker without a supported successful result can leave children.
    No task fact proves incarnation safety or blanket request drain.
    HA task exit proves only that the handoff worker stopped.
    """

    phase: TaskPhase = TaskPhase.UNKNOWN
    outcome: TaskOutcome = TaskOutcome.UNKNOWN
    warning_count: int | None = None
    ha_handoff: bool = False
    request_settled: bool = False


def observe_task(data: dict[str, object], receipt: StartReceipt) -> ActivationObservation:
    """Bind external task fields literally, including split token normalization."""
    expected: dict[str, object] = {
        "upid": receipt.upid,
        "node": receipt.node,
        "pid": receipt.pid,
        "pstart": receipt.pstart,
        "starttime": receipt.starttime,
        "type": receipt.task_type,
        "id": receipt.vmid,
    }
    ha = receipt.task_type == "hastart"
    unknown = ActivationObservation(ha_handoff=ha)
    if any(type(data.get(key)) is not type(value) or data[key] != value for key, value in expected.items()):
        return unknown
    base, separator, token = receipt.api_identity.partition("!")
    if data.get("user") != base or type(data.get("user")) is not str:
        return unknown
    if separator:
        if type(data.get("tokenid")) is not str or data["tokenid"] != token:
            return unknown
    elif "tokenid" in data:
        return unknown
    if data.get("status") == "running":
        return ActivationObservation(TaskPhase.RUNNING, ha_handoff=ha)
    if data.get("status") != "stopped":
        return unknown
    outcome = TaskOutcome.UNKNOWN
    count = None
    status = data.get("exitstatus")
    if status == "OK":
        outcome = TaskOutcome.OK
    elif type(status) is str and len(status) <= 64 and re.fullmatch(r"WARNINGS: [0-9]+", status):
        outcome = TaskOutcome.WARNINGS
        count = int(status.split(" ")[1])
    return ActivationObservation(TaskPhase.STOPPED, outcome, count, ha)


class ProxmoxActivation:
    """Caller-retained one-shot adapter under an already selected exact VM owner.

    Retain this object before start. Reconciliation retries only exact ledger
    bookkeeping, never the POST. Observation can use a fresh finite budget
    after ordinary owner admission stops. Neither method closes the owner.
    """

    def __init__(
        self, owner: OperationOwner, vm_name: str, connection: ProxmoxConnection, expected_locator: ProviderLocator
    ) -> None:
        if owner.ownership.scope.resource_kind is not OperationResourceKind.VM:
            raise ValidationError("Proxmox activation requires VM ownership")
        if owner.ownership.scope.resource_name != vm_name or type(connection) is not ProxmoxConnection:
            raise ValidationError("Proxmox activation requires an exact selected VM")
        if type(expected_locator) is not ProviderLocator:
            raise ValidationError("Proxmox activation requires a selected locator")
        self._payload = ActivationPayload(
            connection.api_url,
            connection.node,
            connection.vmid,
            connection.token_id,
            hashlib.sha256(
                b"agentworks:proxmox-activation:locator:v1\0" + expected_locator.token.encode("utf-8")
            ).hexdigest(),
        )
        encode_activation_payload(self._payload)
        self._owner = owner
        self._wire = _ProxmoxWire(connection)
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._mark_began = False
        self._terminal: ActivationObservation | None = None

    @property
    def payload(self) -> ActivationPayload:
        return self._payload

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def obligation(self) -> LifecycleObligation | None:
        return self._obligation

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Proxmox activation requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Proxmox activation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Proxmox activation transition deadline expired")

    def start(self, deadline: Deadline) -> StartReceipt:
        """Retain registration and admission uncertainty before one start POST."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            if self._attempted:
                raise StateError("Proxmox activation was already attempted")
            self._attempted = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
                obligation_id=self._obligation_id,
            )
            self._remaining(deadline)
            self._mark_began = True
            self._obligation.mark_possible_effect()
            remaining = self._remaining(deadline)
            upid = self._wire.request_vm_start(timeout=remaining)
            receipt = decode_receipt(upid, self._payload)
            # Retain a matching response before CAS, including a late response.
            self._payload = replace(self._payload, upid=receipt.upid)
            self._reconcile_locked()
            self._remaining(deadline)
            return receipt
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Reconcile exact custody after a lost ledger reply without dispatch."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("Proxmox activation has no registration attempt")
        rows = self._owner.list_lifecycle_obligations()
        row = next((row for row in rows if row.obligation_id == self._obligation_id), None)
        if row is None:
            raise StateError("Proxmox activation registration remains uncertain")
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("Proxmox activation custody conflicts")
        durable = decode_activation_payload(row.payload)
        initial = replace(self._payload, upid=None)
        initial_matches = durable == initial and row.payload_revision == 0
        receipt_matches = durable == self._payload and self._payload.upid is not None and row.payload_revision == 1
        if not (initial_matches or receipt_matches):
            raise StateError("Proxmox activation custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.REGISTERED and not self._mark_began:
            self._obligation.resolve()
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT and durable != self._payload:
            self._obligation.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
            )
        elif row.state is LifecycleObligationState.RESOLVED and self._mark_began and self._terminal is None:
            raise StateError("Proxmox activation custody was prematurely resolved")
        if self._terminal is not None and self._obligation.state is LifecycleObligationState.POSSIBLE_EFFECT:
            self._obligation.resolve()

    def observe(self, deadline: Deadline) -> ActivationObservation:
        """Read only the durable exact receipt; never search or re-admit effects."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            obligation = self._obligation
            if (
                self._terminal is not None
                and obligation is not None
                and obligation.state is LifecycleObligationState.RESOLVED
            ):
                return replace(self._terminal, request_settled=True)
            if self._payload.upid is None or obligation is None:
                raise StateError("Proxmox activation has no matching receipt")
            if obligation.state is not LifecycleObligationState.POSSIBLE_EFFECT:
                raise StateError("Proxmox activation has no admitted custody")
            receipt = decode_receipt(self._payload.upid, self._payload)
            data = self._wire.request_task_status(receipt.upid, timeout=self._remaining(deadline))
            self._remaining(deadline)
            # Fence again after the read, including takeover during the exchange.
            self._reconcile_locked()
            self._remaining(deadline)
            observation = observe_task(data, receipt)
            if (
                observation.phase is TaskPhase.STOPPED
                and observation.outcome in {TaskOutcome.OK, TaskOutcome.WARNINGS}
                and not observation.ha_handoff
            ):
                # Preserve exact terminal evidence before a potentially lost resolve reply.
                self._terminal = observation
                self._reconcile_locked()
                self._remaining(deadline)
                return replace(observation, request_settled=True)
            return observation
        finally:
            self._lock.release()
