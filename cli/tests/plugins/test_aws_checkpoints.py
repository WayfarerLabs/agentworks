"""EC2 checkpoint ownership, replay, and emergency-volume behavior."""

from __future__ import annotations

from typing import Any

import pytest

from agentworks.errors import StateError
from agentworks.plugins.aws import checkpoints
from agentworks.plugins.aws.network import EC2Error


class _Waiter:
    def wait(self, **_kwargs: object) -> None:
        return


class _EC2:
    def __init__(self) -> None:
        self.instance: dict[str, Any] = {
            "InstanceId": "i-123",
            "State": {"Name": "stopped"},
            "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-original"}}],
        }
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {
            "vol-original": {
                "VolumeId": "vol-original",
                "SnapshotId": "",
                "Attachments": [{"InstanceId": "i-123"}],
                "Tags": [],
            }
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.tasks_by_token: dict[str, dict[str, Any]] = {}
        self.restore_calls: list[dict[str, Any]] = []
        self.snapshot_create_calls = 0
        self.snapshot_visibility_delay = 0
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.stop_calls = 0

    def get_waiter(self, _name: str) -> _Waiter:
        return _Waiter()

    def describe_instances(self, **_kwargs: object) -> dict[str, object]:
        return {"Reservations": [{"Instances": [self.instance]}]}

    def create_snapshot(self, **kwargs: Any) -> dict[str, str]:
        self.snapshot_create_calls += 1
        snapshot_id = "snap-123"
        self.snapshots[snapshot_id] = {
            "SnapshotId": snapshot_id,
            "VolumeId": kwargs["VolumeId"],
            "State": "completed",
            "Tags": kwargs["TagSpecifications"][0]["Tags"],
        }
        return {"SnapshotId": snapshot_id}

    def describe_snapshots(self, **kwargs: Any) -> dict[str, object]:
        if kwargs.get("Filters") and self.snapshot_visibility_delay:
            self.snapshot_visibility_delay -= 1
            return {"Snapshots": []}
        resources = list(self.snapshots.values())
        if ids := kwargs.get("SnapshotIds"):
            resources = [resource for resource in resources if resource["SnapshotId"] in ids]
        for item in kwargs.get("Filters", []):
            key = str(item["Name"]).removeprefix("tag:")
            values = item["Values"]
            resources = [
                resource
                for resource in resources
                if any(tag["Key"] == key and tag["Value"] in values for tag in resource["Tags"])
            ]
        return {"Snapshots": resources}

    def start_instances(self, **_kwargs: object) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.instance["State"] = {"Name": "running"}

    def stop_instances(self, **_kwargs: object) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.instance["State"] = {"Name": "stopped"}

    def create_replace_root_volume_task(self, **kwargs: Any) -> dict[str, object]:
        if kwargs["ClientToken"] in self.tasks_by_token:
            return {"ReplaceRootVolumeTask": self.tasks_by_token[kwargs["ClientToken"]]}
        self.restore_calls.append(kwargs)
        old_volume_id = self.instance["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
        old_volume = self.volumes[old_volume_id]
        old_volume["Attachments"] = []
        sequence = len(self.restore_calls)
        restored_volume_id = f"vol-restored-{sequence}"
        self.volumes[restored_volume_id] = {
            "VolumeId": restored_volume_id,
            "SnapshotId": kwargs["SnapshotId"],
            "Attachments": [{"InstanceId": "i-123"}],
            "Tags": [],
        }
        self.instance["BlockDeviceMappings"] = [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": restored_volume_id}}]
        task_id = f"replace-{sequence}"
        task = {
            "ReplaceRootVolumeTaskId": task_id,
            "InstanceId": "i-123",
            "SnapshotId": kwargs["SnapshotId"],
            "TaskState": "succeeded",
            "Tags": kwargs["TagSpecifications"][0]["Tags"],
        }
        self.tasks[task_id] = task
        self.tasks_by_token[kwargs["ClientToken"]] = task
        return {"ReplaceRootVolumeTask": task}

    def describe_replace_root_volume_tasks(self, **kwargs: Any) -> dict[str, object]:
        return {"ReplaceRootVolumeTasks": [self.tasks[kwargs["ReplaceRootVolumeTaskIds"][0]]]}

    def create_tags(self, **kwargs: Any) -> None:
        for resource_id in kwargs["Resources"]:
            self.volumes[resource_id]["Tags"] = kwargs["Tags"]

    def delete_tags(self, **kwargs: Any) -> None:
        keys = {tag["Key"] for tag in kwargs["Tags"]}
        for resource_id in kwargs["Resources"]:
            self.volumes[resource_id]["Tags"] = [
                tag for tag in self.volumes[resource_id]["Tags"] if tag["Key"] not in keys
            ]

    def describe_volumes(self, **kwargs: Any) -> dict[str, object]:
        resources = list(self.volumes.values())
        if ids := kwargs.get("VolumeIds"):
            resources = [resource for resource in resources if resource["VolumeId"] in ids]
        for item in kwargs.get("Filters", []):
            key = str(item["Name"]).removeprefix("tag:")
            values = item["Values"]
            resources = [
                resource
                for resource in resources
                if any(tag["Key"] == key and tag["Value"] in values for tag in resource["Tags"])
            ]
        return {"Volumes": resources}

    def delete_snapshot(self, **kwargs: Any) -> None:
        self.snapshots.pop(kwargs["SnapshotId"], None)

    def delete_volume(self, **kwargs: Any) -> None:
        self.volumes.pop(kwargs["VolumeId"], None)


def test_ec2_checkpoint_round_trip_is_replay_safe_and_retains_one_emergency_volume() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )

    assert (
        checkpoints.create_checkpoint(ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=True)
        == checkpoint
    )
    assert checkpoints.list_checkpoints(ec2, instance_id="i-123") == (checkpoint,)

    checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-1")
    checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-1")

    assert len(ec2.restore_calls) == 1
    assert ec2.instance["State"] == {"Name": "stopped"}
    assert ec2.volumes["vol-original"]["Attachments"] == []
    assert ec2.restore_calls[0]["DeleteReplacedRootVolume"] is False

    checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-2")
    checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-2")

    assert len(ec2.restore_calls) == 2
    assert "vol-restored-1" not in ec2.volumes
    assert "vol-restored-2" in ec2.volumes

    checkpoints.delete_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint)
    checkpoints.delete_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint)

    assert not ec2.snapshots
    assert "vol-original" not in ec2.volumes
    assert "vol-restored-2" in ec2.volumes
    assert len(ec2.volumes) == 1
    assert not any(tag["Key"].startswith("agentworks:") for tag in ec2.volumes["vol-restored-2"]["Tags"])


