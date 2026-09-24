"""Private durable WSL2 platform hold under an already owned exact VM."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.execution._vm_guest_identity_protocol import _valid_instance_marker
from agentworks.execution._wsl2_lifecycle import (
    GuestAnchorIdentity,
    GuestAnchorObserver,
    GuestAnchorPresence,
    HostClientStatus,
    OwnedHostClient,
    WSL2AnchorEvidence,
    WSL2GuestAnchorOwner,
)
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from agentworks.execution.carriers.wsl2 import WSL2Connection
    from agentworks.operations import LifecycleObligation, OperationOwner

OBLIGATION_KIND = "wsl2-platform-hold"
PAYLOAD_VERSION = 1
_MAX_LOCATOR_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    """The already-running Windows Agentworks process, not the WSL client."""

    pid: int
    creation_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or not 0 < self.pid <= 0xFFFFFFFF:
            raise ValidationError("WSL2 controller PID is invalid")
        if type(self.creation_ticks) is not int or not 0 < self.creation_ticks <= 0xFFFFFFFFFFFFFFFF:
            raise ValidationError("WSL2 controller creation time is invalid")


@dataclass(frozen=True, slots=True)
class WSL2HoldPayload:
    """Bounded non-secret recovery identity for one independent hold."""

    locator_sha256: str
    instance_marker: str
    distribution: str
    user: str
    nonce: str
    controller: ControllerIdentity
    guest: GuestAnchorIdentity | None = None

    def __post_init__(self) -> None:
        if not _hex(self.locator_sha256, 64) or not _valid_instance_marker(self.instance_marker):
            raise ValidationError("WSL2 hold target identity is invalid")
        if not _literal(self.distribution) or not _literal(self.user) or not _hex(self.nonce, 32):
            raise ValidationError("WSL2 hold preparation is invalid")
        if type(self.controller) is not ControllerIdentity:
            raise ValidationError("WSL2 hold controller identity is invalid")
        if self.guest is not None and type(self.guest) is not GuestAnchorIdentity:
            raise ValidationError("WSL2 hold guest identity is invalid")


def _hex(value: object, length: int) -> bool:
    return type(value) is str and len(value) == length and all(character in "0123456789abcdef" for character in value)


def _literal(value: object) -> bool:
    if type(value) is not str or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeEncodeError:
        return False


def locator_digest(locator: str) -> str:
    """Bind an opaque provider locator without persisting its expansion-heavy text."""
    if type(locator) is not str or "\0" in locator:
        raise ValidationError("WSL2 provider locator is invalid")
    try:
        raw = locator.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValidationError("WSL2 provider locator is invalid") from error
    if not raw or len(raw) > _MAX_LOCATOR_BYTES:
        raise ValidationError("WSL2 provider locator is invalid")
    return hashlib.sha256(b"agentworks:wsl2-platform-hold:locator:v1\0" + raw).hexdigest()


def encode_hold_payload(payload: WSL2HoldPayload) -> bytes:
    """Encode validated adapter state for the persisted ledger boundary."""
    if type(payload) is not WSL2HoldPayload:
        raise ValidationError("WSL2 hold payload is invalid")
    value: dict[str, Any] = {
        "controller_creation_ticks": payload.controller.creation_ticks,
        "controller_pid": payload.controller.pid,
        "distribution": payload.distribution,
        "instance_marker": payload.instance_marker,
        "locator_sha256": payload.locator_sha256,
        "nonce": payload.nonce,
        "user": payload.user,
        "version": PAYLOAD_VERSION,
    }
    if payload.guest is not None:
        value.update(
            guest_boot_id=payload.guest.boot_id,
            guest_pid=payload.guest.pid,
            guest_start_time=payload.guest.start_time,
        )
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValidationError("WSL2 hold payload is invalid") from error
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("WSL2 hold payload exceeds lifecycle bound")
    return encoded


def decode_hold_payload(data: bytes) -> WSL2HoldPayload:
    """Validate a persisted canonical ASCII record before recovery use."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("WSL2 hold payload is invalid")

    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or type(value.get("version")) is not int or value["version"] != PAYLOAD_VERSION:
            raise ValueError("invalid version")
        guest_fields = {"guest_boot_id", "guest_pid", "guest_start_time"}
        guest = (
            GuestAnchorIdentity(value["guest_boot_id"], value["guest_pid"], value["guest_start_time"])
            if guest_fields <= set(value)
            else None
        )
        payload = WSL2HoldPayload(
            value["locator_sha256"],
            value["instance_marker"],
            value["distribution"],
            value["user"],
            value["nonce"],
            ControllerIdentity(value["controller_pid"], value["controller_creation_ticks"]),
            guest,
        )
        if encode_hold_payload(payload) != data:
            raise ValueError("noncanonical JSON")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, ValidationError, RecursionError) as error:
        raise ValidationError("WSL2 hold payload is invalid") from error
    return payload


