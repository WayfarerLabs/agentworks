"""Private one-shot GCE start admission and matching acknowledgment custody."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import Any
from uuid import UUID, uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner

OBLIGATION_KIND = "gcp-gce-start"
PAYLOAD_VERSION = 1
MAX_BODY_BYTES = 65_536
READ_CHUNK_BYTES = 8_192
_COMPONENT = re.compile(r"[a-z0-9-]{1,63}")
_OPERATION = re.compile(r"[A-Za-z0-9_-]{1,512}")


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


def decode_acknowledgment(body: bytes, payload: ActivationPayload) -> str:
    """Select matching provider Operation identity without interpreting startup."""
    try:
        if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
            raise ValueError("oversized body")
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        path = f"/compute/v1/projects/{payload.project_id}/zones/{payload.zone}/instances/{payload.instance_name}"
        if (
            type(value) is not dict
            or value.get("kind") != "compute#operation"
            or type(value.get("name")) is not str
            or _OPERATION.fullmatch(value["name"]) is None
            or value.get("clientOperationId") != payload.request_id
            or value.get("targetId") != payload.instance_id
            or value.get("targetLink")
            not in (f"https://compute.googleapis.com{path}", f"https://www.googleapis.com{path}")
            or ("operationType" in value and value["operationType"] != "start")
        ):
            raise ValueError("foreign acknowledgment")
        operation_name: str = value["name"]
        return operation_name
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("GCE activation acknowledgment is invalid") from None


class GCEActivation:
    """Caller-retained passive admission, never startup or request-drain settlement.

    Start addresses the recorded name, not an atomic incarnation condition.
    Synchronous SDK/credential budgets cannot promise hard elapsed preemption.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        credential: object,
        project_id: str,
        zone: str,
        instance_name: str,
        instance_id: str,
        expected_locator: ProviderLocator,
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(expected_locator) is not ProviderLocator
        ):
            raise ValidationError("GCE activation requires exact selected VM ownership")
        self._payload = ActivationPayload(project_id, zone, instance_name, instance_id, str(uuid4()))
        encode_activation_payload(self._payload)
        if expected_locator.token != f"gcp-gce:{project_id}:{zone}:{instance_id}":
            raise ValidationError("GCE activation locator does not match selected identity")
        self._owner = owner
        self._credential = credential
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._mark_began = False
        self._lock = Lock()
        # These are the actual returned originals, not close proxies or copied handles.
        self._response: Any = None
        self._service_session: Any = None
        self._credential_session: Any = None
        # Request.__del__ closes its session. Keep the wrapper for this adapter's lifetime.
        self._auth_request: Any = None

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
        """An original local handle still lacks confirmed normal closure."""
        return any(handle is not None for handle in (self._response, self._service_session, self._credential_session))

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("GCE activation requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("GCE activation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("GCE activation transition deadline expired")

    def _retain_response(self, response: Any, **kwargs: Any) -> None:
        """Public hook captures custody before Requests can consume an unwanted body."""
        self._response = response
        encodings = [value for key, value in response.headers.items() if key.lower() == "content-encoding"]
        if (
            type(response.status_code) is not int
            or response.status_code != 200
            or len(encodings) > 1
            or (encodings and (type(encodings[0]) is not str or encodings[0].strip().lower() != "identity"))
        ):
            raise ValidationError("GCE activation response is invalid")

    def _read_body(self, deadline: Deadline) -> bytes:
        body = bytearray()
        while True:
            self._remaining(deadline)
            amount = min(READ_CHUNK_BYTES, MAX_BODY_BYTES - len(body) + 1)
            chunk = self._response.raw.read(amount, decode_content=False)
            if type(chunk) is not bytes or len(chunk) > amount:
                raise ValidationError("GCE activation body read is invalid")
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ValidationError("GCE activation body exceeds its ingestion bound")
            # Public stream closure or EOF establishes the entire body, even if this read was late.
            if not chunk or self._response.raw.isclosed():
                return bytes(body)

    def _close_handles(self) -> None:
        control: KeyboardInterrupt | SystemExit | None = None
        try:
            for attribute in ("_response", "_service_session", "_credential_session"):
                handle = getattr(self, attribute)
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:
                    pass
                except (KeyboardInterrupt, SystemExit) as error:
                    if control is None:
                        control = error
                else:
                    setattr(self, attribute, None)
        except (KeyboardInterrupt, SystemExit):
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    def start(self, deadline: Deadline) -> str:
        """Admit one POST and retain its matching ACK before publication or cleanup."""
        self._acquire(deadline)
        control: KeyboardInterrupt | SystemExit | None = None
        try:
            self._remaining(deadline)
            if self._attempted:
                raise StateError("GCE activation was already attempted")
            self._attempted = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
                obligation_id=self._obligation_id,
            )
            import requests  # type: ignore[import-untyped]
            from google.auth.transport.requests import AuthorizedSession, Request

            try:
                self._remaining(deadline)
                self._credential_session = requests.Session()
                self._credential_session.trust_env = False
                self._auth_request = Request(session=self._credential_session)
                remaining = self._remaining(deadline)
                self._service_session = AuthorizedSession(  # type: ignore[no-untyped-call]
                    self._credential,
                    max_refresh_attempts=0,
                    refresh_timeout=remaining,
                    auth_request=self._auth_request,
                )
                self._service_session.trust_env = False
                self._remaining(deadline)
                self._mark_began = True
                self._obligation.mark_possible_effect()
                remaining = self._remaining(deadline)
                self._service_session.request(
                    "POST",
                    start_url(self._payload),
                    stream=True,
                    allow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                    timeout=(remaining, remaining),
                    hooks={"response": self._retain_response},
                )
                operation_name = decode_acknowledgment(self._read_body(deadline), self._payload)
                self._payload = replace(self._payload, operation_name=operation_name)
                self._reconcile_locked()
            except (KeyboardInterrupt, SystemExit) as error:
                control = error
                raise
            finally:
                self._close_handles()
            self._remaining(deadline)
            return operation_name
        except (KeyboardInterrupt, SystemExit):
            if control is not None:
                raise control from None
            raise
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Retry exact fenced ledger bookkeeping without provider work or close retries."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("GCE activation has no registration attempt")
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            raise StateError("GCE activation registration remains uncertain")
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("GCE activation custody conflicts")
        durable = decode_activation_payload(row.payload)
        initial = replace(self._payload, operation_name=None)
        if not (
            (durable == initial and row.payload_revision == 0)
            or (durable == self._payload and self._payload.operation_name is not None and row.payload_revision == 1)
        ):
            raise StateError("GCE activation custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED and self._mark_began:
            raise StateError("GCE activation custody was prematurely resolved")
        if row.state is LifecycleObligationState.REGISTERED and not self._mark_began:
            self._obligation.resolve()
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT and durable != self._payload:
            self._obligation.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
            )
