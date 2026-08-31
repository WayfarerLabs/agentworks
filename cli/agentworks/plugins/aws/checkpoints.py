"""Agentworks-owned EC2 root-volume checkpoint lifecycle."""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any, cast

from agentworks import output
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.errors import StateError
from agentworks.plugins.aws.network import error_code, first_instance_state, wrap_ec2_error

if TYPE_CHECKING:
    from collections.abc import Mapping


_MANAGED_TAG = "agentworks:managed"
_MANAGED_VALUE = "vm-checkpoint"
_VM_TAG = "agentworks:vm-instance-id"
_NAME_TAG = "agentworks:checkpoint-name"
_CHECKPOINT_TAG = "agentworks:checkpoint-id"
_DISPLACED_TAG = "agentworks:displaced-root"
_SUPERSEDED_TAG = "agentworks:superseded-root"
_RESTORE_OPERATION_TAG = "agentworks:restore-operation"
_RESTORED_TAG = "agentworks:restored-root"
_CREATE_OPERATION_TAG = "agentworks:create-operation"
_CREATE_VISIBILITY_SECONDS = 30.0
_CREATE_VISIBILITY_POLL_SECONDS = 1.0
_TASK_POLL_SECONDS = 5.0
_TASK_TIMEOUT_SECONDS = 1800.0


def create_checkpoint(
    ec2: Any,
    *,
    instance_id: str,
    name: str,
    operation_id: str,
    resume: bool,
) -> CheckpointDescriptor:
    """Create or discover a completed root-volume snapshot."""
    instance = _instance(ec2, instance_id)
    if first_instance_state({"Reservations": [{"Instances": [instance]}]}) != "stopped":
        raise StateError(f"EC2 instance '{instance_id}' must be stopped before checkpoint creation")
    root_volume_id = _root_volume_id(instance)
    existing = _owned_snapshots(ec2, instance_id=instance_id, name=name)
    if resume and not existing:
        existing = _wait_for_snapshot_visibility(
            ec2,
            instance_id=instance_id,
            name=name,
            operation_id=operation_id,
        )
        if not existing:
            raise StateError(
                f"EC2 checkpoint '{name}' from the interrupted create operation is still not visible",
                hint=(
                    "Inspect EC2 snapshots for the operation-tagged artifact before retrying; "
                    "do not start a new create."
                ),
            )
    if len(existing) > 1:
        raise StateError(f"EC2 has multiple Agentworks checkpoints named '{name}' for instance '{instance_id}'")
    if existing:
        if _tag_map(existing[0]).get(_CREATE_OPERATION_TAG) != operation_id:
            raise StateError(f"EC2 checkpoint '{name}' belongs to another create operation")
        snapshot_id = str(existing[0]["SnapshotId"])
    else:
        tags = [
            *_tags(instance_id=instance_id, name=name),
            {"Key": _CREATE_OPERATION_TAG, "Value": operation_id},
        ]
        try:
            result = ec2.create_snapshot(
                VolumeId=root_volume_id,
                Description=f"Agentworks checkpoint {name} for {instance_id}",
                TagSpecifications=[{"ResourceType": "snapshot", "Tags": tags}],
            )
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        snapshot_id = str(result.get("SnapshotId") or "")
        if not snapshot_id:
            raise StateError(f"EC2 did not return an identity for checkpoint '{name}'")
    try:
        ec2.get_waiter("snapshot_completed").wait(SnapshotIds=[snapshot_id])
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc
    snapshot = _snapshot(ec2, snapshot_id)
    _verify_snapshot(
        snapshot,
        instance_id=instance_id,
        name=name,
        expected_volume_id=root_volume_id,
    )
    return CheckpointDescriptor(name=name, identifier=snapshot_id)


def list_checkpoints(ec2: Any, *, instance_id: str) -> tuple[CheckpointDescriptor, ...]:
    """List Agentworks snapshots for one exact EC2 instance, including incomplete ones."""
    snapshots = _owned_snapshots(ec2, instance_id=instance_id)
    result = []
    for snapshot in snapshots:
        tags = _tag_map(snapshot)
        name = tags.get(_NAME_TAG)
        snapshot_id = snapshot.get("SnapshotId")
        if name and snapshot_id:
            result.append(CheckpointDescriptor(name=name, identifier=str(snapshot_id)))
    return tuple(sorted(result, key=lambda checkpoint: checkpoint.name))


