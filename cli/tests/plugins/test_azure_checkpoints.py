"""Azure checkpoint ownership, replay, and emergency-disk behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.compute.models import Disk, DiskUpdate, Snapshot, VirtualMachineUpdate

from agentworks.errors import StateError
from agentworks.plugins.azure import checkpoints


class _Poller:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def result(self) -> object:
        return self.value


class _Snapshots:
    def __init__(self) -> None:
        self.resources: dict[str, Any] = {}
        self.create_requests: list[Snapshot] = []

    def get(self, _group: str, name: str) -> Any:
        if name not in self.resources:
            raise ResourceNotFoundError("missing")
        return self.resources[name]

    def list_by_resource_group(self, _group: str) -> list[Any]:
        return list(self.resources.values())

    def begin_create_or_update(self, group: str, name: str, body: Snapshot) -> _Poller:
        self.create_requests.append(body)
        resource = SimpleNamespace(
            id=f"/subscriptions/sub/resourceGroups/{group}/providers/Microsoft.Compute/snapshots/{name}",
            unique_id="snapshot-uid",
            name=name,
            location=body.location,
            creation_data=SimpleNamespace(source_resource_id=body.creation_data.source_resource_id),
            tags=body.tags,
            provisioning_state="Succeeded",
        )
        self.resources[name] = resource
        return _Poller(resource)

    def begin_delete(self, _group: str, name: str) -> _Poller:
        self.resources.pop(name, None)
        return _Poller()


class _Disks:
    def __init__(self) -> None:
        self.create_requests: list[Disk] = []
        self.update_requests: list[DiskUpdate] = []
        self.resources: dict[str, Any] = {
            "disk-original": SimpleNamespace(
                id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/disks/disk-original",
                name="disk-original",
                location="eastus",
                sku=SimpleNamespace(name="Premium_LRS"),
                tags={},
                managed_by="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-a",
            )
        }

    def get(self, _group: str, name: str) -> Any:
        if name not in self.resources:
            raise ResourceNotFoundError("missing")
        return self.resources[name]

    def list_by_resource_group(self, _group: str) -> list[Any]:
        return list(self.resources.values())

    def begin_create_or_update(self, group: str, name: str, body: Disk) -> _Poller:
        self.create_requests.append(body)
        resource = SimpleNamespace(
            id=f"/subscriptions/sub/resourceGroups/{group}/providers/Microsoft.Compute/disks/{name}",
            name=name,
            location=body.location,
            tags=body.tags,
            provisioning_state="Succeeded",
            managed_by=None,
        )
        self.resources[name] = resource
        return _Poller(resource)

    def begin_update(self, _group: str, name: str, body: DiskUpdate) -> _Poller:
        self.update_requests.append(body)
        self.resources[name].tags = body.tags
        return _Poller(self.resources[name])

    def begin_delete(self, _group: str, name: str) -> _Poller:
        self.resources.pop(name, None)
        return _Poller()


class _VMs:
    def __init__(self, disks: _Disks) -> None:
        self.disks = disks
        self.update_requests: list[VirtualMachineUpdate] = []
        self.resource_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-a"
        self.vm = SimpleNamespace(
            id=self.resource_id,
            vm_id="vm-incarnation",
            name="vm-a",
            location="eastus",
            storage_profile=SimpleNamespace(
                os_disk=SimpleNamespace(managed_disk=SimpleNamespace(id=self.disks.resources["disk-original"].id))
            ),
        )

    def get(self, _group: str, _name: str) -> Any:
        return self.vm

    def instance_view(self, _group: str, _name: str) -> Any:
        return SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/deallocated")])

    def begin_update(self, _group: str, _name: str, body: VirtualMachineUpdate) -> _Poller:
        self.update_requests.append(body)
        old_id = self.vm.storage_profile.os_disk.managed_disk.id
        new_id = body.storage_profile.os_disk.managed_disk.id
        old_name = old_id.rstrip("/").rsplit("/", 1)[-1]
        new_name = new_id.rstrip("/").rsplit("/", 1)[-1]
        self.disks.resources[old_name].managed_by = None
        self.disks.resources[new_name].managed_by = self.resource_id
        self.vm.storage_profile.os_disk.managed_disk.id = new_id
        return _Poller(self.vm)


class _Compute:
    def __init__(self) -> None:
        self.snapshots = _Snapshots()
        self.disks = _Disks()
        self.virtual_machines = _VMs(self.disks)


def test_azure_checkpoint_requests_serialize_nested_sdk_models_for_arm() -> None:
    compute = _Compute()
    checkpoint = checkpoints.create_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )
    checkpoints.restore_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
        operation_id="restore-1",
    )

    snapshot_request = compute.snapshots.create_requests[0]
    assert snapshot_request.as_dict()["properties"]["creationData"] == {
        "createOption": "Copy",
        "sourceResourceId": compute.disks.resources["disk-original"].id,
    }

    disk_request = compute.disks.create_requests[0]
    assert disk_request.as_dict()["properties"]["creationData"] == {
        "createOption": "Copy",
        "sourceResourceId": next(iter(compute.snapshots.resources.values())).id,
    }

    vm_update = compute.virtual_machines.update_requests[0]
    replacement_id = vm_update.as_dict()["properties"]["storageProfile"]["osDisk"]["managedDisk"]["id"]
    assert replacement_id.rsplit("/", 1)[-1] in compute.disks.resources
    assert replacement_id != compute.disks.resources["disk-original"].id
    assert compute.disks.update_requests
    assert all(isinstance(request, DiskUpdate) for request in compute.disks.update_requests)


def test_azure_checkpoint_round_trip_is_replay_safe_and_retains_one_emergency_disk() -> None:
    compute = _Compute()
    checkpoint = checkpoints.create_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )

    assert (
        checkpoints.create_checkpoint(
            compute,
            resource_group="rg",
            vm_name="vm-a",
            name="upgrade-1",
            operation_id="create-1",
            resume=True,
        )
        == checkpoint
    )
    assert checkpoints.list_checkpoints(compute, resource_group="rg", vm_name="vm-a") == (checkpoint,)

    checkpoints.restore_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
        operation_id="restore-1",
    )

    first_restored_disk = compute.virtual_machines.vm.storage_profile.os_disk.managed_disk.id
    checkpoints.restore_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
        operation_id="restore-2",
    )
    checkpoints.restore_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
        operation_id="restore-2",
    )

    assert compute.virtual_machines.vm.storage_profile.os_disk.managed_disk.id != first_restored_disk
    assert first_restored_disk.rstrip("/").rsplit("/", 1)[-1] not in compute.disks.resources
    checkpoints.restore_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
        operation_id="restore-1",
    )

    emergency = [
        disk for disk in compute.disks.resources.values() if disk.tags.get("agentworks-checkpoint-role") == "emergency"
    ]
    assert [disk.name for disk in emergency] == ["disk-original"]

    checkpoints.delete_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
    )
    checkpoints.delete_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
    )

    assert not compute.snapshots.resources
    assert "disk-original" not in compute.disks.resources
    assert len(compute.disks.resources) == 1
    current_disk_name = compute.virtual_machines.vm.storage_profile.os_disk.managed_disk.id.rsplit("/", 1)[-1]
    assert not any(key.startswith("agentworks-") for key in compute.disks.resources[current_disk_name].tags)


def test_azure_restore_refuses_a_recreated_snapshot_incarnation() -> None:
    compute = _Compute()
    checkpoint = checkpoints.create_checkpoint(
        compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=False
    )
    snapshot = next(iter(compute.snapshots.resources.values()))
    snapshot.unique_id = "recreated-snapshot"

    with pytest.raises(StateError):
        checkpoints.restore_checkpoint(
            compute,
            resource_group="rg",
            vm_name="vm-a",
            checkpoint=checkpoint,
            operation_id="restore-1",
        )


def test_azure_create_replay_refuses_a_snapshot_from_another_os_disk() -> None:
    compute = _Compute()
    checkpoints.create_checkpoint(
        compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=False
    )
    snapshot = next(iter(compute.snapshots.resources.values()))
    snapshot.creation_data.source_resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/disks/disk-other"
    )

    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=True
        )


def test_azure_incomplete_checkpoint_remains_in_inventory_and_deletable() -> None:
    compute = _Compute()
    checkpoint = checkpoints.create_checkpoint(
        compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=False
    )
    snapshot = next(iter(compute.snapshots.resources.values()))
    snapshot.provisioning_state = "Creating"

    assert checkpoints.list_checkpoints(compute, resource_group="rg", vm_name="vm-a") == (checkpoint,)

    snapshot.provisioning_state = "Failed"
    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=True
        )
    checkpoints.delete_checkpoint(compute, resource_group="rg", vm_name="vm-a", checkpoint=checkpoint)

    assert not compute.snapshots.resources


def test_azure_delete_clears_emergency_markers_when_restore_failed_before_swap() -> None:
    compute = _Compute()
    checkpoint = checkpoints.create_checkpoint(
        compute, resource_group="rg", vm_name="vm-a", name="upgrade-1", operation_id="create-1", resume=False
    )
    checkpoints._tag_emergency_disk(
        compute,
        "rg",
        compute.disks.resources["disk-original"].id,
        vm_id="vm-incarnation",
        name=checkpoint.name,
        checkpoint_id="snapshot-uid",
    )

    checkpoints.delete_checkpoint(
        compute,
        resource_group="rg",
        vm_name="vm-a",
        checkpoint=checkpoint,
    )

    original = compute.disks.resources["disk-original"]
    assert not compute.snapshots.resources
    assert not any(key.startswith("agentworks-") for key in original.tags)
