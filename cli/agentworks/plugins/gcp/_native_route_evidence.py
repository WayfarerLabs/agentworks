"""Bounded native firewall custody payloads and selected global Operations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from ipaddress import IPv4Network
from typing import Any
from uuid import UUID

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError
from agentworks.plugins.gcp._activation_operation import MAX_BODY_BYTES, ActivationState, operation_state
from agentworks.plugins.gcp._native_identity import NativeIdentity, canonical_path

HOSTS = ("https://compute.googleapis.com", "https://www.googleapis.com")
OPERATION = re.compile(r"[A-Za-z0-9_-]{1,512}")
TERMINAL = (ActivationState.SUCCEEDED, ActivationState.FAILED)


def numeric_id(value: object) -> str:
    """Validate a provider or persisted canonical positive uint64 identity."""
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1:
        raise ValidationError("GCE firewall numeric identity is invalid")
    return value


def request_id(value: object) -> str:
    """Validate the canonical nonzero UUID in persisted route input."""
    if type(value) is not str or str(UUID(value)) != value or not UUID(value).int:
        raise ValueError("invalid request UUID")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def json_object(body: bytes) -> dict[str, Any]:
    """Parse one complete bounded external raw UTF-8 JSON object."""
    try:
        if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
            raise ValueError("oversized body")
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        if type(value) is not dict:
            raise ValueError("expected object")
        return value
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("GCE route response JSON is invalid") from None


@dataclass(frozen=True, slots=True, repr=False)
class RoutePayload:
    """Selected identity and current request facts, never provider diagnostics."""

    identity: NativeIdentity
    prefixes: tuple[str, ...]
    nonce: str
    create_request_id: str
    create_operation: str | None = None
    rule_id: str | None = None
    create_state: ActivationState = ActivationState.UNKNOWN
    delete_request_id: str | None = None
    delete_operation: str | None = None
    delete_state: ActivationState = ActivationState.UNKNOWN

    @property
    def name(self) -> str:
        return f"agw-route-{self.nonce}"

    @property
    def description(self) -> str:
        return f"agentworks native SSH route {self.nonce}"


def _validate_payload(payload: RoutePayload) -> None:
    """Validate persisted selection and request facts at the decoding boundary."""
    try:
        if type(payload) is not RoutePayload or type(payload.identity) is not NativeIdentity:
            raise ValueError("invalid payload type")
        identity = payload.identity
        if (
            any(
                type(v) is not str or re.fullmatch(r"[a-z0-9-]{1,63}", v) is None
                for v in (identity.project_id, identity.zone, identity.instance_name)
            )
            or any(
                type(v) is not str or re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", v) is None
                for v in (identity.network_tag, identity.deny_rule)
            )
            or type(identity.access_config_name) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}", identity.access_config_name) is None
        ):
            raise ValueError("invalid native identity")
        numeric_id(identity.instance_id)
        numeric_id(identity.deny_rule_id)
        if canonical_path(identity.network_url, project_id=identity.project_id, kind="network") != identity.network_url:
            raise ValueError("noncanonical selected network")
        if (
            identity.subnet_url is not None
            and canonical_path(identity.subnet_url, project_id=identity.project_id, kind="subnet")
            != identity.subnet_url
        ):
            raise ValueError("noncanonical selected subnet")
        if (
            type(payload.prefixes) is not tuple
            or not 0 < len(payload.prefixes) <= 64
            or len(set(payload.prefixes)) != len(payload.prefixes)
            or any(type(p) is not str or str(IPv4Network(p)) != p for p in payload.prefixes)
            or type(payload.nonce) is not str
            or re.fullmatch(r"[0-9a-f]{32}", payload.nonce) is None
        ):
            raise ValueError("invalid selection")
        request_id(payload.create_request_id)
        for operation, state in (
            (payload.create_operation, payload.create_state),
            (payload.delete_operation, payload.delete_state),
        ):
            if (
                type(state) is not ActivationState
                or ((operation is None) != (state is ActivationState.UNKNOWN))
                or (operation is not None and (type(operation) is not str or OPERATION.fullmatch(operation) is None))
            ):
                raise ValueError("invalid operation knowledge")
        if (payload.create_operation is None) != (payload.rule_id is None):
            raise ValueError("incomplete acknowledgment")
        if payload.rule_id is not None:
            numeric_id(payload.rule_id)
        if payload.delete_request_id is not None:
            request_id(payload.delete_request_id)
            if payload.create_state not in TERMINAL or payload.rule_id is None:
                raise ValueError("premature delete intent")
        elif payload.delete_operation is not None:
            raise ValueError("missing delete intent")
    except (ValueError, TypeError, AttributeError, ValidationError):
        raise ValidationError("GCE route payload is invalid") from None


def encode_payload(payload: RoutePayload) -> bytes:
    """Serialize interior selected facts as canonical bounded v1 JSON."""
    data = json.dumps(asdict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("GCE route payload exceeds its storage bound")
    return data


def decode_payload(data: bytes) -> RoutePayload:
    """Reject corrupt, foreign or noncanonical persisted route input."""
    try:
        if type(data) is not bytes or len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValueError("oversized payload")
        value = json_object(data)
        value["identity"] = NativeIdentity(**value["identity"])
        value["prefixes"] = tuple(value["prefixes"])
        value["create_state"] = ActivationState(value["create_state"])
        value["delete_state"] = ActivationState(value["delete_state"])
        payload = RoutePayload(**value)
        _validate_payload(payload)
        if encode_payload(payload) != data:
            raise ValueError("noncanonical payload")
        return payload
    except (KeyError, ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("GCE route payload is invalid") from None


def base_url(payload: RoutePayload) -> str:
    return f"https://compute.googleapis.com/compute/v1/projects/{payload.identity.project_id}/global"


def rule_url(payload: RoutePayload) -> str:
    return f"{base_url(payload)}/firewalls/{payload.name}"


def acknowledgment(value: dict[str, Any], payload: RoutePayload, *, deleting: bool) -> tuple[str, str]:
    """Select matching receipt identity before status interpretation can fail."""
    try:
        expected_request = payload.delete_request_id if deleting else payload.create_request_id
        name = value.get("name")
        target = numeric_id(value.get("targetId"))
        path = f"/compute/v1/projects/{payload.identity.project_id}/global/firewalls/{payload.name}"
        if (
            value.get("kind") != "compute#operation"
            or type(name) is not str
            or OPERATION.fullmatch(name) is None
            or expected_request is None
            or value.get("clientOperationId") != expected_request
            or value.get("operationType") != ("delete" if deleting else "insert")
            or value.get("targetLink") not in tuple(f"{host}{path}" for host in HOSTS)
            or (deleting and target != payload.rule_id)
        ):
            raise ValueError("foreign operation")
        return name, target
    except (ValueError, TypeError, ValidationError):
        raise ValidationError("GCE route acknowledgment is invalid") from None


def completion(value: dict[str, Any], payload: RoutePayload, *, deleting: bool) -> ActivationState:
    """Require the same global Operation identity, then shared closed status rules."""
    try:
        name, target = acknowledgment(value, payload, deleting=deleting)
        expected = payload.delete_operation if deleting else payload.create_operation
        path = f"/compute/v1/projects/{payload.identity.project_id}/global/operations/{expected}"
        if (
            name != expected
            or target != payload.rule_id
            or "zone" in value
            or "region" in value
            or ("selfLink" in value and value["selfLink"] not in tuple(f"{host}{path}" for host in HOSTS))
        ):
            raise ValueError("foreign global operation")
        return operation_state(value)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ValidationError("GCE route completion is invalid") from None