def restore_checkpoint(
    ec2: Any,
    *,
    instance_id: str,
    checkpoint: CheckpointDescriptor,
    operation_id: str,
) -> None:
    """Replace the root volume from a snapshot and return the instance stopped."""
    snapshot = _snapshot(ec2, checkpoint.identifier)
    _verify_snapshot(snapshot, instance_id=instance_id, name=checkpoint.name)
    original = _instance(ec2, instance_id)
    if first_instance_state({"Reservations": [{"Instances": [original]}]}) != "stopped":
        raise StateError(f"EC2 instance '{instance_id}' must be stopped before checkpoint restore")
    original_volume_id = _root_volume_id(original)
    original_tags = _volume_tags(ec2, original_volume_id)
    if original_tags.get(_RESTORED_TAG) == "true" and original_tags.get(_RESTORE_OPERATION_TAG) == operation_id:
        _delete_superseded_volumes(
            ec2,
            instance_id=instance_id,
            checkpoint_id=checkpoint.identifier,
            operation_id=operation_id,
        )
        return
    emergency = _owned_volumes(ec2, instance_id=instance_id, checkpoint_id=checkpoint.identifier, tag=_DISPLACED_TAG)
    if not emergency:
        _tag_displaced_volume(
            ec2,
            volume_id=original_volume_id,
            instance_id=instance_id,
            checkpoint_id=checkpoint.identifier,
        )
    elif all(str(volume["VolumeId"]) != original_volume_id for volume in emergency):
        _tag_superseded_volume(
            ec2,
            volume_id=original_volume_id,
            instance_id=instance_id,
            checkpoint_id=checkpoint.identifier,
            operation_id=operation_id,
        )
    token = _restore_token(instance_id, checkpoint.identifier, operation_id)
    primary_error: BaseException | None = None
    temporary_start_submitted = False
    try:
        state = first_instance_state({"Reservations": [{"Instances": [original]}]})
        if state != "running":
            output.info(f"Temporarily starting EC2 instance '{instance_id}' for checkpoint restore...")
            temporary_start_submitted = True
            try:
                ec2.start_instances(InstanceIds=[instance_id])
                ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
            except Exception as exc:
                raise wrap_ec2_error(exc) from exc
        output.info(f"Replacing the EC2 root volume from checkpoint '{checkpoint.name}'...")
        try:
            response = ec2.create_replace_root_volume_task(
                InstanceId=instance_id,
                SnapshotId=checkpoint.identifier,
                ClientToken=token,
                DeleteReplacedRootVolume=False,
                TagSpecifications=[
                    {
                        "ResourceType": "replace-root-volume-task",
                        "Tags": [
                            *_tags(instance_id=instance_id, name=checkpoint.name),
                            {"Key": _RESTORE_OPERATION_TAG, "Value": operation_id},
                        ],
                    }
                ],
            )
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        task = response.get("ReplaceRootVolumeTask") or {}
        task_id = str(task.get("ReplaceRootVolumeTaskId") or "")
        if not task_id:
            raise StateError(f"EC2 did not return a restore task for checkpoint '{checkpoint.name}'")
        _wait_for_restore_task(
            ec2,
            task_id=task_id,
            instance_id=instance_id,
            checkpoint=checkpoint,
            operation_id=operation_id,
        )
        restored_volume_id = _root_volume_id(_instance(ec2, instance_id))
        _tag_restored_volume(
            ec2,
            volume_id=restored_volume_id,
            instance_id=instance_id,
            checkpoint_id=checkpoint.identifier,
            operation_id=operation_id,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if temporary_start_submitted:
            _stop_after_restore(ec2, instance_id=instance_id, primary_error=primary_error)
    _delete_superseded_volumes(
        ec2,
        instance_id=instance_id,
        checkpoint_id=checkpoint.identifier,
        operation_id=operation_id,
    )


def delete_checkpoint(
    ec2: Any,
    *,
    instance_id: str,
    checkpoint: CheckpointDescriptor,
) -> None:
    """Delete one owned snapshot and any detached root it displaced."""
    snapshot = _snapshot_optional(ec2, checkpoint.identifier)
    if snapshot is not None:
        _verify_snapshot_ownership(snapshot, instance_id=instance_id, name=checkpoint.name)
    _release_checkpoint_volumes(ec2, instance_id=instance_id, checkpoint_id=checkpoint.identifier)
    if snapshot is not None:
        try:
            ec2.delete_snapshot(SnapshotId=checkpoint.identifier)
        except Exception as exc:
            if error_code(exc) != "InvalidSnapshot.NotFound":
                raise wrap_ec2_error(exc) from exc
    if _snapshot_optional(ec2, checkpoint.identifier) is not None:
        raise StateError(f"EC2 checkpoint '{checkpoint.name}' was not proven absent")


def _instance(ec2: Any, instance_id: str) -> Mapping[str, Any]:
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc
    reservations = response.get("Reservations") or []
    instances = reservations[0].get("Instances") or [] if reservations else []
    if not instances:
        raise StateError(f"EC2 instance '{instance_id}' no longer exists")
    return cast("Mapping[str, Any]", instances[0])


def _root_volume_id(instance: Mapping[str, Any]) -> str:
    root_name = instance.get("RootDeviceName")
    for mapping in instance.get("BlockDeviceMappings") or []:
        if mapping.get("DeviceName") == root_name:
            volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
            if volume_id:
                return str(volume_id)
    raise StateError(f"EC2 instance '{instance.get('InstanceId', 'unknown')}' has no identifiable root volume")


def _owned_snapshots(ec2: Any, *, instance_id: str, name: str | None = None) -> list[Mapping[str, Any]]:
    filters = [
        {"Name": f"tag:{_MANAGED_TAG}", "Values": [_MANAGED_VALUE]},
        {"Name": f"tag:{_VM_TAG}", "Values": [instance_id]},
    ]
    if name is not None:
        filters.append({"Name": f"tag:{_NAME_TAG}", "Values": [name]})
    try:
        response = ec2.describe_snapshots(OwnerIds=["self"], Filters=filters)
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc
    return list(response.get("Snapshots") or [])


def _wait_for_snapshot_visibility(
    ec2: Any,
    *,
    instance_id: str,
    name: str,
    operation_id: str,
) -> list[Mapping[str, Any]]:
    deadline = time.monotonic() + _CREATE_VISIBILITY_SECONDS
    while True:
        snapshots = _owned_snapshots(ec2, instance_id=instance_id, name=name)
        matching = [snapshot for snapshot in snapshots if _tag_map(snapshot).get(_CREATE_OPERATION_TAG) == operation_id]
        if matching:
            return matching
        if snapshots:
            raise StateError(f"EC2 checkpoint '{name}' belongs to another create operation")
        if time.monotonic() >= deadline:
            return []
        time.sleep(_CREATE_VISIBILITY_POLL_SECONDS)


def _snapshot(ec2: Any, snapshot_id: str) -> Mapping[str, Any]:
    snapshot = _snapshot_optional(ec2, snapshot_id)
    if snapshot is None:
        raise StateError(f"EC2 checkpoint artifact '{snapshot_id}' no longer exists")
    return snapshot


def _snapshot_optional(ec2: Any, snapshot_id: str) -> Mapping[str, Any] | None:
    try:
        response = ec2.describe_snapshots(OwnerIds=["self"], SnapshotIds=[snapshot_id])
    except Exception as exc:
        if error_code(exc) == "InvalidSnapshot.NotFound":
            return None
        raise wrap_ec2_error(exc) from exc
    snapshots = response.get("Snapshots") or []
    return snapshots[0] if snapshots else None


def _verify_snapshot(
    snapshot: Mapping[str, Any],
    *,
    instance_id: str,
    name: str,
    expected_volume_id: str | None = None,
) -> None:
    _verify_snapshot_ownership(snapshot, instance_id=instance_id, name=name)
    if snapshot.get("State") != "completed" or (
        expected_volume_id is not None and str(snapshot.get("VolumeId")) != expected_volume_id
    ):
        raise StateError(f"EC2 checkpoint '{name}' does not match the Agentworks VM incarnation")


def _verify_snapshot_ownership(snapshot: Mapping[str, Any], *, instance_id: str, name: str) -> None:
    tags = _tag_map(snapshot)
    if tags.get(_MANAGED_TAG) != _MANAGED_VALUE or tags.get(_VM_TAG) != instance_id or tags.get(_NAME_TAG) != name:
        raise StateError(f"EC2 checkpoint '{name}' does not match the Agentworks VM incarnation")


def _tag_map(resource: Mapping[str, Any]) -> dict[str, str]:
    return {str(tag.get("Key")): str(tag.get("Value")) for tag in resource.get("Tags") or [] if tag.get("Key")}


def _tags(*, instance_id: str, name: str) -> list[dict[str, str]]:
    return [
        {"Key": _MANAGED_TAG, "Value": _MANAGED_VALUE},
        {"Key": _VM_TAG, "Value": instance_id},
        {"Key": _NAME_TAG, "Value": name},
    ]


def _restore_token(instance_id: str, snapshot_id: str, operation_id: str) -> str:
    digest = hashlib.sha256(f"{instance_id}\0{snapshot_id}\0{operation_id}".encode()).hexdigest()
    return f"agw-checkpoint-{digest[:48]}"


def _stop_after_restore(ec2: Any, *, instance_id: str, primary_error: BaseException | None) -> None:
    """Return a temporarily started instance to stopped without masking restore failure."""
    output.info(f"Stopping EC2 instance '{instance_id}' after checkpoint restore...")
    try:
        ec2.stop_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])
    except BaseException as exc:
        if primary_error is not None:
            _warn_stop_cleanup_failure(instance_id)
            return
        if not isinstance(exc, Exception):
            raise
        raise StateError(
            f"EC2 could not prove instance '{instance_id}' stopped after checkpoint restore",
            hint="stop the instance in EC2 before retrying or performing recovery",
        ) from wrap_ec2_error(exc)


