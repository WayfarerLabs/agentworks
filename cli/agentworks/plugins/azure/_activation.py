"""Private one-shot ARM VM start admission and acknowledgment custody."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.azure._identity import MAX_RESOURCE_ID_BYTES as MAX_RESOURCE_ID_BYTES
from agentworks.plugins.azure._identity import parse_resource_id

OBLIGATION_KIND = "azure-vm-start"
PAYLOAD_VERSION = 1
MAX_REQUEST_ID_BYTES = 256
MAX_OPERATION_URL_BYTES = 8192


def _subscription(resource_id: object) -> str:
    return parse_resource_id(resource_id)[0]


def start_url(resource_id: str) -> str:
    """Quote literal identity components before appending the fixed action and query."""
    _subscription(resource_id)
    path = "/".join(quote(component, safe="") for component in resource_id.split("/"))
    return f"https://management.azure.com{path}/start?api-version=2026-04-01"


def _bounded_ascii(value: object, limit: int) -> bool:
    return type(value) is str and 0 < len(value) <= limit and all(32 < ord(char) < 127 for char in value)


def _operation_url(value: object, resource_id: str) -> bool:
    if not _bounded_ascii(value, MAX_OPERATION_URL_BYTES):
        return False
    assert isinstance(value, str)
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        decoded_path = unquote(parsed.path, errors="strict")
        return (
            parsed.scheme == "https"
            and parsed.netloc == "management.azure.com"
            and not parsed.fragment
            and "#" not in value
            and "," not in value
            and re.search(r"%(?![0-9a-fA-F]{2})", value) is None
            and all(component not in {".", ".."} for component in decoded_path.split("/"))
            and "\\" not in decoded_path
            and all(ord(char) >= 32 and ord(char) != 127 for char in decoded_path)
            and parsed.path.startswith(f"/subscriptions/{quote(_subscription(resource_id), safe='')}/")
            and (not parsed.query or (len(query) == 1 and query[0][0] == "api-version" and bool(query[0][1])))
        )
    except ValueError:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class StartAcknowledgment:
    """Response identity only; even HTTP 200 does not settle startup."""

    status_code: int
    request_id: str | None = None
    location: str | None = None
    azure_async_operation: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ActivationPayload:
    """Exact selected ARM VM identity and optional non-secret acknowledgment."""

    resource_id: str
    acknowledgment: StartAcknowledgment | None = None


def _validate_acknowledgment(ack: StartAcknowledgment, resource_id: str) -> None:
    if (
        type(ack) is not StartAcknowledgment
        or type(ack.status_code) is not int
        or ack.status_code not in (200, 202)
        or (
            ack.request_id is not None
            and (not _bounded_ascii(ack.request_id, MAX_REQUEST_ID_BYTES) or "," in ack.request_id)
        )
        or (ack.location is not None and not _operation_url(ack.location, resource_id))
        or (ack.azure_async_operation is not None and not _operation_url(ack.azure_async_operation, resource_id))
        or (ack.status_code == 202 and ack.location is None and ack.azure_async_operation is None)
    ):
        raise ValidationError("Azure activation acknowledgment is invalid")


def encode_activation_payload(payload: ActivationPayload) -> bytes:
    """Validate plugin/recovery input and emit canonical JSON under the ledger limit."""
    if type(payload) is not ActivationPayload:
        raise ValidationError("Azure activation payload is invalid")
    _subscription(payload.resource_id)
    if payload.acknowledgment is not None:
        _validate_acknowledgment(payload.acknowledgment, payload.resource_id)
    encoded = json.dumps(asdict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Azure activation payload exceeds its storage bound")
    return encoded


def decode_activation_payload(data: bytes) -> ActivationPayload:
    """Reject corrupt, oversized or noncanonical persisted recovery input."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Azure activation payload is invalid")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or set(value) != {"resource_id", "acknowledgment"}:
            raise ValueError("invalid keys")
        ack = value["acknowledgment"]
        if ack is not None:
            if type(ack) is not dict or set(ack) != {"status_code", "request_id", "location", "azure_async_operation"}:
                raise ValueError("invalid acknowledgment keys")
            ack = StartAcknowledgment(**ack)
        payload = ActivationPayload(value["resource_id"], ack)
        if encode_activation_payload(payload) != data:
            raise ValueError("noncanonical payload")
        return payload
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("Azure activation payload is invalid") from None


