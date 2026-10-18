"""Bounded GCE activation identity codec and selected Operation interpretation."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from uuid import UUID

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError

MAX_BODY_BYTES = 65_536
_COMPONENT = re.compile(r"[a-z0-9-]{1,63}")
_OPERATION = re.compile(r"[A-Za-z0-9_-]{1,512}")
_HOSTS = ("https://compute.googleapis.com", "https://www.googleapis.com")


class ActivationState(StrEnum):
    """Knowledge of this start request, never current guest readiness."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, repr=False)
class ActivationPayload:
    """Selected non-secret identity, original request UUID and optional ACK name."""

    project_id: str
    zone: str
    instance_name: str
    instance_id: str
    request_id: str
    operation_name: str | None = None


def encode_activation_payload(payload: ActivationPayload) -> bytes:
    """Validate plugin/recovery input and emit canonical bounded JSON."""
    try:
        if (
            type(payload) is not ActivationPayload
            or any(
                type(value) is not str or _COMPONENT.fullmatch(value) is None
                for value in (payload.project_id, payload.zone, payload.instance_name)
            )
            or type(payload.instance_id) is not str
            or re.fullmatch(r"[1-9][0-9]{0,19}", payload.instance_id) is None
            or int(payload.instance_id) > 2**64 - 1
            or type(payload.request_id) is not str
            or str(UUID(payload.request_id)) != payload.request_id
            or UUID(payload.request_id).int == 0
            or (
                payload.operation_name is not None
                and (type(payload.operation_name) is not str or _OPERATION.fullmatch(payload.operation_name) is None)
            )
        ):
            raise ValueError("invalid identity")
        encoded = json.dumps(asdict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValueError("oversized payload")
        return encoded
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("GCE activation payload is invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_activation_payload(data: bytes) -> ActivationPayload:
    """Refuse corrupt, foreign, oversized or noncanonical persisted input."""
    if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("GCE activation payload is invalid")
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object)
        if type(value) is not dict or set(value) != {
            "project_id",
            "zone",
            "instance_name",
            "instance_id",
            "request_id",
            "operation_name",
        }:
            raise ValueError("invalid keys")
        payload = ActivationPayload(**value)
        if encode_activation_payload(payload) != data:
            raise ValueError("noncanonical payload")
        return payload
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("GCE activation payload is invalid") from None


def start_url(payload: ActivationPayload) -> str:
    """Construct the fixed start route from validated selected components."""
    return (
        f"https://compute.googleapis.com/compute/v1/projects/{payload.project_id}/zones/{payload.zone}"
        f"/instances/{payload.instance_name}/start?requestId={payload.request_id}"
    )


def operation_url(payload: ActivationPayload) -> str:
    """Construct the fixed zonal GET route from the retained acknowledgment."""
    if payload.operation_name is None:
        raise ValidationError("GCE activation has no acknowledged operation")
    return (
        f"https://compute.googleapis.com/compute/v1/projects/{payload.project_id}/zones/{payload.zone}"
        f"/operations/{payload.operation_name}"
    )


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _operation(body: bytes, payload: ActivationPayload) -> dict[str, object]:
    if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
        raise ValueError("oversized body")
    value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    path = f"/compute/v1/projects/{payload.project_id}/zones/{payload.zone}/instances/{payload.instance_name}"
    if (
        type(value) is not dict
        or value.get("kind") != "compute#operation"
        or type(value.get("name")) is not str
        or _OPERATION.fullmatch(value["name"]) is None
        or value.get("clientOperationId") != payload.request_id
        or value.get("targetId") != payload.instance_id
        or value.get("targetLink") not in tuple(f"{host}{path}" for host in _HOSTS)
        or ("operationType" in value and value["operationType"] != "start")
    ):
        raise ValueError("foreign operation")
    return value


def decode_acknowledgment(body: bytes, payload: ActivationPayload) -> str:
    """Select matching provider Operation identity without interpreting startup."""
    try:
        operation_name = _operation(body, payload)["name"]
        assert isinstance(operation_name, str)
        return operation_name
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("GCE activation acknowledgment is invalid") from None


def decode_completion(body: bytes, payload: ActivationPayload) -> ActivationState:
    """Validate a selected zoneOperations.get response without retaining diagnostics."""
    try:
        value = _operation(body, payload)
        zone = f"/compute/v1/projects/{payload.project_id}/zones/{payload.zone}"
        if (
            payload.operation_name is None
            or value["name"] != payload.operation_name
            or value.get("operationType") != "start"
            or value.get("status") not in ("PENDING", "RUNNING", "DONE")
            or ("zone" in value and value["zone"] not in tuple(f"{host}{zone}" for host in _HOSTS))
            or (
                "selfLink" in value
                and value["selfLink"]
                not in tuple(f"{host}{zone}/operations/{payload.operation_name}" for host in _HOSTS)
            )
        ):
            raise ValueError("foreign completion")
        has_error = "error" in value
        if has_error:
            error = value["error"]
            if type(error) is not dict or type(error.get("errors")) is not list or not error["errors"]:
                raise ValueError("malformed operation error")
            if any(
                type(item) is not dict or type(item.get("code")) is not str or not item["code"]
                for item in error["errors"]
            ):
                raise ValueError("malformed operation error code")
        # The REST schema defines these as failure indicators, never success evidence.
        if "httpErrorStatusCode" in value:
            code = value["httpErrorStatusCode"]
            if not has_error or type(code) is not int or not 400 <= code <= 599:
                raise ValueError("malformed HTTP error status")
        if "httpErrorMessage" in value:
            message = value["httpErrorMessage"]
            if not has_error or type(message) is not str or not message:
                raise ValueError("malformed HTTP error message")
        if value["status"] != "DONE":
            if has_error or "httpErrorStatusCode" in value or "httpErrorMessage" in value:
                raise ValueError("nonterminal error contradiction")
            return ActivationState.PENDING
        return ActivationState.FAILED if has_error else ActivationState.SUCCEEDED
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("GCE activation completion is invalid") from None
