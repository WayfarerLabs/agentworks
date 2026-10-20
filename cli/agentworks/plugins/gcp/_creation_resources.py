"""One GCE resource obligation, request receipts and exact unaccepted rollback.

Compute deletion addresses names. A fresh numeric-ID check and the returned
operation target validate observations; they cannot prevent a replacement
between read and DELETE because the API has no atomic ID precondition.
Initialized boot disks use fresh unpredictable names, with an absence check
before dispatch. Attachment plus disk ID/users proves the observed association,
not atomic allocation provenance across a concurrent read/insert replacement.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.db import LifecycleObligationState
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation
from agentworks.plugins.gcp._activation_operation import ActivationState
from agentworks.plugins.gcp._creation_api import remaining
from agentworks.plugins.gcp._creation_network import validate_firewall
from agentworks.plugins.gcp._creation_shapes import boot_disk_path, completion, exact_resource, receipt
from agentworks.plugins.gcp._native_route_evidence import HOSTS

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.plugins.gcp._creation_api import CreationAPI

KIND = "gcp-gce-created-resources"
VERSION = 1
TERMINAL = (ActivationState.SUCCEEDED, ActivationState.FAILED)


@dataclass(slots=True, repr=False)
class Resource:
    path: str
    kind: str
    body: dict[str, Any]
    create_request: str = field(default_factory=lambda: str(uuid4()))
    create_entered: bool = False
    create_operation: str | None = None
    resource_id: str | None = None
    create_state: ActivationState = ActivationState.UNKNOWN
    delete_request: str = field(default_factory=lambda: str(uuid4()))
    delete_entered: bool = False
    delete_operation: str | None = None
    delete_state: ActivationState = ActivationState.UNKNOWN
    absent: bool = False

    @property
    def scope(self) -> str:
        return self.path.rsplit("/", 2)[0]


class CreationResources:
    """The original obligation authorizes cleanup after ordinary admission ends.

    There is no resource adoption or restart protocol. Unknown acknowledgments
    retain this exact controller and its bounded non-secret durable intent.
    """

    def __init__(self, owner: OperationOwner, api: CreationAPI, resources: dict[str, Resource]) -> None:
        self.owner, self.api, self.resources = owner, api, resources
        self.accepted = False
        self._id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._registration_entered = self._resolution_entered = False
        self._marked = False
        self._settled = False
        self._durable = self._encode()
        self._revision = 0
        self._publication: tuple[bytes, int, bytes] | None = None

    def _encode(self) -> bytes:
        data = json.dumps(
            {"version": VERSION, "accepted": self.accepted, "resources": {
                key: asdict(value) for key, value in self.resources.items()
            }}, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        if len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValidationError("GCE creation resource intent exceeds the lifecycle bound")
        return data

    def register(self, deadline: Deadline) -> None:
        if self._registration_entered:
            raise StateError("GCE creation resource registration is single use")
        remaining(deadline)
        self._registration_entered = True
        self._obligation = self.owner.register_lifecycle_obligation(
            KIND, payload_version=VERSION, payload=self._durable, obligation_id=self._id,
        )
        remaining(deadline)
        self._obligation.mark_possible_effect()
        self.reconcile(deadline)
        if not self._marked:
            raise StateError("GCE creation effect admission remains unproved")

    def reconcile(self, deadline: Deadline) -> None:
        """Repair only this retained publication's exact before/after revision."""
        remaining(deadline)
        if not self._registration_entered or self._settled:
            return
        row = self.owner.inspect_lifecycle_obligation(self._id)
        if row is None:
            if self._obligation is not None or any(r.create_entered for r in self.resources.values()):
                raise StateError("GCE creation resource registration remains uncertain")
            return
        if row.obligation_kind != KIND or row.payload_version != VERSION:
            raise StateError("GCE creation resource custody conflicts")
        if self._publication is None:
            if (row.payload, row.payload_revision) != (self._durable, self._revision):
                raise StateError("GCE creation resource payload conflicts")
        else:
            before, revision, after = self._publication
            if (row.payload, row.payload_revision) not in ((before, revision), (after, revision + 1)):
                raise StateError("GCE creation publication conflicts")
        self._obligation = LifecycleObligation(self.owner, row)
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolution_entered or self._publication is not None:
                raise StateError("GCE creation resource custody was prematurely resolved")
            self._settled = True
            return
        self._marked = row.state is LifecycleObligationState.POSSIBLE_EFFECT
        if any(r.create_entered for r in self.resources.values()) and not self._marked:
            raise StateError("GCE creation possible effect disappeared")
        if self._publication is not None:
            before, revision, after = self._publication
            if not self._marked:
                raise StateError("GCE creation publication requires retained possible effect")
            if (row.payload, row.payload_revision) == (before, revision):
                self._obligation.publish_payload(expected_revision=revision, payload_version=VERSION, payload=after)
            self._durable, self._revision, self._publication = after, revision + 1, None

    def publish(self, deadline: Deadline) -> None:
        self.reconcile(deadline)
        after = self._encode()
        if after != self._durable:
            self._publication = self._durable, self._revision, after
        self.reconcile(deadline)

    def _retain(self, resource: Resource, value: dict[str, Any], deadline: Deadline, *, deleting: bool) -> None:
        operation, target = receipt(
            value, scope=resource.scope, path=resource.path,
            request_id=resource.delete_request if deleting else resource.create_request,
            deleting=deleting, resource_id=resource.resource_id,
            operation=resource.delete_operation if deleting else resource.create_operation,
        )
        if deleting:
            resource.delete_operation = operation
            resource.delete_state = ActivationState.PENDING
        else:
            resource.create_operation, resource.resource_id = operation, target
            resource.create_state = ActivationState.PENDING
        try:
            state = completion(value)
            if deleting:
                resource.delete_state = state
            else:
                resource.create_state = state
        finally:
            # Matching identity survives a malformed completion or lost publication.
            self.publish(deadline)

    def insert(self, key: str, deadline: Deadline) -> None:
        resource = self.resources[key]
        self.reconcile(deadline)
        if not self._marked or resource.create_entered or self.accepted:
            raise StateError("GCE creation insert cannot be admitted again")
        resource.create_entered = True
        self.publish(deadline)
        path = resource.path.rsplit("/", 1)[0] + f"?requestId={resource.create_request}"
        with self.api.exchange("POST", path, deadline, body=resource.body) as value:
            assert value is not None
            self._retain(resource, value, deadline, deleting=False)
        self.wait(resource, deadline)
        if resource.create_state is not ActivationState.SUCCEEDED:
            raise StateError("GCE creation insert did not succeed")

    def wait(self, resource: Resource, deadline: Deadline, *, deleting: bool = False) -> None:
        state = resource.delete_state if deleting else resource.create_state
        while state not in TERMINAL:
            operation = resource.delete_operation if deleting else resource.create_operation
            if operation is None:
                raise StateError("GCE creation retains an unknown operation association")
            self.reconcile(deadline)
            with self.api.exchange("GET", f"{resource.scope}/operations/{operation}", deadline) as value:
                assert value is not None
                self._retain(resource, value, deadline, deleting=deleting)
            state = resource.delete_state if deleting else resource.create_state
            if state not in TERMINAL:
                time.sleep(min(0.1, remaining(deadline)))

    def capture_disk(self, path: str, deadline: Deadline) -> None:
        """Called only after receipt-owned VM observation proves this boot attachment."""
        disk = self.resources["disk"]
        if path != disk.path:
            raise StateError("GCE created boot disk path changed")
        value = self.api.get(path, deadline)
        assert value is not None
        instance = self.resources["instance"]
        users = value.get("users")
        if type(users) is not list or len(users) != 1 or users[0] not in tuple(
            f"{host}/compute/v1/{instance.path}" for host in HOSTS
        ):
            raise StateError("GCE created disk is not attached to the receipt-owned VM")
        disk.resource_id = exact_resource(value, path, "disk", disk.resource_id)
        self.publish(deadline)

    def delete(self, key: str, deadline: Deadline) -> bool:
        if self.accepted and key != "allow":
            raise StateError("GCE accepted resources cannot select bootstrap rollback")
        resource = self.resources[key]
        self.reconcile(deadline)
        if resource.absent:
            return True
        if key != "disk" and resource.create_entered:
            self.wait(resource, deadline)
        if resource.delete_entered:
            self.wait(resource, deadline, deleting=True)
        if resource.resource_id is None:
            if resource.create_entered or (key == "disk" and self.resources["instance"].create_entered):
                return False
            resource.absent = True
            self.publish(deadline)
            return True
        current = self.api.get(resource.path, deadline, missing=True)
        if current is None:
            resource.absent = True
            self.publish(deadline)
            return True
        exact_resource(current, resource.path, resource.kind, resource.resource_id)
        if resource.kind == "firewall":
            project = resource.path.split("/")[1]
            validate_firewall(current, resource.body, project, resource.resource_id)
        elif resource.kind == "instance":
            disk = self.resources["disk"]
            if disk.resource_id is None:
                return False
            boot_disk_path(current, disk.path)
            attached = self.api.get(disk.path, deadline)
            assert attached is not None
            exact_resource(attached, disk.path, "disk", disk.resource_id)
        elif resource.kind == "disk" and current.get("users", []) != []:
            raise StateError("GCE created boot disk remains attached")
        if resource.delete_entered:
            # One DELETE attempt, even after a terminal failure or a late GET.
            return False
        resource.delete_entered = True
        self.publish(deadline)
        with self.api.exchange("DELETE", resource.path + f"?requestId={resource.delete_request}", deadline) as value:
            assert value is not None
            self._retain(resource, value, deadline, deleting=True)
        self.wait(resource, deadline, deleting=True)
        if resource.delete_state is not ActivationState.SUCCEEDED:
            return False
        while True:
            current = self.api.get(resource.path, deadline, missing=True)
            if current is None:
                resource.absent = True
                self.publish(deadline)
                return True
            exact_resource(current, resource.path, resource.kind, resource.resource_id)
            time.sleep(min(0.1, remaining(deadline)))

    def accept(self, deadline: Deadline) -> None:
        self.reconcile(deadline)
        if not self.accepted:
            # Retain acceptance before publication can fail. Never select rollback again.
            self.accepted = True
        self.publish(deadline)

    @property
    def disposition_complete(self) -> bool:
        attempted = any(resource.create_entered for resource in self.resources.values())
        disposed = all(resource.absent for resource in self.resources.values())
        transferred = self.accepted and self.resources["allow"].absent
        return not attempted or disposed or transferred

    def resolve(self, deadline: Deadline) -> bool:
        self.reconcile(deadline)
        if self._settled or not self._registration_entered:
            return True
        if self._obligation is None:
            return True
        if not self.disposition_complete:
            return False
        if self.api.cleanup_incomplete:
            return False
        if self._marked:
            self.publish(deadline)
        self._resolution_entered = True
        self._obligation.resolve()
        self.reconcile(deadline)
        return self._settled