def decode_acknowledgment(response: Any, resource_id: str) -> StartAcknowledgment:
    """Validate the external HTTP boundary without reading provider body content."""
    request = getattr(response, "request", None)
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    if (
        type(getattr(request, "method", None)) is not str
        or getattr(request, "method", None) != "POST"
        or type(getattr(request, "url", None)) is not str
        or getattr(request, "url", None) != start_url(resource_id)
        or type(status) is not int
        or status not in (200, 202)
        or not isinstance(headers, Mapping)
    ):
        raise ValidationError("Azure activation acknowledgment is invalid")
    retained: dict[str, str] = {}
    for key, value in headers.items():
        if type(key) is not str or key.lower() not in {"x-ms-request-id", "location", "azure-asyncoperation"}:
            continue
        if type(value) is not str:
            raise ValidationError("Azure activation acknowledgment headers are invalid")
        key = key.lower()
        if key in retained:
            raise ValidationError("Azure activation acknowledgment headers conflict")
        retained[key] = value
    ack = StartAcknowledgment(
        status,
        retained.get("x-ms-request-id"),
        retained.get("location"),
        retained.get("azure-asyncoperation"),
    )
    # Combined payload capacity is also an external-response boundary.
    encode_activation_payload(ActivationPayload(resource_id, ack))
    return ack


class AzureVMActivation:
    """Caller-retained one-shot start, with fenced bookkeeping and no settlement.

    Construction is passive. Retain before start. SDK and credential setup are
    synchronous: budgets and late-result refusal do not promise hard preemption.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        credential: object,
        resource_id: str,
        expected_locator: ProviderLocator,
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(expected_locator) is not ProviderLocator
        ):
            raise ValidationError("Azure activation requires exact selected VM ownership")
        self._payload = ActivationPayload(resource_id)
        encode_activation_payload(self._payload)
        if expected_locator.token != f"azure-vm:{resource_id}":
            raise ValidationError("Azure activation locator does not match the selected identity")
        self._owner = owner
        self._credential = credential
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._mark_began = False
        self._unclosed_handles: tuple[Any, ...] = ()

    @property
    def payload(self) -> ActivationPayload:
        return self._payload

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def obligation(self) -> LifecycleObligation | None:
        return self._obligation

    @property
    def cleanup_incomplete(self) -> bool:
        """Original handles remain in custody until close returns normally."""
        return bool(self._unclosed_handles)

    def _close_handles(self) -> None:
        control: KeyboardInterrupt | SystemExit | None = None
        try:
            for handle in self._unclosed_handles:
                try:
                    handle.close()
                except Exception:
                    pass
                except (KeyboardInterrupt, SystemExit) as error:
                    if control is None:
                        control = error
                else:
                    # Interrupted bookkeeping may retain a closed original, never lose an uncertain one.
                    self._unclosed_handles = tuple(
                        original for original in self._unclosed_handles if original is not handle
                    )
        except (KeyboardInterrupt, SystemExit):
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Azure activation requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Azure activation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Azure activation transition deadline expired")

    def start(self, deadline: Deadline) -> StartAcknowledgment:
        """Submit at most once, retaining matching ACK before publication or cleanup."""
        self._acquire(deadline)
        control: KeyboardInterrupt | SystemExit | None = None
        try:
            self._remaining(deadline)
            if self._attempted:
                raise StateError("Azure activation was already attempted")
            self._attempted = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
                obligation_id=self._obligation_id,
            )
            from azure.core.rest import HttpRequest

            from agentworks.plugins.azure._activation_client import compute_start_client

            try:
                self._remaining(deadline)
                self._unclosed_handles = (
                    compute_start_client(self._credential, _subscription(self._payload.resource_id)),
                )
                client = self._unclosed_handles[0]
                self._remaining(deadline)
                request = HttpRequest("POST", start_url(self._payload.resource_id))
                self._mark_began = True
                self._obligation.mark_possible_effect()
                remaining = self._remaining(deadline)
                self._unclosed_handles = (
                    client.send_request(request, stream=True, connection_timeout=remaining, read_timeout=remaining),
                    client,
                )
                response = self._unclosed_handles[0]
                acknowledgment = decode_acknowledgment(response, self._payload.resource_id)
                self._payload = replace(self._payload, acknowledgment=acknowledgment)
                self._reconcile_locked()
            except (KeyboardInterrupt, SystemExit) as error:
                control = error
                raise
            finally:
                self._close_handles()
            self._remaining(deadline)
            return acknowledgment
        except (KeyboardInterrupt, SystemExit):
            if control is not None:
                raise control from None
            raise
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Reconcile custody only, never replay, infer power settlement or close owner."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("Azure activation has no registration attempt")
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            raise StateError("Azure activation registration remains uncertain")
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("Azure activation custody conflicts")
        durable = decode_activation_payload(row.payload)
        initial = replace(self._payload, acknowledgment=None)
        initial_matches = durable == initial and row.payload_revision == 0
        acknowledgment_matches = (
            durable == self._payload and self._payload.acknowledgment is not None and row.payload_revision == 1
        )
        if not (initial_matches or acknowledgment_matches):
            raise StateError("Azure activation custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED and self._mark_began:
            raise StateError("Azure activation custody was prematurely resolved")
        if row.state is LifecycleObligationState.REGISTERED and not self._mark_began:
            self._obligation.resolve()
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT and durable != self._payload:
            self._obligation.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
            )
