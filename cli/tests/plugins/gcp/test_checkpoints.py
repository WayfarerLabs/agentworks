"""GCE checkpoint ownership, replay, and emergency-disk behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.errors import StateError
from agentworks.plugins.gcp import checkpoints

if TYPE_CHECKING:
    from typing import Any


_PROJECT = "project-a"
_ZONE = "us-central1-a"
_INSTANCE = "vm-a"
_INSTANCE_ID = "201"


class _Operation:
    status = None
    error = None

    def result(self, *, timeout: float) -> None:
        assert timeout > 0


class _Instances:
    def __init__(self) -> None:
        self.resource = compute_v1.Instance(
            id=int(_INSTANCE_ID),
            name=_INSTANCE,
            status="TERMINATED",
            disks=[
                compute_v1.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    device_name="persistent-disk-0",
                    mode="READ_WRITE",
                    source=f"projects/{_PROJECT}/zones/{_ZONE}/disks/disk-original",
                    type_="PERSISTENT",
                )
            ],
        )

    def get(self, **_kwargs: object) -> compute_v1.Instance:
        return self.resource

    def detach_disk(self, *, request: compute_v1.DetachDiskInstanceRequest) -> _Operation:
        self.resource.disks = [disk for disk in self.resource.disks if disk.device_name != request.device_name]
        return _Operation()

    def attach_disk(self, *, request: compute_v1.AttachDiskInstanceRequest) -> _Operation:
        self.resource.disks = [compute_v1.AttachedDisk(compute_v1.AttachedDisk.to_dict(request.attached_disk_resource))]
        return _Operation()


class _Snapshots:
    def __init__(self) -> None:
        self.resources: dict[str, compute_v1.Snapshot] = {}

    def get(self, *, snapshot: str, **_kwargs: object) -> compute_v1.Snapshot:
        if snapshot not in self.resources:
            raise api_exceptions.NotFound("missing")  # type: ignore[no-untyped-call]
        return self.resources[snapshot]

    def list(self, **_kwargs: object) -> list[compute_v1.Snapshot]:
        return list(self.resources.values())

    def insert(self, *, request: compute_v1.InsertSnapshotRequest) -> _Operation:
        resource = compute_v1.Snapshot(compute_v1.Snapshot.to_dict(request.snapshot_resource))
        resource.id = 301
        resource.status = "READY"
        resource.self_link = f"projects/{_PROJECT}/global/snapshots/{resource.name}"
        resource.source_disk_id = "401"
        self.resources[resource.name] = resource
        return _Operation()

    def delete(self, *, request: compute_v1.DeleteSnapshotRequest) -> _Operation:
        self.resources.pop(request.snapshot, None)
        return _Operation()


class _Disks:
    def __init__(self) -> None:
        original = compute_v1.Disk(
            id=401,
            name="disk-original",
            self_link=f"projects/{_PROJECT}/zones/{_ZONE}/disks/disk-original",
            type_=f"projects/{_PROJECT}/zones/{_ZONE}/diskTypes/pd-balanced",
            status="READY",
            users=[f"projects/{_PROJECT}/zones/{_ZONE}/instances/{_INSTANCE}"],
            label_fingerprint="fingerprint",
        )
        self.resources: dict[str, compute_v1.Disk] = {original.name: original}

    def get(self, *, disk: str, **_kwargs: object) -> compute_v1.Disk:
        if disk not in self.resources:
            raise api_exceptions.NotFound("missing")  # type: ignore[no-untyped-call]
        return self.resources[disk]

    def list(self, **_kwargs: object) -> list[compute_v1.Disk]:
        return list(self.resources.values())

    def insert(self, *, request: compute_v1.InsertDiskRequest) -> _Operation:
        resource = compute_v1.Disk(compute_v1.Disk.to_dict(request.disk_resource))
        resource.id = 402
        resource.status = "READY"
        resource.self_link = f"projects/{_PROJECT}/zones/{_ZONE}/disks/{resource.name}"
        resource.source_snapshot_id = "301"
        resource.label_fingerprint = "replacement"
        self.resources[resource.name] = resource
        return _Operation()

    def set_labels(self, *, request: compute_v1.SetLabelsDiskRequest) -> _Operation:
        resource = self.resources[request.resource]
        resource.labels = dict(request.zone_set_labels_request_resource.labels)
        resource.label_fingerprint = "updated"
        resource.users = []
        return _Operation()

    def delete(self, *, request: compute_v1.DeleteDiskRequest) -> _Operation:
        self.resources.pop(request.disk, None)
        return _Operation()


class _Cache:
    def __init__(self) -> None:
        self.instances = _Instances()
        self.snapshots = _Snapshots()
        self.disks = _Disks()

    def client(self, kind: str, _ctx: RunContext) -> Any:
        return getattr(self, kind)


def test_gce_checkpoint_round_trip_is_replay_safe_and_retains_one_emergency_disk() -> None:
    cache = _Cache()
    ctx = RunContext()
    checkpoint = checkpoints.create_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )

    assert (
        checkpoints.create_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            name="upgrade-1",
            operation_id="create-1",
            resume=True,
        )
        == checkpoint
    )
    assert checkpoints.list_checkpoints(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
    ) == (checkpoint,)

    for _attempt in range(2):
        checkpoints.restore_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            checkpoint=checkpoint,
            operation_id="restore-1",
        )

    first_restored_disk = cache.instances.resource.disks[0].source.rsplit("/", 1)[-1]
    for _attempt in range(2):
        checkpoints.restore_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            checkpoint=checkpoint,
            operation_id="restore-2",
        )

    assert cache.instances.resource.disks[0].source.rsplit("/", 1)[-1] != first_restored_disk
    assert first_restored_disk not in cache.disks.resources

    emergency = [
        disk for disk in cache.disks.resources.values() if disk.labels.get("agentworks_checkpoint_role") == "emergency"
    ]
    assert [disk.name for disk in emergency] == ["disk-original"]

    for _attempt in range(2):
        checkpoints.delete_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            checkpoint=checkpoint,
        )

    assert not cache.snapshots.resources
    assert "disk-original" not in cache.disks.resources
    assert len(cache.disks.resources) == 1
    current_disk_name = cache.instances.resource.disks[0].source.rsplit("/", 1)[-1]
    assert not any(key.startswith("agentworks_") for key in cache.disks.resources[current_disk_name].labels)


def test_gce_restore_refuses_a_recreated_snapshot_incarnation() -> None:
    cache = _Cache()
    ctx = RunContext()
    checkpoint = checkpoints.create_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )
    snapshot = next(iter(cache.snapshots.resources.values()))
    snapshot.id = 999

    with pytest.raises(StateError):
        checkpoints.restore_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            checkpoint=checkpoint,
            operation_id="restore-1",
        )


def test_gce_create_replay_refuses_a_snapshot_from_another_boot_disk() -> None:
    cache = _Cache()
    ctx = RunContext()
    checkpoints.create_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )
    snapshot = next(iter(cache.snapshots.resources.values()))
    snapshot.source_disk_id = "999"

    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            name="upgrade-1",
            operation_id="create-1",
            resume=True,
        )


def test_gce_incomplete_checkpoint_remains_in_inventory_and_deletable() -> None:
    cache = _Cache()
    ctx = RunContext()
    checkpoint = checkpoints.create_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )
    snapshot = next(iter(cache.snapshots.resources.values()))
    snapshot.status = "CREATING"

    assert checkpoints.list_checkpoints(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
    ) == (checkpoint,)

    snapshot.status = "FAILED"
    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            cache,  # type: ignore[arg-type]
            ctx,
            project_id=_PROJECT,
            zone=_ZONE,
            instance_name=_INSTANCE,
            instance_id=_INSTANCE_ID,
            name="upgrade-1",
            operation_id="create-1",
            resume=True,
        )
    checkpoints.delete_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        checkpoint=checkpoint,
    )

    assert not cache.snapshots.resources


def test_gce_delete_clears_emergency_markers_when_restore_failed_before_swap() -> None:
    cache = _Cache()
    ctx = RunContext()
    checkpoint = checkpoints.create_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        name="upgrade-1",
        operation_id="create-1",
        resume=False,
    )
    checkpoints._tag_emergency_disk(
        cache.disks,
        _PROJECT,
        _ZONE,
        cache.disks.resources["disk-original"],
        instance_id=_INSTANCE_ID,
        name=checkpoint.name,
        checkpoint_id="301",
    )

    checkpoints.delete_checkpoint(
        cache,  # type: ignore[arg-type]
        ctx,
        project_id=_PROJECT,
        zone=_ZONE,
        instance_name=_INSTANCE,
        instance_id=_INSTANCE_ID,
        checkpoint=checkpoint,
    )

    original = cache.disks.resources["disk-original"]
    assert not cache.snapshots.resources
    assert not any(key.startswith("agentworks_") for key in original.labels)
