"""Private one-shot EC2 start admission and acknowledgment custody."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner

OBLIGATION_KIND = "aws-ec2-start"
PAYLOAD_VERSION = 1


def _request_id(value: object) -> bool:
    if type(value) is not str or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class ActivationPayload:
    """Non-secret selected identity and optional provider acknowledgment."""

    account_id: str
    region: str
    instance_id: str
    request_id: str | None = None


def encode_activation_payload(payload: ActivationPayload) -> bytes:
    """Validate the persisted recovery boundary and emit canonical bounded JSON."""
    if (
        type(payload) is not ActivationPayload
        or type(payload.account_id) is not str
        or re.fullmatch(r"[0-9]{12}", payload.account_id) is None
        or type(payload.region) is not str
        or re.fullmatch(r"[a-z0-9-]{1,64}", payload.region) is None
        or type(payload.instance_id) is not str
        or re.fullmatch(r"i-[0-9a-f]{8}(?:[0-9a-f]{9})?", payload.instance_id) is None
        or (payload.request_id is not None and not _request_id(payload.request_id))
    ):
        raise ValidationError("EC2 activation payload is invalid")
    encoded = json.dumps(asdict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("EC2 activation payload exceeds lifecycle bound")
    return encoded


def decode_activation_payload(data: bytes) -> ActivationPayload:
    """Reject corrupt or noncanonical persisted adapter input."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("EC2 activation payload is invalid")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or set(value) != {"account_id", "region", "instance_id", "request_id"}:
            raise ValueError("invalid keys")
        payload = ActivationPayload(value["account_id"], value["region"], value["instance_id"], value["request_id"])
        if encode_activation_payload(payload) != data:
            raise ValueError("noncanonical payload")
        return payload
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("EC2 activation payload is invalid") from None


def decode_acknowledgment(response: object, payload: ActivationPayload) -> str:
    """Bind external acknowledgment to one selected instance, without interpreting power."""
    if type(response) is not dict:
        raise ValidationError("EC2 activation acknowledgment is invalid")
    changes = response.get("StartingInstances")
    metadata = response.get("ResponseMetadata")
    if (
        type(changes) is not list
        or len(changes) != 1
        or type(changes[0]) is not dict
        or type(changes[0].get("InstanceId")) is not str
        or changes[0]["InstanceId"] != payload.instance_id
        or type(metadata) is not dict
        or type(metadata.get("HTTPStatusCode")) is not int
        or not 200 <= metadata["HTTPStatusCode"] < 300
        or type(metadata.get("RetryAttempts")) is not int
        or metadata["RetryAttempts"] != 0
        or not _request_id(metadata.get("RequestId"))
    ):
        raise ValidationError("EC2 activation acknowledgment is invalid")
    request_id: str = metadata["RequestId"]
    return request_id


class EC2Activation:
    """Caller-retained admission for one EC2 start, never startup settlement.

    Construction is passive. Retain this adapter before calling start; after
    interruption reconcile retries fenced ledger bookkeeping only. Selected
    credential work is synchronous and cannot enforce a hard elapsed deadline.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        session: Any,
        account_id: str,
        region: str,
        instance_id: str,
        expected_locator: ProviderLocator,
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(expected_locator) is not ProviderLocator
        ):
            raise ValidationError("EC2 activation requires exact selected VM ownership")
        self._payload = ActivationPayload(account_id, region, instance_id)
        encode_activation_payload(self._payload)
        if expected_locator.token != f"aws-ec2:{account_id}:{region}:{instance_id}":
            raise ValidationError("EC2 activation locator does not match the selected identity")
        self._owner = owner
        self._session = session
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._mark_began = False

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
            raise ValidationError("EC2 activation requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("EC2 activation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("EC2 activation transition deadline expired")

    def start(self, deadline: Deadline) -> str:
        """Submit once and return acknowledgment identity, never completion evidence."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            if self._attempted:
                raise StateError("EC2 activation was already attempted")
            self._attempted = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
                obligation_id=self._obligation_id,
            )
            from botocore.config import Config

            remaining = self._remaining(deadline)
            client = self._session.client(
                "ec2",
                region_name=self._payload.region,
                config=Config(
                    connect_timeout=remaining,
                    read_timeout=remaining,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
            control: KeyboardInterrupt | SystemExit | None = None
            try:
                self._remaining(deadline)
                self._mark_began = True
                self._obligation.mark_possible_effect()
                self._remaining(deadline)
                response = client.start_instances(InstanceIds=[self._payload.instance_id])
                request_id = decode_acknowledgment(response, self._payload)
                # Retain even a late matching acknowledgment before publication or close.
                self._payload = replace(self._payload, request_id=request_id)
                self._reconcile_locked()
            except (KeyboardInterrupt, SystemExit) as error:
                control = error
                raise
            finally:
                # Ordinary close failures preserve the result; controls still escape.
                try:
                    with suppress(Exception):
                        client.close()
                except (KeyboardInterrupt, SystemExit):
                    if control is None:
                        raise
            self._remaining(deadline)
            return request_id
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Reconcile exact custody without another provider call or resolving startup."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("EC2 activation has no registration attempt")
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            raise StateError("EC2 activation registration remains uncertain")
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("EC2 activation custody conflicts")
        durable = decode_activation_payload(row.payload)
        initial = replace(self._payload, request_id=None)
        initial_matches = durable == initial and row.payload_revision == 0
        acknowledgment_matches = (
            durable == self._payload and self._payload.request_id is not None and row.payload_revision == 1
        )
        if not (initial_matches or acknowledgment_matches):
            raise StateError("EC2 activation custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED and self._mark_began:
            raise StateError("EC2 activation custody was prematurely resolved")
        if row.state is LifecycleObligationState.REGISTERED and not self._mark_began:
            self._obligation.resolve()
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT and durable != self._payload:
            self._obligation.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
            )
