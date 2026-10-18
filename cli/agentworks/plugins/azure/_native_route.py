"""One operation-owned Azure native SSH rule, with explicit foreground cleanup.

Parent identity and nonce checks are observations under cooperating operators,
not atomic provider incarnation preconditions. Removing an NSG rule does not
terminate already established flows. The caller owns the borrowed credential.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from ipaddress import IPv4Network
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import AgentworksError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.azure._identity import canonical_vm_id, observed_resource_id_equal, parse_resource_id
from agentworks.plugins.azure._native_route_evidence import (
    TERMINAL,
    RouteDeadlineExpired,
    RuleAcknowledgment,
    acknowledgment,
    json_body,
    monitor_state,
    resource_url,
    response_identity,
)
from agentworks.plugins.azure._native_route_topology import (
    RouteTopology,
    observe_nic,
    observe_nsg,
    properties,
    rule_inventory,
    rule_properties,
    select_priority,
    validate_rule,
    vm_nic,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

KIND = "azure-native-ssh-route"
PAYLOAD_VERSION = 1


def _sdk[T](call: Callable[[], T]) -> T:
    failure: str | None = None
    try:
        return call()
    except (AgentworksError, RouteDeadlineExpired):
        raise
    except Exception as error:
        failure = type(error).__name__
    raise StateError(f"Azure route request failed ({failure})")


class AzureNativeRoute:
    """Passive selection retained before I/O; open once, settle only its effect.

    Each request uses the public streamed SDK method with no replay middleware.
    Retain actual returned originals before another call; successful public
    close is local retirement, not physical socket-drain proof. SDK construction,
    credential acquisition, reads and closes remain synchronously best effort.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        credential: object,
        resource_id: str,
        vm_id: str,
        expected_locator: ProviderLocator,
        observed_vm: object,
        prefixes: tuple[str, ...],
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(expected_locator) is not ProviderLocator
        ):
            raise ValidationError("Azure route requires exact selected VM ownership")
        self._subscription, _, _ = parse_resource_id(resource_id)
        self._vm_id = canonical_vm_id(vm_id)
        if expected_locator.token != f"azure-vm:v2:{self._vm_id}:{resource_id}":
            raise ValidationError("Azure route locator does not match recorded incarnation")
        try:
            valid = (
                type(prefixes) is tuple
                and 0 < len(prefixes) <= 64
                and all(type(prefix) is str and str(IPv4Network(prefix)) == prefix for prefix in prefixes)
                and len(set(prefixes)) == len(prefixes)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValidationError("Azure route source prefixes are invalid")
        self._nic_id = vm_nic(observed_vm, resource_id, self._vm_id)
        self._owner, self._vm_name, self._credential = owner, vm_name, credential
        self._resource_id, self._prefixes = resource_id, prefixes
        self._nonce = uuid4().hex
        self._name = f"allow-ssh-native-{self._nonce}"
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._topology: RouteTopology | None = None
        self._rule: dict[str, Any] | None = None
        self._create_ack: RuleAcknowledgment | None = None
        self._delete_ack: RuleAcknowledgment | None = None
        self._create_state: str | None = None
        self._delete_state: str | None = None
        self._history: list[bytes] = []
        self._attempted = self._closed = self._settling = False
        self._register_began = self._put_began = self._delete_began = False
        self._delete_intent = False
        self._resolution_began = self._absent = False
        self._evidence_overflow = False
        self._handles: tuple[Any, ...] = ()
        self._lock = Lock()

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def cleanup_incomplete(self) -> bool:
        """Whether a returned original still lacks a positive public close."""
        return bool(self._handles)

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Azure route requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise RouteDeadlineExpired("Azure route deadline expired")
        return remaining

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise RouteDeadlineExpired("Azure route transition deadline expired")
        try:
            self._remaining(deadline)
            yield
        finally:
            self._lock.release()

    def _close_handles(self, deadline: Deadline) -> None:
        control: BaseException | None = None
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
        if control is not None:
            raise control

    @contextmanager
    def _custody(self, deadline: Deadline) -> Iterator[None]:
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
        from agentworks.plugins.azure._route_client import network_route_client

        self._close_handles(deadline)
        self._remaining(deadline)
        if self.cleanup_incomplete:
            raise StateError("Azure route retains an unretired original")
        self._handles = (_sdk(lambda: network_route_client(self._credential, self._subscription)),)

    def _send(self, request: Any, deadline: Deadline) -> Any:
        client = self._handles[0]
        remaining = self._remaining(deadline)
        self._handles = (
            _sdk(
                lambda: client.send_request(request, stream=True, connection_timeout=remaining, read_timeout=remaining)
            ),
            client,
        )
        return self._handles[0]

    def _get(self, resource_id: str, deadline: Deadline, *, missing: bool = False) -> dict[str, Any] | None:
        from azure.core.rest import HttpRequest

        self._construct(deadline)
        url = resource_url(resource_id)
        response = self._send(HttpRequest("GET", url), deadline)
        status = _sdk(lambda: response_identity(response, "GET", url, (200, 404) if missing else (200,)))
        return None if status == 404 else _sdk(lambda: json_body(response, lambda: self._remaining(deadline)))

    def _observe_topology(self, deadline: Deadline) -> dict[str, Any]:
        nic = self._get(self._nic_id, deadline)
        assert nic is not None
        nsg_id, nic_guid = observe_nic(nic, self._nic_id, self._resource_id)
        if self._topology is not None and (
            nic_guid != self._topology.nic_guid
            or not observed_resource_id_equal(nsg_id, self._topology.nsg_id, "networkSecurityGroups")
        ):
            raise StateError("Azure route NIC incarnation or association changed")
        nsg = self._get(nsg_id, deadline)
        assert nsg is not None
        topology = RouteTopology(
            self._nic_id, nic_guid, nsg_id, observe_nsg(nsg, self._nic_id, nsg_id, self._resource_id)
        )
        if self._topology is None:
            self._topology = topology
        elif (
            topology.nic_guid != self._topology.nic_guid
            or topology.nsg_guid != self._topology.nsg_guid
            or not observed_resource_id_equal(topology.nsg_id, self._topology.nsg_id, "networkSecurityGroups")
        ):
            raise StateError("Azure route parent incarnation or association changed")
        return nsg

    def _rule_id(self) -> str:
        assert self._topology is not None
        return f"{self._topology.nsg_id}/securityRules/{self._name}"

    def _record(self) -> None:
        assert self._topology is not None and self._rule is not None
        payload = json.dumps(
            {
                "resource_id": self._resource_id,
                "vm_id": self._vm_id,
                "topology": asdict(self._topology),
                "name": self._name,
                "nonce": self._nonce,
                "rule": self._rule,
                "create_ack": None if self._create_ack is None else asdict(self._create_ack),
                "create_state": self._create_state,
                "delete_intent": self._delete_intent,
                "delete_ack": None if self._delete_ack is None else asdict(self._delete_ack),
                "delete_state": self._delete_state,
                "absent": self._absent,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            self._evidence_overflow = True
            raise ValidationError("Azure route evidence exceeds its storage bound")
        if not self._history or self._history[-1] != payload:
            self._history.append(payload)

    def _reconcile(self, deadline: Deadline, *, admitting: bool = False) -> bool:
        self._remaining(deadline)
        if self._evidence_overflow:
            raise StateError("Azure route retains evidence beyond its ledger bound")
        if not self._register_began:
            return not self.cleanup_incomplete
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        self._remaining(deadline)
        if row is None:
            if self._put_began:
                raise StateError("Azure route registration remains uncertain")
            return not self.cleanup_incomplete
        if (
            row.obligation_kind != KIND
            or row.payload_version != PAYLOAD_VERSION
            or not 0 <= row.payload_revision < len(self._history)
            or row.payload != self._history[row.payload_revision]
            or (self._put_began and row.state is LifecycleObligationState.REGISTERED)
        ):
            raise StateError("Azure route custody conflicts with retained evidence")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolution_began or row.payload_revision != len(self._history) - 1:
                raise StateError("Azure route custody was prematurely resolved")
            return not self.cleanup_incomplete
        if row.state is LifecycleObligationState.POSSIBLE_EFFECT:
            for revision in range(row.payload_revision + 1, len(self._history)):
                self._remaining(deadline)
                self._obligation.publish_payload(
                    expected_revision=revision - 1, payload_version=PAYLOAD_VERSION, payload=self._history[revision]
                )
        if not admitting and not self.cleanup_incomplete and (not self._put_began or self._absent):
            self._remaining(deadline)
            self._resolution_began = True
            self._obligation.resolve()
            return True
        return False

    def _poll(self, deadline: Deadline, *, deleting: bool = False) -> None:
        from azure.core.rest import HttpRequest

        ack = self._delete_ack if deleting else self._create_ack
        if ack is None:
            return
        while (self._delete_state if deleting else self._create_state) not in TERMINAL:
            if ack.monitor is None:
                raise StateError("Azure route request lacks monitorable completion evidence")
            self._construct(deadline)
            response = self._send(HttpRequest("GET", ack.monitor), deadline)

            def observe(response: Any = response) -> str:
                return monitor_state(response, ack, lambda: self._remaining(deadline))

            state = _sdk(observe)
            if deleting:
                self._delete_state = state
            else:
                self._create_state = state
            self._record()
            self._reconcile(deadline)
            self._remaining(deadline)
            if state not in TERMINAL:
                time.sleep(min(0.25, self._remaining(deadline)))

    def open(self, deadline: Deadline) -> None:
        """Register and fence one PUT, then foreground-poll its exact monitor."""
        from azure.core.rest import HttpRequest

        with self._transition(deadline):
            if self._closed or self._settling or self._attempted or self.cleanup_incomplete:
                raise StateError("Azure route opening is single use")
            self._attempted = True
            with self._custody(deadline):
                nsg = self._observe_topology(deadline)
                self._rule = rule_properties(select_priority(nsg, self._name), self._nonce, self._prefixes)
                self._record()
                request = HttpRequest("PUT", resource_url(self._rule_id()), json={"properties": self._rule})
                self._register_began = True
                self._obligation = self._owner.register_lifecycle_obligation(
                    KIND, payload_version=PAYLOAD_VERSION, payload=self._history[0], obligation_id=self._obligation_id
                )
                self._construct(deadline)
                self._reconcile(deadline, admitting=True)
                self._obligation.mark_possible_effect()
                self._put_began = True
                response = self._send(request, deadline)
                self._create_ack = _sdk(lambda: acknowledgment(response, "PUT", request.url, self._subscription))
                self._record()
                if self._create_ack.monitor is None:
                    value = _sdk(lambda: json_body(response, lambda: self._remaining(deadline)))
                    validate_rule(value, self._rule_id(), self._name, self._rule, completed_failure=True)
                    self._create_state = properties(value)["provisioningState"]
                    self._record()
                self._reconcile(deadline)
                self._poll(deadline)
                if self._create_state != "Succeeded":
                    raise StateError("Azure route creation did not succeed")
                self._verify_rule(deadline)
                self._remaining(deadline)
            self._remaining(deadline)

    def _verify_rule(self, deadline: Deadline, *, cleanup: bool = False) -> bool:
        nsg = self._observe_topology(deadline)
        if not cleanup:
            rule_inventory(nsg)
        value = self._get(self._rule_id(), deadline, missing=cleanup)
        if value is None:
            return False
        assert self._rule is not None
        validate_rule(value, self._rule_id(), self._name, self._rule, completed_failure=cleanup)
        return True

    def check(self, deadline: Deadline, *, observed_vm: object) -> None:
        """Recheck only the retained route and its parent association."""
        with self._transition(deadline):
            if self._closed or self._settling or self.cleanup_incomplete or not self._put_began:
                raise StateError("Azure route cannot admit a check")
            if not observed_resource_id_equal(
                vm_nic(observed_vm, self._resource_id, self._vm_id), self._nic_id, "networkInterfaces"
            ):
                raise StateError("Azure route VM network association changed")
            with self._custody(deadline):
                self._reconcile(deadline)
                self._poll(deadline)
                if self._create_state != "Succeeded":
                    raise StateError("Azure route creation did not succeed")
                self._verify_rule(deadline)
                self._remaining(deadline)
            self._remaining(deadline)

    def settle(self, deadline: Deadline) -> bool:
        """Finish acknowledged requests and remove only this operation's rule."""
        from azure.core.rest import HttpRequest

        with self._transition(deadline):
            self._settling = True
            self._close_handles(deadline)
            if self.cleanup_incomplete:
                return False
            if self._reconcile(deadline):
                return True
            if self._create_ack is None:
                return False
            with self._custody(deadline):
                self._poll(deadline)
                if self._create_state not in TERMINAL:
                    return False
                if not self._delete_began:
                    if self._verify_rule(deadline, cleanup=True):
                        self._construct(deadline)
                        request = HttpRequest("DELETE", resource_url(self._rule_id()))
                        self._delete_intent = True
                        self._record()
                        self._reconcile(deadline)
                        self._remaining(deadline)
                        self._delete_began = True
                        response = self._send(request, deadline)
                        self._delete_ack = _sdk(
                            lambda: acknowledgment(response, "DELETE", request.url, self._subscription)
                        )
                        self._delete_state = "Succeeded" if self._delete_ack.monitor is None else None
                        self._record()
                        self._reconcile(deadline)
                    else:
                        self._absent = True
                        self._record()
                if self._delete_began:
                    if self._delete_ack is None:
                        return False
                    self._poll(deadline, deleting=True)
                    if self._delete_state not in TERMINAL:
                        return False
                    self._observe_topology(deadline)
                    value = self._get(self._rule_id(), deadline, missing=True)
                    if value is not None:
                        assert self._rule is not None
                        validate_rule(value, self._rule_id(), self._name, self._rule, completed_failure=True)
                        return False
                    self._absent = True
                    self._record()
            return self._reconcile(deadline)

    def close(self, deadline: Deadline) -> bool:
        """Terminally bar open/check; retry actual original closes without I/O."""
        with self._transition(deadline):
            self._closed = True
            self._close_handles(deadline)
            self._remaining(deadline)
            return not self.cleanup_incomplete