def _warn_stop_cleanup_failure(instance_id: str) -> None:
    try:  # noqa: SIM105 - warning failures must not replace the active restore failure
        output.warn(
            f"could not prove EC2 instance '{instance_id}' stopped after the restore failed; "
            "the original failure is unchanged; stop the instance in EC2 before retrying or recovering"
        )
    except BaseException:
        pass


def _wait_for_restore_task(
    ec2: Any,
    *,
    task_id: str,
    instance_id: str,
    checkpoint: CheckpointDescriptor,
    operation_id: str,
) -> None:
    deadline = time.monotonic() + _TASK_TIMEOUT_SECONDS
    while True:
        try:
            response = ec2.describe_replace_root_volume_tasks(ReplaceRootVolumeTaskIds=[task_id])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        tasks = response.get("ReplaceRootVolumeTasks") or []
        task = tasks[0] if tasks else None
        if (
            task is None
            or str(task.get("InstanceId")) != instance_id
            or str(task.get("SnapshotId")) != checkpoint.identifier
            or _tag_map(task).get(_RESTORE_OPERATION_TAG) != operation_id
        ):
            raise StateError(f"EC2 returned an unrelated restore task for checkpoint '{checkpoint.name}'")
        state = str(task.get("TaskState"))
        if state == "succeeded":
            return
        if state in {"failed", "failing", "failed-detached"}:
            hint = (
                "inspect the replacement task and reattach a bootable root volume before retrying"
                if state == "failed-detached"
                else "inspect the EC2 root-volume replacement task before retrying"
            )
            raise StateError(f"EC2 checkpoint restore task '{task_id}' ended in state '{state}'", hint=hint)
        if time.monotonic() >= deadline:
            raise StateError(
                f"EC2 checkpoint restore task '{task_id}' did not complete in time",
                hint="inspect the task before retrying because its outcome is not yet known",
            )
        time.sleep(_TASK_POLL_SECONDS)


