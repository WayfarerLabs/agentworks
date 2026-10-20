"""One retained Azure resource set, literal ARM requests and exact rollback."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

from agentworks.db import LifecycleObligationState
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import AgentworksError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation
from agentworks.plugins.azure._identity import canonical_vm_id, resource_name_equal
from agentworks.plugins.azure._native_route_evidence import (
    TERMINAL,
    acknowledgment,
    json_body,
    monitor_state,
    response_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.operations import OperationOwner
    from agentworks.plugins.azure._creation_resources import CreationResource
    from agentworks.plugins.azure._native_route_evidence import RuleAcknowledgment

KIND = "azure-created-resources"


class CreationDeadlineExpired(TimeoutError):
    """Core-authored expiry, distinguished from SDK exception diagnostics."""


def remaining(deadline: Deadline) -> float:
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("Azure creation requires a finite deadline")
    value = deadline.remaining()
    assert value is not None
    if value <= 0:
        raise CreationDeadlineExpired("Azure creation deadline expired")
    return value


def _sdk[T](call: Callable[[], T]) -> T:
    failure: str | None = None
    try:
        return call()
    except (AgentworksError, CreationDeadlineExpired):
        raise
    except Exception as error:
        failure = type(error).__name__
    raise StateError(f"Azure creation request failed ({failure})")


def resource_url(resource_id: str, *, expand: bool = False) -> str:
    version = "2025-04-01" if "/Microsoft.Compute/" in resource_id else "2025-09-01"
    path = "/".join(quote(part, safe="") for part in resource_id.split("/"))
    return f"https://management.azure.com{path}?api-version={version}" + ("&$expand=instanceView" if expand else "")


@dataclass(slots=True)
class ResourceEvidence:
    resource_id: str
    request_sha256: str
    sent: bool = False
    create_ack: RuleAcknowledgment | None = None
    create_state: str | None = None
    identity: str | None = None
    delete_sent: bool = False
    delete_ack: RuleAcknowledgment | None = None
    delete_state: str | None = None
    absent: bool = False


class AzureCreationRequests:
    """Serialize under the access lock; retain every returned original before use.

    Unknown PUT replies never authorize name-based adoption or deletion. A known
    receipt permits its monitor and exact completion read. Public close proves local handle
    retirement, not physical socket drainage or hard SDK preemption.
    """

    def __init__(
        self, owner: OperationOwner, subscription: str, plan: tuple[CreationResource, ...], disk_id: str
    ) -> None:
        self.owner, self.subscription, self.plan, self.disk_id = owner, subscription, plan, disk_id
        self.observed_disk_id: str | None = None
        self.resources = [
            ResourceEvidence(
                item.resource_id, hashlib.sha256(json.dumps(item.body, sort_keys=True).encode()).hexdigest()
            )
            for item in plan
        ]
        self.accepted = False
        self._id = uuid4().hex
        self._history: list[bytes] = []
        self._registered = self._marked = self._resolving = False
        self._handles: tuple[Any, ...] = ()

    @contextmanager
    def custody(self, deadline: Deadline) -> Iterator[None]:
        control: BaseException | None = None
        try:
            try:
                yield
            except BaseException as error:
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                self.close(deadline)
        except BaseException:
            if control is not None:
                raise control from None
            raise

    @property
    def cleanup_incomplete(self) -> bool:
        return bool(self._handles)

    def close(self, deadline: Deadline) -> bool:
        control: BaseException | None = None
        for handle in self._handles:
            try:
                remaining(deadline)
                handle.close()
            except Exception:
                pass
            except BaseException as error:
                if control is None:
                    control = error
            else:
                self._handles = tuple(item for item in self._handles if item is not handle)
        if control is not None:
            raise control
        return not self._handles

    def _record(self) -> None:
        payload = json.dumps(
            {
                "resources": [asdict(item) for item in self.resources],
                "disk_id": self.disk_id,
                "observed_disk_id": self.observed_disk_id,
                "accepted": self.accepted,
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValidationError("Azure creation evidence exceeds lifecycle bound")
        if not self._history or payload != self._history[-1]:
            self._history.append(payload)

    def reconcile(self, deadline: Deadline, *, resolve: bool = False) -> bool:
        remaining(deadline)
        self._record()
        if not self._registered:
            return not self.cleanup_incomplete
        row = self.owner.inspect_lifecycle_obligation(self._id)
        if row is None:
            if self._marked:
                raise StateError("Azure creation registration remains uncertain")
            return not self.cleanup_incomplete
        if (
            row.obligation_kind != KIND
            or row.payload_version != 1
            or not 0 <= row.payload_revision < len(self._history)
            or row.payload != self._history[row.payload_revision]
            or (any(item.sent for item in self.resources) and row.state is LifecycleObligationState.REGISTERED)
        ):
            raise StateError("Azure creation lifecycle custody conflicts")
        obligation = LifecycleObligation(self.owner, row)
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolving or row.payload_revision != len(self._history) - 1:
                raise StateError("Azure creation lifecycle custody was prematurely resolved")
            return not self.cleanup_incomplete
        if row.state is LifecycleObligationState.POSSIBLE_EFFECT:
            for revision in range(row.payload_revision + 1, len(self._history)):
                remaining(deadline)
                obligation.publish_payload(
                    expected_revision=revision - 1, payload_version=1, payload=self._history[revision]
                )
        if resolve and not self.cleanup_incomplete:
            remaining(deadline)
            self._resolving = True
            obligation.resolve()
            return True
        return False

    def register(self, deadline: Deadline) -> None:
        self.owner.list_pending_lifecycle_obligations()
        remaining(deadline)
        self._record()
        self._registered = True
        self.owner.register_lifecycle_obligation(
            KIND, payload_version=1, payload=self._history[0], obligation_id=self._id
        )

    def call(
        self,
        credential: object,
        method: str,
        url: str,
        deadline: Deadline,
        *,
        body: dict[str, Any] | None = None,
        create_only: bool = False,
    ) -> Any:
        from azure.core.rest import HttpRequest

        from agentworks.plugins.azure._route_client import network_route_client

        self.close(deadline)
        if self.cleanup_incomplete:
            raise StateError("Azure creation retains an unretired original")
        self.owner.list_pending_lifecycle_obligations()
        remaining(deadline)
        self._handles = (_sdk(lambda: network_route_client(credential, self.subscription)),)
        client = self._handles[0]
        request = HttpRequest(method, url, json=body, headers={"If-None-Match": "*"} if create_only else {})
        budget = remaining(deadline)
        self._handles = (
            _sdk(lambda: client.send_request(request, stream=True, connection_timeout=budget, read_timeout=budget)),
            client,
        )
        return self._handles[0]

    def get(
        self, credential: object, resource_id: str, deadline: Deadline, *, expand: bool = False
    ) -> dict[str, Any] | None:
        url = resource_url(resource_id, expand=expand)
        response = self.call(credential, "GET", url, deadline)
        status = _sdk(lambda: response_identity(response, "GET", url, (200, 404)))
        if status == 404:
            return None
        value = _sdk(lambda: json_body(response, lambda: remaining(deadline)))
        observed = value.get("id")
        if type(observed) is not str or not resource_name_equal(observed, resource_id):
            raise StateError("Azure creation observed a different resource path")
        return value

    @staticmethod
    def identity(value: dict[str, Any], resource_id: str) -> str:
        props = value.get("properties")
        if type(props) is not dict:
            raise ValidationError("Azure creation resource properties are invalid")
        return canonical_vm_id(props.get("vmId" if "/virtualMachines/" in resource_id else "resourceGuid"))

    def verify(self, item: ResourceEvidence, value: dict[str, Any]) -> None:
        expected = self.plan[self.resources.index(item)].body["tags"]
        if value.get("tags") != expected or self.identity(value, item.resource_id) != item.identity:
            raise StateError("Azure created resource identity or ownership changed")

    def _associate(self, item: ResourceEvidence, value: dict[str, Any]) -> None:
        if type(value.get("id")) is not str or not resource_name_equal(value["id"], item.resource_id):
            raise StateError("Azure creation returned a foreign resource")
        identity = self.identity(value, item.resource_id)
        if item.identity is not None and item.identity != identity:
            raise StateError("Azure creation resource incarnation changed")
        item.identity = identity
        self.verify(item, value)
        if "/virtualMachines/" in item.resource_id:
            disk = (
                value.get("properties", {}).get("storageProfile", {}).get("osDisk", {}).get("managedDisk", {}).get("id")
            )
            if type(disk) is not str or not resource_name_equal(disk, self.disk_id):
                raise StateError("Azure creation OS disk association is unproved")
            self.observed_disk_id = disk

    def _finish_create(self, item: ResourceEvidence, credential: object, deadline: Deadline) -> dict[str, Any] | None:
        self.poll(item, credential, deadline)
        if item.create_ack is None or item.create_ack.status_code != 201:
            raise StateError("Azure creation has no new-resource receipt")
        value = self.get(credential, item.resource_id, deadline)
        if value is not None:
            # This is completion of an acknowledged original request. No GET
            # may establish association for an unacknowledged PUT.
            self._associate(item, value)
            self.reconcile(deadline)
        return value

    def poll(self, item: ResourceEvidence, credential: object, deadline: Deadline, *, deleting: bool = False) -> None:
        ack = item.delete_ack if deleting else item.create_ack
        state = item.delete_state if deleting else item.create_state
        while state not in TERMINAL:
            if ack is None or ack.monitor is None:
                raise StateError("Azure creation request completion remains uncertain")
            response = self.call(credential, "GET", ack.monitor, deadline)
            state = _sdk(lambda response=response: monitor_state(response, ack, lambda: remaining(deadline)))
            if deleting:
                item.delete_state = state
            else:
                item.create_state = state
            self.reconcile(deadline)
            if state not in TERMINAL:
                time.sleep(min(0.25, remaining(deadline)))

    def create(self, index: int, credential: object, deadline: Deadline) -> dict[str, Any]:
        item, request = self.resources[index], self.plan[index]
        if item.sent:
            raise StateError("Azure resource creation cannot be replayed")
        if self.get(credential, item.resource_id, deadline) is not None:
            raise StateError("Azure creation refuses an existing resource")
        self.close(deadline)
        if self.cleanup_incomplete:
            raise StateError("Azure creation read original remains unsettled")
        row = self.owner.inspect_lifecycle_obligation(self._id)
        if row is None:
            raise StateError("Azure creation has no resource custody")
        if not self._marked:
            self._marked = True
            LifecycleObligation(self.owner, row).mark_possible_effect()
        item.sent = True
        self.reconcile(deadline)
        url = resource_url(item.resource_id)
        response = self.call(
            credential,
            "PUT",
            url,
            deadline,
            body=request.body,
            create_only="/virtualMachines/" in item.resource_id,
        )
        item.create_ack = _sdk(lambda: acknowledgment(response, "PUT", url, self.subscription))
        self.reconcile(deadline)
        # Only a returned new-resource receipt supplies provenance. A later GET
        # cannot repair a lost receipt by finding a matching name or tag.
        if item.create_ack.status_code != 201:
            raise StateError("Azure creation did not acknowledge a new resource")
        value = _sdk(lambda: json_body(response, lambda: remaining(deadline)))
        if not resource_name_equal(str(value.get("id")), item.resource_id):
            raise StateError("Azure creation returned a foreign resource")
        props = value.get("properties", {})
        if type(props) is not dict:
            raise ValidationError("Azure creation resource properties are invalid")
        initial_identity = props.get("vmId" if "/virtualMachines/" in item.resource_id else "resourceGuid")
        if initial_identity is not None:
            item.identity = canonical_vm_id(initial_identity)
        if "/virtualMachines/" in item.resource_id:
            disk = props.get("storageProfile", {}).get("osDisk", {}).get("managedDisk", {}).get("id")
            if disk is not None:
                if type(disk) is not str or not resource_name_equal(disk, self.disk_id):
                    raise StateError("Azure creation OS disk association is unproved")
                self.observed_disk_id = disk
        if item.create_ack.monitor is None:
            state = value.get("properties", {}).get("provisioningState")
            if state in TERMINAL:
                item.create_state = state
        self.reconcile(deadline)
        result = self._finish_create(item, credential, deadline)
        if item.create_state != "Succeeded":
            raise StateError("Azure resource creation failed")
        if result is None:
            raise StateError("Azure created resource disappeared")
        return result

    def rollback(self, credential: object, deadline: Deadline) -> bool:
        """Delete only acknowledged originals, after all retained creates settle."""
        for item in self.resources:
            if item.sent:
                self._finish_create(item, credential, deadline)
        for item in reversed(self.resources):
            if not item.sent or item.absent:
                continue
            if item.create_ack is None or item.create_ack.status_code != 201:
                return False
            if item.delete_sent:
                self.poll(item, credential, deadline, deleting=True)
            value = self.get(credential, item.resource_id, deadline)
            if value is not None:
                if item.identity is None:
                    return False
                self.verify(item, value)
                if item.delete_sent:
                    # Even a successful DELETE must be followed to absence.
                    return False
                item.delete_sent = True
                self.reconcile(deadline)
                url = resource_url(item.resource_id)
                response = self.call(credential, "DELETE", url, deadline)
                item.delete_ack = _sdk(
                    lambda response=response, url=url: acknowledgment(response, "DELETE", url, self.subscription)
                )
                if item.delete_ack.monitor is None:
                    item.delete_state = "Succeeded"
                self.reconcile(deadline)
                self.poll(item, credential, deadline, deleting=True)
                while self.get(credential, item.resource_id, deadline) is not None:
                    time.sleep(min(0.25, remaining(deadline)))
            item.absent = True
            self.reconcile(deadline)
        # VM deleteOption=Delete is a request, not disk absence proof. Never
        # sweep names, tags or all disks when a configuration field is missing.
        if self.resources[-1].sent:
            while self.get(credential, self.disk_id, deadline) is not None:
                time.sleep(min(0.25, remaining(deadline)))
        return True
