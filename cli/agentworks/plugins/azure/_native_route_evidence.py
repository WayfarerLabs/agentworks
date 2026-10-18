"""Bounded public ARM response facts for an operation-owned security rule.

These are rule request receipts, independent of the persisted VM-start codecs.
No provider response body or provider error diagnostic enters the route ledger.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from agentworks.errors import ValidationError
from agentworks.plugins.azure._operation_url import valid_operation_url

if TYPE_CHECKING:
    from collections.abc import Callable

API_VERSION = "2025-09-01"
TERMINAL = {"Succeeded", "Failed", "Canceled"}
MAX_STATUS_BYTES = 16_384
MAX_RESOURCE_BYTES = 524_288


class RouteDeadlineExpired(TimeoutError):
    """Core-authored refusal, preserved across the SDK error boundary."""


def resource_url(resource_id: str) -> str:
    # Callers supply validated literal resource paths, never provider URLs.
    path = "/".join(quote(component, safe="") for component in resource_id.split("/"))
    return f"https://management.azure.com{path}?api-version={API_VERSION}"


def _ascii(value: object, limit: int) -> bool:
    return type(value) is str and 0 < len(value) <= limit and all(32 < ord(char) < 127 for char in value)


@dataclass(frozen=True, slots=True, repr=False)
class RuleAcknowledgment:
    status_code: int
    request_id: str | None
    azure_async_operation: str | None
    location: str | None

    @property
    def monitor(self) -> str | None:
        return self.azure_async_operation if self.azure_async_operation is not None else self.location


def response_identity(response: Any, method: str, url: str, statuses: tuple[int, ...]) -> int:
    """Validate the actual external response against its exact submitted request."""
    request = getattr(response, "request", None)
    request_method = getattr(request, "method", None)
    request_url = getattr(request, "url", None)
    status = getattr(response, "status_code", None)
    if (
        type(request_method) is not str
        or request_method != method
        or type(request_url) is not str
        or request_url != url
        or type(status) is not int
        or status not in statuses
    ):
        raise ValidationError("Azure route response identity or status is invalid")
    return status


def acknowledgment(response: Any, method: str, url: str, subscription: str) -> RuleAcknowledgment:
    """Validate only safe receipt headers, including on HTTP 200 and 201."""
    status = response_identity(response, method, url, (200, 201) if method == "PUT" else (200, 202, 204))
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        raise ValidationError("Azure route receipt headers are invalid")
    retained: dict[str, str] = {}
    for key, value in headers.items():
        if type(key) is not str or key.lower() not in {"x-ms-request-id", "azure-asyncoperation", "location"}:
            continue
        key = key.lower()
        if key in retained or not _ascii(value, 256 if key == "x-ms-request-id" else 8192) or "," in value:
            raise ValidationError("Azure route receipt headers are invalid")
        if key != "x-ms-request-id" and not valid_operation_url(value, subscription):
            raise ValidationError("Azure route monitor identity is invalid")
        retained[key] = value
    ack = RuleAcknowledgment(
        status, retained.get("x-ms-request-id"), retained.get("azure-asyncoperation"), retained.get("location")
    )
    if status == 202 and ack.monitor is None:
        raise ValidationError("Azure route receipt has no completion monitor")
    return ack


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def json_body(response: Any, budget: Callable[[], float], limit: int = MAX_RESOURCE_BYTES) -> dict[str, Any]:
    """Read bounded external JSON, checking the original budget before each read."""
    body = bytearray()
    budget()
    iterator = iter(response.iter_bytes())
    while True:
        budget()
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        if type(chunk) is not bytes or len(chunk) > limit - len(body):
            raise ValidationError("Azure route response body exceeds its bound")
        body.extend(chunk)
    try:
        value = json.loads(body, object_pairs_hook=_unique)
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("Azure route response JSON is invalid") from None


def monitor_state(response: Any, ack: RuleAcknowledgment, budget: Callable[[], float]) -> str:
    """Observe exactly the retained monitor without following new headers."""
    assert ack.monitor is not None
    status = response_identity(response, "GET", ack.monitor, (200,) if ack.azure_async_operation else (200, 202, 204))
    if ack.azure_async_operation is None:
        return "Pending" if status == 202 else "Succeeded"
    state = json_body(response, budget, MAX_STATUS_BYTES).get("status")
    if not _ascii(state, 256):
        raise ValidationError("Azure route monitor state is invalid")
    return state if state in TERMINAL else "Pending"
