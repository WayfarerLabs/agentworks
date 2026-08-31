"""Agentworks-owned GCE boot-disk checkpoint lifecycle."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.errors import StateError
from agentworks.plugins.gcp.compute import canonical_resource_url, provider_resource_id
from agentworks.plugins.gcp.errors import call_google, call_google_optional, wait_for_extended_operation

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.plugins.gcp.auth import GcpClientCache


_MANAGED_LABEL = "agentworks_managed"
_MANAGED_VALUE = "vm_checkpoint"
_VM_ID_LABEL = "agentworks_vm_id"
_NAME_HASH_LABEL = "agentworks_name_hash"
_CHECKPOINT_ID_LABEL = "agentworks_checkpoint_id"
_ROLE_LABEL = "agentworks_checkpoint_role"
_DEVICE_LABEL = "agentworks_device_name"
_OPERATION_LABEL = "agentworks_restore_operation"
_CREATE_OPERATION_LABEL = "agentworks_create_operation"
_NAME_DESCRIPTION_PREFIX = "agentworks-checkpoint-name:"
_READY_TIMEOUT_SECONDS = 1800.0
_READY_POLL_SECONDS = 5.0


def create_checkpoint(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    instance_id: str,
    name: str,
    operation_id: str,
    resume: bool,
) -> CheckpointDescriptor:
    """Create or discover a completed snapshot of the stopped boot disk."""
    from google.cloud import compute_v1

    instance = _instance(clients, ctx, project_id, zone, instance_name, instance_id)
    _require_stopped(instance)
    boot = _boot_disk(instance)
    boot_disk_name = str(boot.source).rstrip("/").rsplit("/", 1)[-1]
    boot_disk = _get_disk(clients.client("disks", ctx), project_id, zone, boot_disk_name)
    boot_disk_id = provider_resource_id(boot_disk.id)
    if boot_disk_id is None:
        raise StateError(f"GCE boot disk '{boot_disk_name}' has no provider incarnation ID")
    snapshot_name = _artifact_name("agw-checkpoint", instance_id, name)
    del resume  # The deterministic resource name is the GCE replay boundary.
    snapshots = clients.client("snapshots", ctx)
    snapshot = _get_snapshot_optional(snapshots, project_id, snapshot_name)
    if snapshot is None:
        labels = {
            **_labels(instance_id=instance_id, name=name),
            _CREATE_OPERATION_LABEL: _operation_hash(operation_id),
        }
        resource = compute_v1.Snapshot(
            name=snapshot_name,
            source_disk=str(boot.source),
            labels=labels,
            description=_encode_name(name),
        )
        request_id = _request_id("snapshot", instance_id, name)
        operation = call_google(
            lambda: snapshots.insert(
                request=compute_v1.InsertSnapshotRequest(
                    project=project_id,
                    snapshot_resource=resource,
                    request_id=request_id,
                )
            ),
            operation="creating a VM checkpoint snapshot",
            resource=f"snapshot {project_id}/{snapshot_name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"snapshot {project_id}/{snapshot_name}",
            zone=None,
            timeout=_READY_TIMEOUT_SECONDS,
        )
    snapshot = _wait_for_snapshot(snapshots, project_id=project_id, snapshot_name=snapshot_name)
    if dict(snapshot.labels or {}).get(_CREATE_OPERATION_LABEL) != _operation_hash(operation_id):
        raise StateError(f"GCE checkpoint '{name}' belongs to another create operation")
    _verify_snapshot(
        snapshot,
        instance_id=instance_id,
        name=name,
        expected_source_disk_id=boot_disk_id,
    )
    return CheckpointDescriptor(name=name, identifier=_snapshot_identifier(snapshot))


def list_checkpoints(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    instance_id: str,
) -> tuple[CheckpointDescriptor, ...]:
    """List owned snapshots, including incomplete ones, for one exact VM incarnation."""
    _instance(clients, ctx, project_id, zone, instance_name, instance_id)
    snapshots = clients.client("snapshots", ctx)
    resources = call_google(
        lambda: list(snapshots.list(project=project_id)),
        operation="listing VM checkpoints",
        resource=f"project {project_id}",
    )
    result = []
    for snapshot in resources:
        labels = dict(snapshot.labels or {})
        if labels.get(_MANAGED_LABEL) != _MANAGED_VALUE or labels.get(_VM_ID_LABEL) != instance_id:
            continue
        name = _decode_name(str(snapshot.description or ""))
        if name is None or labels.get(_NAME_HASH_LABEL) != _name_hash(name):
            continue
        result.append(CheckpointDescriptor(name=name, identifier=_snapshot_identifier(snapshot)))
    return tuple(sorted(result, key=lambda checkpoint: checkpoint.name))


def restore_checkpoint(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    instance_id: str,
    checkpoint: CheckpointDescriptor,
    operation_id: str,
) -> None:
    """Swap the checkpoint-derived disk onto the same stopped GCE instance."""
    from google.cloud import compute_v1

    snapshots = clients.client("snapshots", ctx)
    snapshot_name, snapshot_id = _split_identifier(checkpoint.identifier)
    snapshot = _get_snapshot_optional(snapshots, project_id, snapshot_name)
    if snapshot is None:
        raise StateError(f"GCE checkpoint artifact '{checkpoint.identifier}' no longer exists")
    _verify_snapshot(snapshot, instance_id=instance_id, name=checkpoint.name, expected_id=snapshot_id)
    instances = clients.client("instances", ctx)
    disks = clients.client("disks", ctx)
    instance = _instance(clients, ctx, project_id, zone, instance_name, instance_id)
    _require_stopped(instance)
    boot = _boot_disk_optional(instance)
    replacement_name = _artifact_name("agw-restore", instance_id, snapshot_id, operation_id)
    replacement = _get_disk_optional(disks, project_id, zone, replacement_name)
    if replacement is None:
        if boot is None:
            raise StateError(
                f"GCE instance '{instance_name}' has no boot disk and no checkpoint replacement disk",
                hint="reattach the original boot disk before retrying checkpoint restore",
            )
        source_disk_name = str(boot.source).rstrip("/").rsplit("/", 1)[-1]
        source_disk = _get_disk(disks, project_id, zone, source_disk_name)
        replacement_resource = compute_v1.Disk(
            name=replacement_name,
            source_snapshot=str(snapshot.self_link),
            type_=str(source_disk.type_),
            labels={
                **_labels(instance_id=instance_id, name=checkpoint.name),
                _CHECKPOINT_ID_LABEL: snapshot_id,
                _ROLE_LABEL: "restored",
                _DEVICE_LABEL: str(boot.device_name),
                _OPERATION_LABEL: _operation_hash(operation_id),
            },
        )
        operation = call_google(
            lambda: disks.insert(
                request=compute_v1.InsertDiskRequest(
                    project=project_id,
                    zone=zone,
                    disk_resource=replacement_resource,
                    request_id=_request_id("restore-disk", instance_id, snapshot_id, operation_id),
                )
            ),
            operation="creating a checkpoint replacement disk",
            resource=f"disk {project_id}/{zone}/{replacement_name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"disk {project_id}/{zone}/{replacement_name}",
            zone=zone,
            timeout=_READY_TIMEOUT_SECONDS,
        )
    replacement = _wait_for_disk(disks, project_id=project_id, zone=zone, disk_name=replacement_name)
    _verify_replacement(
        replacement,
        snapshot_id=snapshot_id,
        instance_id=instance_id,
        operation_id=operation_id,
    )
    replacement_url = canonical_resource_url(str(replacement.self_link))
    if boot is not None and canonical_resource_url(str(boot.source)) == replacement_url:
        _cleanup_superseded_disks(
            disks,
            project_id,
            zone,
            instance_id=instance_id,
            checkpoint_id=snapshot_id,
            operation_id=operation_id,
            current_disk_url=replacement_url,
        )
        return
    emergency = _emergency_disks(disks, project_id, zone, instance_id=instance_id, checkpoint_id=snapshot_id)
    if len(emergency) > 1:
        raise StateError("GCE has multiple emergency disks for one Agentworks checkpoint")
    if not emergency:
        if boot is None:
            raise StateError(
                f"GCE instance '{instance_name}' has no boot disk and no retained emergency disk",
                hint="inspect the instance disks before retrying checkpoint restore",
            )
        source_disk_name = str(boot.source).rstrip("/").rsplit("/", 1)[-1]
        source_disk = _get_disk(disks, project_id, zone, source_disk_name)
        _tag_emergency_disk(
            disks,
            project_id,
            zone,
            source_disk,
            instance_id=instance_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_id,
        )
    elif boot is not None and canonical_resource_url(str(boot.source)) != canonical_resource_url(
        str(emergency[0].self_link)
    ):
        source_disk_name = str(boot.source).rstrip("/").rsplit("/", 1)[-1]
        _tag_superseded_disk(
            disks,
            project_id,
            zone,
            _get_disk(disks, project_id, zone, source_disk_name),
            instance_id=instance_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_id,
            operation_id=operation_id,
        )
    if boot is not None:
        operation = call_google(
            lambda: instances.detach_disk(
                request=compute_v1.DetachDiskInstanceRequest(
                    project=project_id,
                    zone=zone,
                    instance=instance_name,
                    device_name=str(boot.device_name),
                    request_id=_request_id("detach-boot", instance_id, snapshot_id, operation_id),
                )
            ),
            operation="detaching the displaced boot disk",
            resource=f"instance {project_id}/{zone}/{instance_name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"instance {project_id}/{zone}/{instance_name}",
            zone=zone,
            timeout=_READY_TIMEOUT_SECONDS,
        )
    device_name = str((replacement.labels or {}).get(_DEVICE_LABEL) or "persistent-disk-0")
    attached = compute_v1.AttachedDisk(
        auto_delete=True,
        boot=True,
        device_name=device_name,
        mode="READ_WRITE",
        source=str(replacement.self_link),
        type_="PERSISTENT",
    )
    operation = call_google(
        lambda: instances.attach_disk(
            request=compute_v1.AttachDiskInstanceRequest(
                project=project_id,
                zone=zone,
                instance=instance_name,
                attached_disk_resource=attached,
                request_id=_request_id("attach-boot", instance_id, snapshot_id, operation_id),
            )
        ),
        operation="attaching the checkpoint replacement boot disk",
        resource=f"instance {project_id}/{zone}/{instance_name}",
    )
    wait_for_extended_operation(
        operation,
        label=f"instance {project_id}/{zone}/{instance_name}",
        zone=zone,
        timeout=_READY_TIMEOUT_SECONDS,
    )
    converged = _instance(clients, ctx, project_id, zone, instance_name, instance_id)
    converged_boot = _boot_disk(converged)
    if canonical_resource_url(str(converged_boot.source)) != replacement_url:
        raise StateError(f"GCE VM '{instance_name}' was not proven restored to checkpoint '{checkpoint.name}'")
    _cleanup_superseded_disks(
        disks,
        project_id,
        zone,
        instance_id=instance_id,
        checkpoint_id=snapshot_id,
        operation_id=operation_id,
        current_disk_url=replacement_url,
    )


def delete_checkpoint(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    instance_id: str,
    checkpoint: CheckpointDescriptor,
) -> None:
    """Delete an owned snapshot and its detached emergency boot disk."""
    from google.cloud import compute_v1

    instance = _instance(clients, ctx, project_id, zone, instance_name, instance_id)
    snapshots = clients.client("snapshots", ctx)
    disks = clients.client("disks", ctx)
    snapshot_name, snapshot_id = _split_identifier(checkpoint.identifier)
    snapshot = _get_snapshot_optional(snapshots, project_id, snapshot_name)
    if snapshot is not None:
        _verify_snapshot_ownership(
            snapshot,
            instance_id=instance_id,
            name=checkpoint.name,
            expected_id=snapshot_id,
        )
    boot = _boot_disk_optional(instance)
    checkpoint_disks = _owned_checkpoint_disks(
        disks,
        project_id,
        zone,
        instance_id=instance_id,
        checkpoint_id=snapshot_id,
    )
    if checkpoint_disks and boot is None:
        raise StateError(
            f"GCE instance '{instance_name}' has no current boot disk while checkpoint disks are retained",
            hint="Repair the boot-disk attachment before deleting the managed checkpoint.",
        )
    current_boot = None if boot is None else canonical_resource_url(str(boot.source))
    for disk in checkpoint_disks:
        disk_url = canonical_resource_url(str(disk.self_link))
        if disk_url == current_boot:
            _clear_checkpoint_labels(
                disks,
                project_id,
                zone,
                disk,
                instance_id=instance_id,
                checkpoint_id=snapshot_id,
            )
            continue
        if disk.users:
            raise StateError(
                f"GCE checkpoint disk '{disk.name}' is attached outside the VM's current boot disk",
                hint="Detach or recover the disk before deleting the managed checkpoint.",
            )
        request = compute_v1.DeleteDiskRequest(
            project=project_id,
            zone=zone,
            disk=str(disk.name),
            request_id=_request_id("delete-checkpoint-disk", instance_id, snapshot_id, str(disk.name)),
        )
        operation = call_google(
            partial(disks.delete, request=request),
            operation="deleting a retained checkpoint disk",
            resource=f"disk {project_id}/{zone}/{disk.name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"disk {project_id}/{zone}/{disk.name}",
            zone=zone,
            timeout=_READY_TIMEOUT_SECONDS,
        )
    if _owned_checkpoint_disks(
        disks,
        project_id,
        zone,
        instance_id=instance_id,
        checkpoint_id=snapshot_id,
    ):
        raise StateError("GCE checkpoint disks were not fully released")

    if snapshot is not None:
        operation = call_google(
            lambda: snapshots.delete(
                request=compute_v1.DeleteSnapshotRequest(
                    project=project_id,
                    snapshot=snapshot_name,
                    request_id=_request_id("delete-snapshot", instance_id, snapshot_id),
                )
            ),
            operation="deleting a VM checkpoint snapshot",
            resource=f"snapshot {project_id}/{snapshot_name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"snapshot {project_id}/{snapshot_name}",
            zone=None,
            timeout=_READY_TIMEOUT_SECONDS,
        )
    if _get_snapshot_optional(snapshots, project_id, snapshot_name) is not None:
        raise StateError(f"GCE checkpoint '{checkpoint.name}' was not proven absent")


def _instance(
    clients: GcpClientCache,
    ctx: RunContext,
    project_id: str,
    zone: str,
    instance_name: str,
    instance_id: str,
) -> Any:
    instance = call_google_optional(
        lambda: clients.client("instances", ctx).get(project=project_id, zone=zone, instance=instance_name),
        operation="reading the checkpoint VM",
        resource=f"instance {project_id}/{zone}/{instance_name}",
    )
    if instance is None or provider_resource_id(instance.id) != instance_id:
        raise StateError(f"GCE instance '{instance_name}' no longer matches its Agentworks VM incarnation")
    return instance


def _boot_disk(instance: Any) -> Any:
    boot = _boot_disk_optional(instance)
    if boot is None:
        raise StateError(f"GCE instance '{instance.name}' has no identifiable boot disk")
    return boot


def _require_stopped(instance: Any) -> None:
    if str(instance.status).upper() not in {"TERMINATED", "SUSPENDED"}:
        raise StateError(f"GCE instance '{instance.name}' must be stopped before checkpoint operations")


def _boot_disk_optional(instance: Any) -> Any | None:
    boots = [disk for disk in instance.disks or [] if bool(disk.boot)]
    if len(boots) > 1:
        raise StateError(f"GCE instance '{instance.name}' has multiple boot disks")
    return boots[0] if boots else None


def _get_snapshot_optional(snapshots: Any, project_id: str, snapshot_name: str) -> Any | None:
    return call_google_optional(
        lambda: snapshots.get(project=project_id, snapshot=snapshot_name),
        operation="reading a VM checkpoint snapshot",
        resource=f"snapshot {project_id}/{snapshot_name}",
    )


def _wait_for_snapshot(snapshots: Any, *, project_id: str, snapshot_name: str) -> Any:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while True:
        snapshot = _get_snapshot_optional(snapshots, project_id, snapshot_name)
        if snapshot is None:
            raise StateError(f"GCE snapshot '{snapshot_name}' disappeared before completion")
        status = str(snapshot.status).upper()
        if status == "READY":
            return snapshot
        if status == "FAILED":
            raise StateError(f"GCE snapshot '{snapshot_name}' failed")
        if time.monotonic() >= deadline:
            raise StateError(
                f"GCE snapshot '{snapshot_name}' did not complete in time",
                hint="inspect the snapshot before retrying because its outcome is not yet known",
            )
        time.sleep(_READY_POLL_SECONDS)


def _get_disk_optional(disks: Any, project_id: str, zone: str, disk_name: str) -> Any | None:
    return call_google_optional(
        lambda: disks.get(project=project_id, zone=zone, disk=disk_name),
        operation="reading a checkpoint disk",
        resource=f"disk {project_id}/{zone}/{disk_name}",
    )


def _get_disk(disks: Any, project_id: str, zone: str, disk_name: str) -> Any:
    disk = _get_disk_optional(disks, project_id, zone, disk_name)
    if disk is None:
        raise StateError(f"GCE disk '{disk_name}' no longer exists")
    return disk


def _wait_for_disk(disks: Any, *, project_id: str, zone: str, disk_name: str) -> Any:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while True:
        disk = _get_disk(disks, project_id, zone, disk_name)
        status = str(disk.status).upper()
        if status == "READY":
            return disk
        if status == "FAILED":
            raise StateError(f"GCE disk '{disk_name}' failed")
        if time.monotonic() >= deadline:
            raise StateError(
                f"GCE disk '{disk_name}' did not become ready in time",
                hint="inspect the disk before retrying because its outcome is not yet known",
            )
        time.sleep(_READY_POLL_SECONDS)


def _verify_snapshot(
    snapshot: Any,
    *,
    instance_id: str,
    name: str,
    expected_id: str | None = None,
    expected_source_disk_id: str | None = None,
) -> None:
    _verify_snapshot_ownership(
        snapshot,
        instance_id=instance_id,
        name=name,
        expected_id=expected_id,
    )
    if str(snapshot.status).upper() != "READY" or (
        expected_source_disk_id is not None and provider_resource_id(snapshot.source_disk_id) != expected_source_disk_id
    ):
        raise StateError(f"GCE checkpoint '{name}' does not match the Agentworks VM incarnation")


def _verify_snapshot_ownership(
    snapshot: Any,
    *,
    instance_id: str,
    name: str,
    expected_id: str | None = None,
) -> None:
    labels = dict(snapshot.labels or {})
    snapshot_id = provider_resource_id(snapshot.id)
    if (
        labels.get(_MANAGED_LABEL) != _MANAGED_VALUE
        or labels.get(_VM_ID_LABEL) != instance_id
        or labels.get(_NAME_HASH_LABEL) != _name_hash(name)
        or _decode_name(str(snapshot.description or "")) != name
        or snapshot_id is None
        or (expected_id is not None and snapshot_id != expected_id)
    ):
        raise StateError(f"GCE checkpoint '{name}' does not match the Agentworks VM incarnation")


def _verify_replacement(disk: Any, *, snapshot_id: str, instance_id: str, operation_id: str) -> None:
    labels = dict(disk.labels or {})
    if (
        labels.get(_MANAGED_LABEL) != _MANAGED_VALUE
        or labels.get(_VM_ID_LABEL) != instance_id
        or labels.get(_CHECKPOINT_ID_LABEL) != snapshot_id
        or labels.get(_ROLE_LABEL) != "restored"
        or labels.get(_OPERATION_LABEL) != _operation_hash(operation_id)
        or provider_resource_id(disk.source_snapshot_id) != snapshot_id
        or str(disk.status).upper() != "READY"
    ):
        raise StateError("GCE checkpoint replacement disk does not match its persisted provider identities")


def _tag_emergency_disk(
    disks: Any,
    project_id: str,
    zone: str,
    disk: Any,
    *,
    instance_id: str,
    name: str,
    checkpoint_id: str,
) -> None:
    from google.cloud import compute_v1

    labels = {
        **dict(disk.labels or {}),
        **_labels(instance_id=instance_id, name=name),
        _CHECKPOINT_ID_LABEL: checkpoint_id,
        _ROLE_LABEL: "emergency",
    }
    operation = call_google(
        lambda: disks.set_labels(
            request=compute_v1.SetLabelsDiskRequest(
                project=project_id,
                zone=zone,
                resource=str(disk.name),
                request_id=_request_id("tag-emergency", instance_id, checkpoint_id),
                zone_set_labels_request_resource=compute_v1.ZoneSetLabelsRequest(
                    labels=labels,
                    label_fingerprint=str(disk.label_fingerprint),
                ),
            )
        ),
        operation="retaining the pre-restore emergency disk",
        resource=f"disk {project_id}/{zone}/{disk.name}",
    )
    wait_for_extended_operation(
        operation,
        label=f"disk {project_id}/{zone}/{disk.name}",
        zone=zone,
        timeout=_READY_TIMEOUT_SECONDS,
    )


def _tag_superseded_disk(
    disks: Any,
    project_id: str,
    zone: str,
    disk: Any,
    *,
    instance_id: str,
    name: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    from google.cloud import compute_v1

    labels = {
        **dict(disk.labels or {}),
        **_labels(instance_id=instance_id, name=name),
        _CHECKPOINT_ID_LABEL: checkpoint_id,
        _ROLE_LABEL: "superseded",
        _OPERATION_LABEL: _operation_hash(operation_id),
    }
    operation = call_google(
        lambda: disks.set_labels(
            request=compute_v1.SetLabelsDiskRequest(
                project=project_id,
                zone=zone,
                resource=str(disk.name),
                request_id=_request_id("tag-superseded", instance_id, checkpoint_id, operation_id),
                zone_set_labels_request_resource=compute_v1.ZoneSetLabelsRequest(
                    labels=labels,
                    label_fingerprint=str(disk.label_fingerprint),
                ),
            )
        ),
        operation="marking a superseded checkpoint restore disk",
        resource=f"disk {project_id}/{zone}/{disk.name}",
    )
    wait_for_extended_operation(
        operation,
        label=f"disk {project_id}/{zone}/{disk.name}",
        zone=zone,
        timeout=_READY_TIMEOUT_SECONDS,
    )


def _cleanup_superseded_disks(
    disks: Any,
    project_id: str,
    zone: str,
    *,
    instance_id: str,
    checkpoint_id: str,
    operation_id: str,
    current_disk_url: str,
) -> None:
    from google.cloud import compute_v1

    operation_hash = _operation_hash(operation_id)
    resources = call_google(
        lambda: list(disks.list(project=project_id, zone=zone)),
        operation="listing superseded checkpoint disks",
        resource=f"zone {project_id}/{zone}",
    )
    for disk in resources:
        labels = dict(disk.labels or {})
        if (
            labels.get(_MANAGED_LABEL) != _MANAGED_VALUE
            or labels.get(_VM_ID_LABEL) != instance_id
            or labels.get(_CHECKPOINT_ID_LABEL) != checkpoint_id
            or labels.get(_ROLE_LABEL) != "superseded"
            or labels.get(_OPERATION_LABEL) != operation_hash
        ):
            continue
        if canonical_resource_url(str(disk.self_link)) == current_disk_url or disk.users:
            raise StateError("GCE could not safely release a superseded checkpoint disk")
        request = compute_v1.DeleteDiskRequest(
            project=project_id,
            zone=zone,
            disk=str(disk.name),
            request_id=_request_id("delete-superseded", instance_id, checkpoint_id, operation_id),
        )
        operation = call_google(
            partial(disks.delete, request=request),
            operation="deleting a superseded checkpoint disk",
            resource=f"disk {project_id}/{zone}/{disk.name}",
        )
        wait_for_extended_operation(
            operation,
            label=f"disk {project_id}/{zone}/{disk.name}",
            zone=zone,
            timeout=_READY_TIMEOUT_SECONDS,
        )


def _clear_checkpoint_labels(
    disks: Any,
    project_id: str,
    zone: str,
    disk: Any,
    *,
    instance_id: str,
    checkpoint_id: str,
) -> None:
    from google.cloud import compute_v1

    labels = dict(disk.labels or {})
    if (
        labels.get(_MANAGED_LABEL) != _MANAGED_VALUE
        or labels.get(_VM_ID_LABEL) != instance_id
        or labels.get(_CHECKPOINT_ID_LABEL) != checkpoint_id
    ):
        raise StateError("GCE current boot disk does not match the checkpoint being deleted")
    for key in (
        _MANAGED_LABEL,
        _VM_ID_LABEL,
        _NAME_HASH_LABEL,
        _CHECKPOINT_ID_LABEL,
        _ROLE_LABEL,
        _DEVICE_LABEL,
        _OPERATION_LABEL,
    ):
        labels.pop(key, None)
    operation = call_google(
        lambda: disks.set_labels(
            request=compute_v1.SetLabelsDiskRequest(
                project=project_id,
                zone=zone,
                resource=str(disk.name),
                request_id=_request_id("clear-checkpoint", instance_id, checkpoint_id),
                zone_set_labels_request_resource=compute_v1.ZoneSetLabelsRequest(
                    labels=labels,
                    label_fingerprint=str(disk.label_fingerprint),
                ),
            )
        ),
        operation="releasing checkpoint ownership from the current boot disk",
        resource=f"disk {project_id}/{zone}/{disk.name}",
    )
    wait_for_extended_operation(
        operation,
        label=f"disk {project_id}/{zone}/{disk.name}",
        zone=zone,
        timeout=_READY_TIMEOUT_SECONDS,
    )


def _emergency_disks(
    disks: Any,
    project_id: str,
    zone: str,
    *,
    instance_id: str,
    checkpoint_id: str,
) -> list[Any]:
    resources = call_google(
        lambda: list(disks.list(project=project_id, zone=zone)),
        operation="listing checkpoint emergency disks",
        resource=f"zone {project_id}/{zone}",
    )
    return [
        disk
        for disk in resources
        if dict(disk.labels or {}).get(_MANAGED_LABEL) == _MANAGED_VALUE
        and dict(disk.labels or {}).get(_VM_ID_LABEL) == instance_id
        and dict(disk.labels or {}).get(_CHECKPOINT_ID_LABEL) == checkpoint_id
        and dict(disk.labels or {}).get(_ROLE_LABEL) == "emergency"
    ]


def _owned_checkpoint_disks(
    disks: Any,
    project_id: str,
    zone: str,
    *,
    instance_id: str,
    checkpoint_id: str,
) -> list[Any]:
    resources = call_google(
        lambda: list(disks.list(project=project_id, zone=zone)),
        operation="listing retained checkpoint disks",
        resource=f"zone {project_id}/{zone}",
    )
    return [
        disk
        for disk in resources
        if dict(disk.labels or {}).get(_MANAGED_LABEL) == _MANAGED_VALUE
        and dict(disk.labels or {}).get(_VM_ID_LABEL) == instance_id
        and dict(disk.labels or {}).get(_CHECKPOINT_ID_LABEL) == checkpoint_id
    ]


def _snapshot_identifier(snapshot: Any) -> str:
    snapshot_id = provider_resource_id(snapshot.id)
    if not snapshot.name or snapshot_id is None:
        raise StateError("GCE checkpoint snapshot has incomplete provider identity")
    return f"{snapshot.name}|{snapshot_id}"


def _split_identifier(identifier: str) -> tuple[str, str]:
    name, separator, snapshot_id = identifier.rpartition("|")
    if not separator or not name or provider_resource_id(snapshot_id) is None:
        raise StateError("GCE checkpoint descriptor is incomplete")
    return name, snapshot_id


def _labels(*, instance_id: str, name: str) -> dict[str, str]:
    return {
        _MANAGED_LABEL: _MANAGED_VALUE,
        _VM_ID_LABEL: instance_id,
        _NAME_HASH_LABEL: _name_hash(name),
    }


def _name_hash(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def _artifact_name(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _request_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "agentworks:gce-checkpoint:" + ":".join(parts)))


def _operation_hash(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode()).hexdigest()[:32]


def _encode_name(name: str) -> str:
    encoded = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
    return _NAME_DESCRIPTION_PREFIX + encoded


def _decode_name(description: str) -> str | None:
    if not description.startswith(_NAME_DESCRIPTION_PREFIX):
        return None
    encoded = description.removeprefix(_NAME_DESCRIPTION_PREFIX)
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