def test_ec2_restore_refuses_a_snapshot_retagged_to_another_instance() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    vm_tag = next(
        tag for tag in ec2.snapshots[checkpoint.identifier]["Tags"] if tag["Key"] == "agentworks:vm-instance-id"
    )
    vm_tag["Value"] = "i-other"

    with pytest.raises(StateError):
        checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-1")

    assert not ec2.restore_calls


def test_ec2_start_failure_survives_a_failed_stop_cleanup() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    start_failure = RuntimeError("start failed")
    ec2.start_error = start_failure
    ec2.stop_error = RuntimeError("stop failed")

    with pytest.raises(EC2Error) as caught:
        checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-1")

    assert caught.value.__cause__ is start_failure
    assert ec2.stop_calls == 1


def test_ec2_restore_failure_survives_a_failed_stop_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    restore_failure = StateError("restore failed")
    ec2.stop_error = RuntimeError("stop failed")
    monkeypatch.setattr(
        checkpoints,
        "_wait_for_restore_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(restore_failure),
    )

    with pytest.raises(StateError) as caught:
        checkpoints.restore_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint, operation_id="restore-1")

    assert caught.value is restore_failure
    assert ec2.stop_calls == 1


def test_ec2_create_replay_refuses_a_snapshot_from_another_root_volume() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    ec2.snapshots[checkpoint.identifier]["VolumeId"] = "vol-other"

    with pytest.raises(StateError):
        checkpoints.create_checkpoint(ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=True)


def test_ec2_incomplete_checkpoint_remains_in_inventory_and_deletable() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    ec2.snapshots[checkpoint.identifier]["State"] = "pending"

    assert checkpoints.list_checkpoints(ec2, instance_id="i-123") == (checkpoint,)

    ec2.snapshots[checkpoint.identifier]["State"] = "error"
    with pytest.raises(StateError):
        checkpoints.create_checkpoint(ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=True)
    checkpoints.delete_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint)

    assert not ec2.snapshots


def test_ec2_resume_waits_for_an_initially_invisible_accepted_snapshot() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    ec2.snapshot_visibility_delay = 1

    resumed = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=True
    )

    assert resumed == checkpoint
    assert ec2.snapshot_create_calls == 1


def test_ec2_resume_with_permanently_empty_inventory_never_submits_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ec2 = _EC2()
    monkeypatch.setattr(checkpoints, "_CREATE_VISIBILITY_SECONDS", 0.0)

    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            ec2,
            instance_id="i-123",
            name="upgrade-1",
            operation_id="create-1",
            resume=True,
        )

    assert ec2.snapshot_create_calls == 0


def test_ec2_delete_clears_emergency_markers_when_restore_failed_before_swap() -> None:
    ec2 = _EC2()
    checkpoint = checkpoints.create_checkpoint(
        ec2, instance_id="i-123", name="upgrade-1", operation_id="create-1", resume=False
    )
    checkpoints._tag_displaced_volume(
        ec2,
        volume_id="vol-original",
        instance_id="i-123",
        checkpoint_id=checkpoint.identifier,
    )

    checkpoints.delete_checkpoint(ec2, instance_id="i-123", checkpoint=checkpoint)

    assert "vol-original" in ec2.volumes
    assert not ec2.snapshots
    assert not any(tag["Key"].startswith("agentworks:") for tag in ec2.volumes["vol-original"]["Tags"])