class WSL2PlatformHold:
    """Caller-retained private hold; neither release nor failure closes its owner."""

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        locator: str,
        instance_marker: str,
        connection: WSL2Connection,
        native: OwnedHostClient,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        self._owner = owner
        self._vm_name = vm_name
        self._locator = locator
        self._marker = instance_marker
        self._connection = connection
        self._native = native
        self._anchor = WSL2GuestAnchorOwner(connection, native, observer=observer)
        self._transition_lock = Lock()
        self._obligation: LifecycleObligation | None = None
        self._payload: WSL2HoldPayload | None = None
        self._attempted = False
        self._registration_uncertain = False

    @property
    def payload(self) -> WSL2HoldPayload | None:
        return self._payload

    @property
    def obligation(self) -> LifecycleObligation | None:
        return self._obligation

    @property
    def registration_uncertain(self) -> bool:
        return self._registration_uncertain

    @property
    def evidence(self) -> WSL2AnchorEvidence:
        return self._anchor.evidence

    def start(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Register, commit possible effect, dispatch once, and publish READY."""
        self._acquire_transition(deadline, "start")
        try:
            if deadline.expired:
                raise TimeoutError("WSL2 hold start deadline expired waiting for transition")
            return self._start_locked(deadline)
        finally:
            self._transition_lock.release()

    def _acquire_transition(self, deadline: Deadline, action: str) -> None:
        """Bound entry to one hold transition by its caller's finite deadline."""
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError(f"WSL2 hold {action} requires a finite deadline")
        first_attempt = True
        while True:
            remaining = deadline.remaining()
            assert remaining is not None
            if remaining <= 0:
                if first_attempt:
                    raise ValidationError(f"WSL2 hold {action} deadline has expired")
                raise TimeoutError(f"WSL2 hold {action} deadline expired waiting for transition")
            if self._transition_lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
                return
            first_attempt = False

    def _start_locked(self, deadline: Deadline) -> WSL2AnchorEvidence:
        if self._attempted:
            raise ValidationError("WSL2 platform hold was already started")
        if (
            self._owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or self._owner.ownership.scope.resource_name != self._vm_name
            or type(deadline) is not Deadline
            or deadline.expires_at is None
            or deadline.expired
        ):
            raise ValidationError("WSL2 hold requires exact VM scope and finite live deadline")
        digest = locator_digest(self._locator)
        if (
            not _valid_instance_marker(self._marker)
            or not _literal(self._connection.distribution)
            or not _literal(self._connection.user)
            or self._connection.wsl_executable.casefold() not in {"wsl", "wsl.exe"}
        ):
            raise ValidationError("WSL2 hold preparation is invalid")
        if (
            len(json.dumps(self._connection.distribution, ensure_ascii=True))
            + len(json.dumps(self._connection.user, ensure_ascii=True))
            > MAX_LIFECYCLE_PAYLOAD_BYTES - 512
        ):
            raise ValidationError("WSL2 hold preparation exceeds lifecycle bound")
        self._attempted = True
        pid, ticks = self._native.current_controller_identity()
        payload = WSL2HoldPayload(
            digest,
            self._marker,
            self._connection.distribution,
            self._connection.user,
            secrets.token_hex(16),
            ControllerIdentity(pid, ticks),
        )
        self._payload = payload
        encoded = encode_hold_payload(payload)
        self._registration_uncertain = True
        self._obligation = self._owner.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=PAYLOAD_VERSION, payload=encoded
        )
        self._registration_uncertain = False
        self._obligation.mark_possible_effect()
        try:
            evidence = self._anchor.start(deadline, nonce=payload.nonce)
        except BaseException as primary:
            try:
                self._publish_ready_identity()
            except BaseException:
                primary.add_note("WSL2 hold READY identity publication is uncertain")
            raise
        self._publish_ready_identity()
        return evidence

    def _publish_ready_identity(self) -> None:
        identity = self._anchor.evidence.identity
        if identity is None:
            return
        payload = self._payload
        obligation = self._obligation
        assert payload is not None and obligation is not None
        published = replace(payload, guest=identity)
        self._payload = published
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=PAYLOAD_VERSION,
            payload=encode_hold_payload(published),
        )

    def release(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Resolve only no-client creation or settled, exact guest absence."""
        self._acquire_transition(deadline, "release")
        try:
            if deadline.expired:
                raise TimeoutError("WSL2 hold release deadline expired waiting for transition")
            return self._release_locked(deadline)
        finally:
            self._transition_lock.release()

    def _release_locked(self, deadline: Deadline) -> WSL2AnchorEvidence:
        if not self._attempted:
            raise ValidationError("WSL2 platform hold was not started")
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("WSL2 hold release requires a finite deadline")
        if deadline.expired:
            raise ValidationError("WSL2 hold release deadline has expired")
        evidence = self._anchor.release(deadline)
        never_created = evidence.local.settled and evidence.local.host_client_status is HostClientStatus.NOT_CREATED
        guest_absent = (
            evidence.local.settled
            and evidence.identity is not None
            and evidence.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED
        )
        obligation = self._obligation
        if obligation is not None and (never_created or guest_absent):
            obligation.resolve()
        return evidence