def _tag_displaced_volume(
    ec2: Any,
    *,
    volume_id: str,
    instance_id: str,
    checkpoint_id: str,
) -> None:
    try:
        ec2.create_tags(
            Resources=[volume_id],
            Tags=[
                {"Key": _MANAGED_TAG, "Value": _MANAGED_VALUE},
                {"Key": _VM_TAG, "Value": instance_id},
                {"Key": _CHECKPOINT_TAG, "Value": checkpoint_id},
                {"Key": _DISPLACED_TAG, "Value": "true"},
            ],
        )
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc


def _volume_tags(ec2: Any, volume_id: str) -> dict[str, str]:
    try:
        volumes = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes") or []
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc
    if not volumes:
        raise StateError(f"EC2 root volume '{volume_id}' no longer exists")
    return _tag_map(cast("Mapping[str, Any]", volumes[0]))


def _owned_volumes(
    ec2: Any,
    *,
    instance_id: str,
    checkpoint_id: str,
    tag: str,
    operation_id: str | None = None,
) -> list[Mapping[str, Any]]:
    filters = [
        {"Name": f"tag:{_MANAGED_TAG}", "Values": [_MANAGED_VALUE]},
        {"Name": f"tag:{_VM_TAG}", "Values": [instance_id]},
        {"Name": f"tag:{_CHECKPOINT_TAG}", "Values": [checkpoint_id]},
        {"Name": f"tag:{tag}", "Values": ["true"]},
    ]
    if operation_id is not None:
        filters.append({"Name": f"tag:{_RESTORE_OPERATION_TAG}", "Values": [operation_id]})
    try:
        return cast("list[Mapping[str, Any]]", ec2.describe_volumes(Filters=filters).get("Volumes") or [])
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc


