"""Retained, exact-resource rollback of an unaccepted Proxmox creation.

Logical image absence is proved while VM membership and effective observation
permissions still exist. Destroy completion and exact VMID availability follow
that proof; neither means physical capacity reclamation or secure erasure.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _Endpoint, _valid_task_id
from agentworks.execution.carriers.proxmox import _ProxmoxWire
from agentworks.plugins.proxmox._rollback_observations import (
    RollbackDisk,
    associated_config,
    disks,
    image_volumes,
    require_no_snapshots_or_pending,
    require_permissions,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carriers.proxmox import ProxmoxConnection
    from agentworks.plugins.proxmox._creation import CloneAssociation


class RollbackPhase(StrEnum):
    INITIAL = "initial"
    ASSOCIATED = "associated"
    STOP_REQUESTED = "stop-requested"
    STOPPED = "stopped"
    INVENTORIED = "inventoried"
    REMOVE_REQUESTED = "remove-requested"
    REMOVAL_SETTLED = "removal-settled"
    DESTROY_REQUESTED = "destroy-requested"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, repr=False)
class RollbackState:
    phase: RollbackPhase = RollbackPhase.INITIAL
    generation: str | None = None
    disks: tuple[RollbackDisk, ...] = ()
    digest: str | None = None
    stop_upid: str | None = None
    remove_upid: str | None = None
    destroy_upid: str | None = None


_UPID = re.compile(
    r"UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):([0-9A-Fa-f]{8}):"
    r"([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):(qmstop|qmconfig|qmdestroy):([1-9][0-9]*):([^:\s/]+):"
)


def _task_fields(value: object, connection: ProxmoxConnection, task_type: str) -> dict[str, object]:
    """Bind the external acknowledgment to the exact submitted mutation."""
    if not _valid_task_id(value):
        raise ValidationError("Proxmox rollback task receipt is invalid")
    assert isinstance(value, str)
    match = _UPID.fullmatch(value)
    if match is None:
        raise ValidationError("Proxmox rollback task receipt is invalid")
    node, pid, pstart, starttime, kind, vmid, identity = match.groups()
    if (node, kind, vmid, identity) != (connection.node, task_type, str(connection.vmid), connection.token_id):
        raise ValidationError("Proxmox rollback receipt does not match its exact request")
    user, separator, token = identity.partition("!")
    result: dict[str, object] = {
        "upid": value,
        "node": node,
        "pid": int(pid, 16),
        "pstart": int(pstart, 16),
        "starttime": int(starttime, 16),
        "type": kind,
        "id": vmid,
        "user": user,
    }
    if separator:
        result["tokenid"] = token
    return result


class ProxmoxCreationRollback:
    """One concrete sequence under the original already-armed resource obligation.

    The creation access serializes this controller and publishes its immutable
    state into the existing resource history. No ordinary admission is reopened.
    Wire-entry facts distinguish an interrupted pre-dispatch publication from a
    possibly sent request within this retained process. No process-loss recovery
    or replay of a possibly sent request is implemented.
    """

    def __init__(
        self,
        connection: ProxmoxConnection,
        *,
        instance_marker: str,
        storage: str,
        custody: LocalDeliveryCustody,
        publish: Callable[[RollbackState], None],
    ) -> None:
        self._wire = _ProxmoxWire(connection)
        self._marker, self._storage = instance_marker, storage
        self._custody, self._publish = custody, publish
        self.state = RollbackState()
        self._entered: set[RollbackPhase] = set()

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Proxmox rollback requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Proxmox rollback deadline expired")
        return remaining

    def _record(self, **changes: object) -> None:
        # Retain the exact requested state before publication, including a lost
        # committed response. The callback reconciles that same resource history.
        self.state = replace(self.state, **changes)
        self._publish(self.state)

    def _check(self, deadline: Deadline) -> None:
        self._remaining(deadline)
        self._publish(self.state)
        if not self._custody.settled:
            raise StateError("Proxmox rollback retains local delivery")

    def _read(self, endpoint: _Endpoint, deadline: Deadline, *, storage: str | None = None) -> object:
        self._check(deadline)
        value = self._wire.request_rollback_observation(
            endpoint, storage=storage, custody=self._custody, timeout=self._remaining(deadline)
        )
        self._check(deadline)
        return value

    def _config(self, association: CloneAssociation, deadline: Deadline) -> dict[str, object]:
        self._check(deadline)
        config = self._wire.request_current_config(timeout=self._remaining(deadline), custody=self._custody)
        self._check(deadline)
        associated_config(config, association, self._marker, self.state.generation)
        return config

    def _power(self, deadline: Deadline) -> str:
        self._check(deadline)
        status = self._wire.request_power(timeout=self._remaining(deadline), custody=self._custody)
        self._check(deadline)
        if status.get("status") not in ("running", "stopped"):
            raise StateError("Proxmox rollback power is unproved")
        return str(status["status"])

    def _permissions(self, deadline: Deadline) -> None:
        require_permissions(
            self._read(_Endpoint.VM_PERMISSIONS, deadline),
            f"/vms/{self._wire._connection.vmid}",
            ("VM.Config.Disk", "VM.Config.CDROM", "VM.Config.Cloudinit"),
        )
        require_permissions(
            self._read(_Endpoint.STORAGE_PERMISSIONS, deadline, storage=self._storage),
            f"/storage/{self._storage}",
            ("Datastore.Audit", "Datastore.AllocateSpace"),
        )

    def _shape(self, deadline: Deadline) -> None:
        require_no_snapshots_or_pending(
            self._read(_Endpoint.SNAPSHOTS, deadline), self._read(_Endpoint.PENDING, deadline)
        )

    def _volumes(self, deadline: Deadline) -> frozenset[str]:
        return image_volumes(
            self._read(_Endpoint.STORAGE_IMAGES, deadline, storage=self._storage),
            self._storage,
            self._wire._connection.vmid,
        )

    def _submit(
        self,
        phase: RollbackPhase,
        task_type: str,
        receipt_field: str,
        deadline: Deadline,
        request: Callable[[], str],
    ) -> str:
        if self.state.phase is not phase:
            self._record(phase=phase)
        self._check(deadline)
        upid = getattr(self.state, receipt_field)
        if upid is not None:
            assert isinstance(upid, str)
            return upid
        if phase in self._entered:
            raise StateError("Proxmox rollback acknowledgment is unknown; mutation cannot be replayed")
        self._entered.add(phase)
        upid = request()
        _task_fields(upid, self._wire._connection, task_type)
        self._record(**{receipt_field: upid})
        self._check(deadline)
        return upid

    def _wait(self, upid: str, task_type: str, deadline: Deadline) -> None:
        expected = _task_fields(upid, self._wire._connection, task_type)
        while True:
            self._check(deadline)
            data = self._wire.request_task_status(upid, timeout=self._remaining(deadline), custody=self._custody)
            self._check(deadline)
            if any(type(data.get(key)) is not type(value) or data[key] != value for key, value in expected.items()) or (
                "tokenid" not in expected and "tokenid" in data
            ):
                raise StateError("Proxmox rollback task identity is unproved")
            if data.get("status") == "stopped" and data.get("exitstatus") == "OK":
                return
            if data.get("status") != "running":
                raise StateError("Proxmox rollback task completion is unproved")
            time.sleep(min(0.1, self._remaining(deadline)))

    def settle(
        self,
        association: CloneAssociation,
        deadline: Deadline,
        *,
        expected_generation: str | None,
    ) -> bool:
        """Advance retained cleanup only after original creation controllers settle."""
        if (association.node, association.vmid) != (self._wire._connection.node, self._wire._connection.vmid):
            raise StateError("Proxmox rollback association does not match the original destination")
        self._check(deadline)
        if self.state.phase is RollbackPhase.INITIAL:
            config = self._config(association, deadline)
            self._record(
                phase=RollbackPhase.ASSOCIATED,
                generation=associated_config(config, association, self._marker, expected_generation),
            )
        if self.state.phase in (RollbackPhase.ASSOCIATED, RollbackPhase.STOP_REQUESTED):
            # A known stop receipt is always observed, even if power already fell.
            if self.state.phase is RollbackPhase.STOP_REQUESTED or self._power(deadline) == "running":
                if self.state.stop_upid is None and RollbackPhase.STOP_REQUESTED not in self._entered:
                    self._config(association, deadline)
                upid = self._submit(
                    RollbackPhase.STOP_REQUESTED,
                    "qmstop",
                    "stop_upid",
                    deadline,
                    lambda: self._wire.request_vm_stop(timeout=self._remaining(deadline), custody=self._custody),
                )
                self._wait(upid, "qmstop", deadline)
            if self._power(deadline) != "stopped":
                raise StateError("Proxmox rollback stop is not observed")
            self._record(phase=RollbackPhase.STOPPED)
        if self.state.phase is RollbackPhase.STOPPED:
            self._shape(deadline)
            self._permissions(deadline)
            config = self._config(association, deadline)
            inventory = disks(config, self._storage)
            observed = self._volumes(deadline)
            self._permissions(deadline)
            if observed != frozenset(disk.volume for disk in inventory):
                raise StateError("Proxmox rollback cannot positively inventory every exact owned image")
            digest = config["digest"]
            assert isinstance(digest, str)
            self._record(phase=RollbackPhase.INVENTORIED, disks=inventory, digest=digest)
        if self.state.phase in (RollbackPhase.INVENTORIED, RollbackPhase.REMOVE_REQUESTED):
            if self.state.disks:
                if self.state.remove_upid is None and RollbackPhase.REMOVE_REQUESTED not in self._entered:
                    self._shape(deadline)
                    self._permissions(deadline)
                    config = self._config(association, deadline)
                    if self._power(deadline) != "stopped" or disks(config, self._storage) != self.state.disks:
                        raise StateError("Proxmox rollback disk inventory changed")
                    digest = config["digest"]
                    assert isinstance(digest, str)
                    self._record(digest=digest)
                upid = self._submit(
                    RollbackPhase.REMOVE_REQUESTED,
                    "qmconfig",
                    "remove_upid",
                    deadline,
                    lambda: self._wire.request_vm_remove_disks(
                        tuple(disk.key for disk in self.state.disks),
                        digest=self.state.digest or "",
                        timeout=self._remaining(deadline),
                        custody=self._custody,
                    ),
                )
                self._wait(upid, "qmconfig", deadline)
            self._record(phase=RollbackPhase.REMOVAL_SETTLED)
        if self.state.phase in (RollbackPhase.REMOVAL_SETTLED, RollbackPhase.DESTROY_REQUESTED):
            if self.state.destroy_upid is None and RollbackPhase.DESTROY_REQUESTED not in self._entered:
                # PVE has no generation-conditional destroy. This fresh proof is
                # deliberately not described as an atomic provider precondition.
                self._verify_empty(association, deadline)
            upid = self._submit(
                RollbackPhase.DESTROY_REQUESTED,
                "qmdestroy",
                "destroy_upid",
                deadline,
                lambda: self._wire.request_vm_destroy(timeout=self._remaining(deadline), custody=self._custody),
            )
            self._wait(upid, "qmdestroy", deadline)
            self._check(deadline)
            self._wire.request_vmid_free(timeout=self._remaining(deadline), custody=self._custody)
            self._check(deadline)
            self._record(phase=RollbackPhase.COMPLETE)
        self._check(deadline)
        return self.state.phase is RollbackPhase.COMPLETE

    def _verify_empty(self, association: CloneAssociation, deadline: Deadline) -> None:
        self._shape(deadline)
        self._permissions(deadline)
        config = self._config(association, deadline)
        if disks(config, self._storage) or self._power(deadline) != "stopped":
            raise StateError("Proxmox rollback config still references images or VM is not stopped")
        if self._volumes(deadline):
            raise StateError("Proxmox rollback logical image absence is unproved")
        self._permissions(deadline)
        if self._config(association, deadline)["digest"] != config["digest"]:
            raise StateError("Proxmox rollback config changed during logical image verification")
