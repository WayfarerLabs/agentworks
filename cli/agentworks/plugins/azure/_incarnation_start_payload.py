"""Bounded evidence for one exact-incarnation Azure start request."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError
from agentworks.plugins.azure._activation import (
    ActivationPayload,
    StartAcknowledgment,
    encode_activation_payload,
)
from agentworks.plugins.azure._identity import canonical_vm_id

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline

PAYLOAD_VERSION = 2
MAX_STATUS_BODY_BYTES = 16 * 1024


class StartState(StrEnum):
    """Request completion, independently of VM availability."""

    UNKNOWN = "Unknown"
    PENDING = "Pending"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELED = "Canceled"


TERMINAL_STATES = (StartState.SUCCEEDED, StartState.FAILED, StartState.CANCELED)


class StartDeadlineExpired(TimeoutError):
    """Core-authored deadline refusal, separate from SDK timeout diagnostics."""


@dataclass(frozen=True, slots=True, repr=False)
class IncarnationStartPayload:
    """Recorded identity, validated acknowledgment and optional closed status."""

    resource_id: str
    vm_id: str
    acknowledgment: StartAcknowledgment | None = None
    terminal_status: StartState | None = None


def encode_start_payload(payload: IncarnationStartPayload) -> bytes:
    """Validate plugin/recovery input without accepting legacy identity evidence."""
    if type(payload) is not IncarnationStartPayload or canonical_vm_id(payload.vm_id) != payload.vm_id:
        raise ValidationError("Azure incarnation start payload is invalid")
    encode_activation_payload(ActivationPayload(payload.resource_id, payload.acknowledgment))
    if payload.terminal_status is not None and (
        type(payload.terminal_status) is not StartState
        or payload.terminal_status not in TERMINAL_STATES
        or payload.acknowledgment is None
        or (monitor_endpoint(payload.acknowledgment) is None and payload.terminal_status is not StartState.SUCCEEDED)
    ):
        raise ValidationError("Azure incarnation start terminal evidence is invalid")
    encoded = json.dumps(asdict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Azure incarnation start payload exceeds its storage bound")
    return encoded


def decode_start_payload(data: bytes) -> IncarnationStartPayload:
    """Reject corrupt, duplicate-key or noncanonical persisted v2 input."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Azure incarnation start payload is invalid")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or set(value) != {"resource_id", "vm_id", "acknowledgment", "terminal_status"}:
            raise ValueError
        ack = value["acknowledgment"]
        if ack is not None:
            if type(ack) is not dict or set(ack) != {"status_code", "request_id", "location", "azure_async_operation"}:
                raise ValueError
            ack = StartAcknowledgment(**ack)
        status = value["terminal_status"]
        payload = IncarnationStartPayload(
            value["resource_id"], value["vm_id"], ack, None if status is None else StartState(status)
        )
        if encode_start_payload(payload) != data:
            raise ValueError
        return payload
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("Azure incarnation start payload is invalid") from None


def monitor_endpoint(ack: StartAcknowledgment) -> str | None:
    return ack.azure_async_operation if ack.azure_async_operation is not None else ack.location


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def observe_start_response(response: Any, ack: StartAcknowledgment, deadline: Deadline) -> StartState:
    """Validate one streamed monitor response; never consume Location bodies."""
    request = getattr(response, "request", None)
    status = getattr(response, "status_code", None)
    if (
        type(getattr(request, "method", None)) is not str
        or getattr(request, "method", None) != "GET"
        or type(getattr(request, "url", None)) is not str
        or getattr(request, "url", None) != monitor_endpoint(ack)
        or type(status) is not int
    ):
        raise ValidationError("Azure start monitor response identity is invalid")
    if ack.azure_async_operation is None:
        if status == 202:
            return StartState.PENDING
        if status in (200, 204):
            return StartState.SUCCEEDED
        raise ValidationError("Azure start monitor HTTP status is invalid")
    if status != 200:
        raise ValidationError("Azure start monitor HTTP status is invalid")
    body = bytearray()
    for chunk in response.iter_bytes():
        if type(chunk) is not bytes or len(chunk) > MAX_STATUS_BODY_BYTES - len(body):
            raise ValidationError("Azure start monitor body exceeds its bound")
        body.extend(chunk)
        # Retain no body, and refuse another iteration after elapsed local time.
        remaining = deadline.remaining()
        if remaining is None or remaining <= 0:
            raise StartDeadlineExpired("Azure start monitor deadline expired")
    try:
        value = json.loads(body, object_pairs_hook=_unique_object)
        if type(value) is not dict:
            raise ValueError
        observed = value.get("status")
        if type(observed) is not str or not 0 < len(observed) <= 256 or not observed.isascii():
            raise ValueError
        if not all(32 < ord(char) < 127 for char in observed):
            raise ValueError
        if observed in TERMINAL_STATES:
            return StartState(observed)
        return StartState.PENDING
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("Azure start monitor status is invalid") from None
