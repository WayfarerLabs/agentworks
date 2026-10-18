"""Retained one-shot GCE start, explicit completion observation and local settlement."""

from __future__ import annotations

from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation_operation import (
    MAX_BODY_BYTES as MAX_BODY_BYTES,
)
from agentworks.plugins.gcp._activation_operation import (
    ActivationPayload as ActivationPayload,
)
from agentworks.plugins.gcp._activation_operation import (
    ActivationState as ActivationState,
)
from agentworks.plugins.gcp._activation_operation import (
    decode_acknowledgment as decode_acknowledgment,
)
from agentworks.plugins.gcp._activation_operation import (
    decode_activation_payload as decode_activation_payload,
)
from agentworks.plugins.gcp._activation_operation import (
    decode_completion,
    operation_url,
)
from agentworks.plugins.gcp._activation_operation import (
    encode_activation_payload as encode_activation_payload,
)
from agentworks.plugins.gcp._activation_operation import (
    start_url as start_url,
)

if TYPE_CHECKING:
    from agentworks.plugins.gcp._owned_auth import _OwnedAuth

OBLIGATION_KIND = "gcp-gce-start"
PAYLOAD_VERSION = 1
READ_CHUNK_BYTES = 8_192


class GCEActivation:
    """Caller-retained request admission, observation and custody settlement.

    Start addresses the recorded name, not an atomic incarnation condition.
    Synchronous SDK/credential budgets cannot promise hard elapsed preemption.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        auth: _OwnedAuth,
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
        self._auth = auth
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._mark_began = False
        self._state = ActivationState.UNKNOWN
        self._closed = False
        self._resolution_intended = False
        self._settled = False
        self._lock = Lock()
        # These are the actual returned originals, not close proxies or copied handles.
        self._response: Any = None
        self._service_session: Any = None

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
    def state(self) -> ActivationState:
        """Retained request knowledge, not VM or guest readiness."""
        return self._state

    @property
    def cleanup_incomplete(self) -> bool:
        """An original local handle still lacks confirmed normal closure."""
        return any(handle is not None for handle in (self._response, self._service_session))

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("GCE activation requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise LimitExceededError("GCE activation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise LimitExceededError("GCE activation transition deadline expired")

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
        control: BaseException | None = None
        try:
            for attribute in ("_response", "_service_session"):
                handle = getattr(self, attribute)
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:
                    pass
                except BaseException as error:
                    if control is None:
                        control = error
                else:
                    setattr(self, attribute, None)
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    def start(self, deadline: Deadline) -> str:
        """Admit one POST and retain its matching ACK before publication or cleanup."""
        self._request(deadline, observing=False)
        assert self._payload.operation_name is not None
        return self._payload.operation_name

    def observe(self, deadline: Deadline) -> ActivationState:
        """Admit at most one selected Operation GET, retaining validated terminal state."""
        self._request(deadline, observing=True)
        return self._state

    def _request(self, deadline: Deadline, *, observing: bool) -> None:
        self._acquire(deadline)
        control: BaseException | None = None
        auth_begun = False
        try:
            self._remaining(deadline)
            if self._closed or self.cleanup_incomplete:
                raise StateError("GCE activation service admission is closed")
            if observing:
                if self._state is ActivationState.UNKNOWN:
                    raise StateError("GCE activation has no acknowledged operation")
                if self._state in (ActivationState.SUCCEEDED, ActivationState.FAILED):
                    return
                self._reconcile_locked()
            else:
                if self._attempted:
                    raise StateError("GCE activation was already attempted")
                self._attempted = True
                self._obligation = self._owner.register_lifecycle_obligation(
                    OBLIGATION_KIND,
                    payload_version=PAYLOAD_VERSION,
                    payload=encode_activation_payload(self._payload),
                    obligation_id=self._obligation_id,
                )
            method = "GET" if observing else "POST"
            url = operation_url(self._payload) if observing else start_url(self._payload)
            import requests  # type: ignore[import-untyped]

            try:
                self._remaining(deadline)
                self._auth.begin(deadline)
                auth_begun = True
                self._auth.acquire()
                headers = self._auth.apply_headers(method, url)
                self._service_session = requests.Session()
                self._service_session.trust_env = False
                self._remaining(deadline)
                if not observing:
                    self._mark_began = True
                    assert self._obligation is not None
                    self._obligation.mark_possible_effect()
                remaining = self._remaining(deadline)
                self._auth.remaining()
                self._service_session.request(
                    method,
                    url,
                    stream=True,
                    allow_redirects=False,
                    headers=headers,
                    timeout=(remaining, remaining),
                    hooks={"response": self._retain_response},
                )
                body = self._read_body(deadline)
                if observing:
                    self._state = decode_completion(body, self._payload)
                else:
                    operation_name = decode_acknowledgment(body, self._payload)
                    self._payload = replace(self._payload, operation_name=operation_name)
                    self._state = ActivationState.PENDING
                self._reconcile_locked()
            except BaseException as error:
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                self._close_handles()
            self._remaining(deadline)
        except BaseException as error:
            if control is not None:
                raise control from None
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                if auth_begun:
                    self._auth.retire_responses()
            except BaseException as error:
                if control is not None:
                    raise control from None
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                try:
                    if auth_begun:
                        self._auth.end()
                except BaseException as error:
                    if control is not None:
                        raise control from None
                    if not isinstance(error, Exception):
                        control = error
                    raise
                finally:
                    try:
                        self._lock.release()
                    except BaseException:
                        if control is not None:
                            raise control from None
                        raise

    def close(self, deadline: Deadline) -> bool:
        """Bar service admission, then retire only this borrower's service originals."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._closed = True
            self._close_handles()
            self._remaining(deadline)
            return not self.cleanup_incomplete
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise

        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise

    def settle(self, deadline: Deadline) -> bool:
        """Retire local originals and resolve proved custody without provider work."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            if self._settled:
                return True
            self._close_handles()
            self._remaining(deadline)
            if not self._attempted:
                self._settled = self._closed and not self.cleanup_incomplete
                return self._settled
            self._reconcile_locked(settling=True)
            self._remaining(deadline)
            return self._settled
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise

    def reconcile(self, deadline: Deadline) -> None:
        """Retry exact fenced ledger bookkeeping without provider work or close retries."""
        self._acquire(deadline)
        try:
            self._remaining(deadline)
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self, *, settling: bool = False) -> None:
        if self._settled:
            return
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
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolution_intended:
                raise StateError("GCE activation custody was prematurely resolved")
            self._settled = True
            return
        if row.state is LifecycleObligationState.REGISTERED and not self._mark_began and not self.cleanup_incomplete:
            self._resolution_intended = True
            self._obligation.resolve()
            self._settled = True
        elif row.state is LifecycleObligationState.POSSIBLE_EFFECT and durable != self._payload:
            self._obligation.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=PAYLOAD_VERSION,
                payload=encode_activation_payload(self._payload),
            )
        if (
            settling
            and row.state is LifecycleObligationState.POSSIBLE_EFFECT
            and self._state in (ActivationState.SUCCEEDED, ActivationState.FAILED)
            and not self.cleanup_incomplete
        ):
            self._resolution_intended = True
            self._obligation.resolve()
            self._settled = True
