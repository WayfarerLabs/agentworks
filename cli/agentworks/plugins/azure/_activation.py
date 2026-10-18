"""Legacy persisted ARM start evidence and shared request validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError
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