def _tag_superseded_volume(
    ec2: Any,
    *,
    volume_id: str,
    instance_id: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    try:
        ec2.create_tags(
            Resources=[volume_id],
            Tags=[
                {"Key": _MANAGED_TAG, "Value": _MANAGED_VALUE},
                {"Key": _VM_TAG, "Value": instance_id},
                {"Key": _CHECKPOINT_TAG, "Value": checkpoint_id},
                {"Key": _SUPERSEDED_TAG, "Value": "true"},
                {"Key": _RESTORE_OPERATION_TAG, "Value": operation_id},
            ],
        )
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc


def _tag_restored_volume(
    ec2: Any,
    *,
    volume_id: str,
    instance_id: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    try:
        ec2.delete_tags(Resources=[volume_id], Tags=[{"Key": _SUPERSEDED_TAG}, {"Key": _DISPLACED_TAG}])
        ec2.create_tags(
            Resources=[volume_id],
            Tags=[
                {"Key": _MANAGED_TAG, "Value": _MANAGED_VALUE},
                {"Key": _VM_TAG, "Value": instance_id},
                {"Key": _CHECKPOINT_TAG, "Value": checkpoint_id},
                {"Key": _RESTORED_TAG, "Value": "true"},
                {"Key": _RESTORE_OPERATION_TAG, "Value": operation_id},
            ],
        )
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc


def _delete_superseded_volumes(
    ec2: Any,
    *,
    instance_id: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    current_root = _root_volume_id(_instance(ec2, instance_id))
    for volume in _owned_volumes(
        ec2,
        instance_id=instance_id,
        checkpoint_id=checkpoint_id,
        tag=_SUPERSEDED_TAG,
        operation_id=operation_id,
    ):
        volume_id = str(volume["VolumeId"])
        if volume_id == current_root or volume.get("Attachments"):
            raise StateError("EC2 could not safely release a superseded checkpoint volume")
        try:
            ec2.delete_volume(VolumeId=volume_id)
        except Exception as exc:
            if error_code(exc) != "InvalidVolume.NotFound":
                raise wrap_ec2_error(exc) from exc


def _release_checkpoint_volumes(ec2: Any, *, instance_id: str, checkpoint_id: str) -> None:
    filters = [
        {"Name": f"tag:{_MANAGED_TAG}", "Values": [_MANAGED_VALUE]},
        {"Name": f"tag:{_VM_TAG}", "Values": [instance_id]},
        {"Name": f"tag:{_CHECKPOINT_TAG}", "Values": [checkpoint_id]},
    ]
    try:
        volumes = ec2.describe_volumes(Filters=filters).get("Volumes") or []
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc
    current_root = _root_volume_id(_instance(ec2, instance_id)) if volumes else None
    for volume in volumes:
        volume_id = str(volume["VolumeId"])
        if volume_id == current_root:
            try:
                ec2.delete_tags(
                    Resources=[volume_id],
                    Tags=[
                        {"Key": _MANAGED_TAG},
                        {"Key": _VM_TAG},
                        {"Key": _NAME_TAG},
                        {"Key": _CHECKPOINT_TAG},
                        {"Key": _DISPLACED_TAG},
                        {"Key": _SUPERSEDED_TAG},
                        {"Key": _RESTORED_TAG},
                        {"Key": _RESTORE_OPERATION_TAG},
                    ],
                )
            except Exception as exc:
                raise wrap_ec2_error(exc) from exc
            continue
        if volume.get("Attachments"):
            raise StateError(
                f"EC2 checkpoint volume '{volume_id}' is attached outside the VM's current root",
                hint="Detach or recover the volume before deleting the managed checkpoint.",
            )
        try:
            ec2.delete_volume(VolumeId=volume_id)
        except Exception as exc:
            if error_code(exc) != "InvalidVolume.NotFound":
                raise wrap_ec2_error(exc) from exc
    try:
        remaining = ec2.describe_volumes(Filters=filters).get("Volumes") or []
    except Exception as exc:
        if error_code(exc) != "InvalidVolume.NotFound":
            raise wrap_ec2_error(exc) from exc
        remaining = []
    if remaining:
        raise StateError("EC2 checkpoint volumes were not fully released")
