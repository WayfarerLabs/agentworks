"""Private one-shot exact-incarnation ARM start and explicit request settlement."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind
from agentworks.errors import AgentworksError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.azure._activation import OBLIGATION_KIND, StartAcknowledgment, decode_acknowledgment, start_url
from agentworks.plugins.azure._identity import canonical_vm_id, parse_resource_id
from agentworks.plugins.azure._incarnation_start_payload import (
    PAYLOAD_VERSION,
    TERMINAL_STATES,
    IncarnationStartPayload,
    StartDeadlineExpired,
    StartState,
    decode_start_payload,
    encode_start_payload,
    monitor_endpoint,
    observe_start_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _sdk[T](call: Callable[[], T]) -> T:
    """Detach SDK diagnostics while preserving domain and control exceptions."""
    failure_kind: str | None = None
    try:
        return call()
    except (AgentworksError, StartDeadlineExpired):
        raise
    except Exception as error:
        failure_kind = type(error).__name__
    raise StateError(f"Azure start request failed ({failure_kind})")


class AzureIncarnationStart:
    """Retain before start; caller owns credential and operation-owner lifetime.

    Each observation submits at most one GET. Public close is best effort local
    retirement, not proof of socket drain. SDK work cannot be hard preempted.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        credential: object,
        resource_id: str,
        vm_id: str,
        expected_locator: ProviderLocator,
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(expected_locator) is not ProviderLocator
        ):
            raise ValidationError("Azure start requires exact selected VM ownership")
        self._payload = IncarnationStartPayload(resource_id, canonical_vm_id(vm_id))
        encode_start_payload(self._payload)
        if expected_locator.token != f"azure-vm:v2:{self._payload.vm_id}:{resource_id}":
            raise ValidationError("Azure start locator does not match recorded incarnation")
        self._owner = owner
        self._credential = credential
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = False
        self._closed = False
        self._mark_began = False
        self._bookkeeping_uncertain = False
        self._resolution_began = False
        self._handles: tuple[Any, ...] = ()

    @property
    def payload(self) -> IncarnationStartPayload:
        return self._payload

    @property
    def state(self) -> StartState:
        if self._payload.terminal_status is not None:
            return self._payload.terminal_status
        return StartState.UNKNOWN if self._payload.acknowledgment is None else StartState.PENDING

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def obligation(self) -> LifecycleObligation | None:
        return self._obligation

    @property
    def cleanup_incomplete(self) -> bool:
        return bool(self._handles)

    @property
    def closed_without_start_attempt(self) -> bool:
        return self._closed and not self._attempted

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Azure start requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Azure start deadline expired")
        return remaining

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Azure start transition deadline expired")
        try:
            self._remaining(deadline)
            yield
        finally:
            self._lock.release()

    def _close_handles(self, deadline: Deadline) -> None:
        control: BaseException | None = None
        try:
            for handle in self._handles:
                try:
                    self._remaining(deadline)
                    handle.close()
                except Exception:
                    pass
                except BaseException as error:
                    if control is None:
                        control = error
                else:
                    self._handles = tuple(original for original in self._handles if original is not handle)
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    @contextmanager
    def _provider_custody(self, deadline: Deadline) -> Iterator[None]:
        control: BaseException | None = None
        try:
            try:
                yield
            except BaseException as error:
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                self._close_handles(deadline)
        except BaseException:
            if control is not None:
                raise control from None
            raise

    def _construct(self, deadline: Deadline) -> None:
        from agentworks.plugins.azure._activation_client import compute_start_client

        self._remaining(deadline)
        self._handles = (
            _sdk(lambda: compute_start_client(self._credential, parse_resource_id(self._payload.resource_id)[0])),
        )

    def _send(self, method: str, url: str, deadline: Deadline) -> Any:
        from azure.core.rest import HttpRequest

        client = self._handles[0]
        remaining = self._remaining(deadline)
        self._handles = (
            _sdk(
                lambda: client.send_request(
                    HttpRequest(method, url), stream=True, connection_timeout=remaining, read_timeout=remaining
                )
            ),
            client,
        )
        response = self._handles[0]
        return response

    def start(self, deadline: Deadline) -> StartAcknowledgment:
        """Register and mark before one POST, retaining late evidence before refusal."""
        with self._transition(deadline):
            if self._closed or self._attempted or self.cleanup_incomplete:
                raise StateError("Azure start cannot admit another request")
            self._attempted = True
            self._bookkeeping_uncertain = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=encode_start_payload(self._payload),
                obligation_id=self._obligation_id,
            )
            with self._provider_custody(deadline):
                self._construct(deadline)
                self._remaining(deadline)
                self._mark_began = True
                self._obligation.mark_possible_effect()
                self._bookkeeping_uncertain = False
                response = self._send("POST", start_url(self._payload.resource_id), deadline)
                ack = _sdk(lambda: decode_acknowledgment(response, self._payload.resource_id))
                terminal = StartState.SUCCEEDED if monitor_endpoint(ack) is None else None
                candidate = replace(self._payload, acknowledgment=ack, terminal_status=terminal)
                # Admission reserves space for the longest closed status too.
                encode_start_payload(replace(candidate, terminal_status=StartState.SUCCEEDED))
                self._payload = candidate
                self._reconcile(deadline)
            self._reconcile(deadline)
            self._remaining(deadline)
            return ack

    def observe(self, deadline: Deadline) -> StartState:
        """Observe only the exact retained monitor, once per explicit call."""
        with self._transition(deadline):
            ack = self._payload.acknowledgment
            if self._closed or self.cleanup_incomplete or self._bookkeeping_uncertain or ack is None:
                raise StateError("Azure start cannot admit an observation")
            if self.state in TERMINAL_STATES:
                return self.state
            endpoint = monitor_endpoint(ack)
            if endpoint is None:
                raise StateError("Azure start has no retained monitor")
            with self._provider_custody(deadline):
                self._construct(deadline)
                response = self._send("GET", endpoint, deadline)
                observed = _sdk(lambda: observe_start_response(response, ack, deadline))
                if observed in TERMINAL_STATES:
                    self._payload = replace(self._payload, terminal_status=observed)
                self._reconcile(deadline)
            self._reconcile(deadline)
            self._remaining(deadline)
            return self.state

    def close(self, deadline: Deadline) -> bool:
        """Terminally bar provider admission and retry original public closes only."""
        with self._transition(deadline):
            self._closed = True
            self._close_handles(deadline)
            self._remaining(deadline)
            return not self.cleanup_incomplete

    def settle(self, deadline: Deadline) -> bool:
        """Retry original cleanup and fenced evidence publication, never provider I/O."""
        with self._transition(deadline):
            self._close_handles(deadline)
            settled = self._reconcile(deadline)
            self._remaining(deadline)
            return settled

    def _reconcile(self, deadline: Deadline) -> bool:
        self._remaining(deadline)
        if not self._attempted:
            return self._closed and not self.cleanup_incomplete
        self._bookkeeping_uncertain = True
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        self._remaining(deadline)
        if row is None:
            if self._mark_began:
                raise StateError("Azure start registration remains uncertain")
            self._bookkeeping_uncertain = False
            return not self.cleanup_incomplete
        initial = replace(self._payload, acknowledgment=None, terminal_status=None)
        acknowledged = replace(self._payload, terminal_status=None)
        expected = {0: initial}
        if self._payload.acknowledgment is not None:
            expected[1] = acknowledged
        if self._payload.terminal_status is not None:
            expected[2] = self._payload
        if (
            row.obligation_kind != OBLIGATION_KIND
            or row.payload_version != PAYLOAD_VERSION
            or row.payload_revision not in expected
            or decode_start_payload(row.payload) != expected[row.payload_revision]
        ):
            raise StateError("Azure start custody conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolution_began or row.payload_revision != max(expected):
                raise StateError("Azure start custody was prematurely resolved")
            self._bookkeeping_uncertain = False
            return True
        if row.state is LifecycleObligationState.POSSIBLE_EFFECT:
            for revision in range(row.payload_revision + 1, max(expected) + 1):
                self._remaining(deadline)
                self._obligation.publish_payload(
                    expected_revision=revision - 1,
                    payload_version=PAYLOAD_VERSION,
                    payload=encode_start_payload(expected[revision]),
                )
        no_effect = row.state is LifecycleObligationState.REGISTERED and not self._mark_began
        terminal = row.state is LifecycleObligationState.POSSIBLE_EFFECT and self._payload.terminal_status is not None
        if not self.cleanup_incomplete and (no_effect or terminal):
            self._remaining(deadline)
            self._resolution_began = True
            self._obligation.resolve()
            self._bookkeeping_uncertain = False
            return True
        self._bookkeeping_uncertain = False
        return False
