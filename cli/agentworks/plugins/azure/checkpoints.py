"""Agentworks-owned Azure OS-disk checkpoint lifecycle."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.errors import StateError
from agentworks.plugins.azure.network import wrap_azure_error

_MANAGED_TAG = "agentworks-managed"
_MANAGED_VALUE = "vm-checkpoint"
_VM_ID_TAG = "agentworks-vm-id"
_NAME_TAG = "agentworks-checkpoint-name"
_CHECKPOINT_TAG = "agentworks-checkpoint-id"
_ROLE_TAG = "agentworks-checkpoint-role"
_OPERATION_TAG = "agentworks-restore-operation"
_CREATE_OPERATION_TAG = "agentworks-create-operation"
_READY_POLL_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 1800.0


def create_checkpoint(
    compute: Any,
    *,
    resource_group: str,
    vm_name: str,
    name: str,
    operation_id: str,
    resume: bool,
) -> CheckpointDescriptor:
    """Create or discover a completed snapshot of the stopped VM's OS disk."""
    vm = _vm(compute, resource_group, vm_name)
    _require_deallocated(compute, resource_group, vm_name)
    vm_id = _required_text(vm, "vm_id", f"Azure VM '{vm_name}' has no provider incarnation ID")
    disk_id = _os_disk_id(vm, vm_name)
    snapshot_name = _artifact_name("agw-checkpoint", vm_id, name)
    existing = _get_optional(compute.snapshots, resource_group, snapshot_name)
    del resume  # The deterministic resource name is the Azure replay boundary.
    tags = {**_tags(vm_id=vm_id, name=name), _CREATE_OPERATION_TAG: operation_id}
    body = {
        "location": _required_text(vm, "location", f"Azure VM '{vm_name}' has no location"),
        "creation_data": {"create_option": "Copy", "source_resource_id": disk_id},
        "tags": tags,
    }
    if existing is not None:
        _verify_snapshot_ownership(existing, vm_id=vm_id, name=name)
        if _resource_tags(existing).get(_CREATE_OPERATION_TAG) != operation_id:
            raise StateError(f"Azure checkpoint '{name}' belongs to another create operation")
        existing = _wait_for_resource(
            compute.snapshots,
            resource_group,
            snapshot_name,
            resource_label=f"Azure checkpoint '{name}'",
        )
    else:
        try:
            existing = compute.snapshots.begin_create_or_update(resource_group, snapshot_name, body).result()
        except Exception as exc:
            raise wrap_azure_error(exc) from exc
    _verify_snapshot(
        existing,
        vm_id=vm_id,
        name=name,
        expected_source_disk_id=disk_id,
    )
    return CheckpointDescriptor(name=name, identifier=_snapshot_identifier(existing, snapshot_name))


def list_checkpoints(
    compute: Any,
    *,
    resource_group: str,
    vm_name: str,
) -> tuple[CheckpointDescriptor, ...]:
    """List snapshots belonging to the exact live Azure VM incarnation."""
    vm = _vm(compute, resource_group, vm_name)
    vm_id = _required_text(vm, "vm_id", f"Azure VM '{vm_name}' has no provider incarnation ID")
    try:
        snapshots = list(compute.snapshots.list_by_resource_group(resource_group))
    except Exception as exc:
        raise wrap_azure_error(exc) from exc
    result = []
    for snapshot in snapshots:
        tags = _resource_tags(snapshot)
        name = tags.get(_NAME_TAG)
        if tags.get(_MANAGED_TAG) != _MANAGED_VALUE or tags.get(_VM_ID_TAG) != vm_id or not name:
            continue
        result.append(
            CheckpointDescriptor(
                name=name,
                identifier=_snapshot_identifier(
                    snapshot,
                    _required_text(snapshot, "name", "Azure snapshot has no name"),
                ),
            )
        )
    return tuple(sorted(result, key=lambda checkpoint: checkpoint.name))


def restore_checkpoint(
    compute: Any,
    *,
    resource_group: str,
    vm_name: str,
    checkpoint: CheckpointDescriptor,
    operation_id: str,
) -> None:
    """Swap a checkpoint-derived OS disk onto the same deallocated VM."""
    vm = _vm(compute, resource_group, vm_name)
    _require_deallocated(compute, resource_group, vm_name)
    vm_id = _required_text(vm, "vm_id", f"Azure VM '{vm_name}' has no provider incarnation ID")
    snapshot = _snapshot_by_identifier(compute, resource_group, checkpoint.identifier)
    _verify_snapshot(snapshot, vm_id=vm_id, name=checkpoint.name)
    snapshot_id = _required_text(snapshot, "id", f"Azure checkpoint '{checkpoint.name}' has no resource ID")
    snapshot_unique_id = _required_text(
        snapshot,
        "unique_id",
        f"Azure checkpoint '{checkpoint.name}' has no provider incarnation ID",
    )
    replacement_name = _artifact_name("agw-restore", vm_id, snapshot_unique_id, operation_id)
    current_disk_id = _os_disk_id(vm, vm_name)
    existing_replacement = _get_optional(compute.disks, resource_group, replacement_name)
    if existing_replacement is not None:
        _verify_replacement_ownership(
            existing_replacement,
            vm_id=vm_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_unique_id,
            operation_id=operation_id,
        )
        existing_replacement = _wait_for_resource(
            compute.disks,
            resource_group,
            replacement_name,
            resource_label=f"Azure replacement disk '{replacement_name}'",
        )
        _verify_replacement(
            existing_replacement,
            vm_id=vm_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_unique_id,
            operation_id=operation_id,
        )
    else:
        source_disk = _disk(compute, resource_group, current_disk_id)
        body = {
            "location": _required_text(snapshot, "location", f"Azure checkpoint '{checkpoint.name}' has no location"),
            "creation_data": {"create_option": "Copy", "source_resource_id": snapshot_id},
            "tags": {
                **_tags(vm_id=vm_id, name=checkpoint.name),
                _CHECKPOINT_TAG: snapshot_unique_id,
                _ROLE_TAG: "restored",
                _OPERATION_TAG: operation_id,
            },
        }
        sku = getattr(source_disk, "sku", None)
        if sku is not None:
            body["sku"] = sku
        try:
            existing_replacement = compute.disks.begin_create_or_update(
                resource_group,
                replacement_name,
                body,
            ).result()
        except Exception as exc:
            raise wrap_azure_error(exc) from exc
        _verify_replacement(
            existing_replacement,
            vm_id=vm_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_unique_id,
            operation_id=operation_id,
        )
    replacement_id = _required_text(
        existing_replacement,
        "id",
        f"Azure replacement disk '{replacement_name}' has no resource ID",
    )
    if current_disk_id.lower() == replacement_id.lower():
        _cleanup_superseded_disks(
            compute,
            resource_group,
            vm_id=vm_id,
            checkpoint_id=snapshot_unique_id,
            operation_id=operation_id,
            current_disk_id=current_disk_id,
        )
        return
    emergency = _existing_emergency_disk(compute, resource_group, vm_id=vm_id, checkpoint_id=snapshot_unique_id)
    if emergency is None:
        _tag_emergency_disk(
            compute,
            resource_group,
            current_disk_id,
            vm_id=vm_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_unique_id,
        )
    elif _required_text(emergency, "id", "Azure emergency disk has no resource ID").lower() != current_disk_id.lower():
        _tag_superseded_disk(
            compute,
            resource_group,
            current_disk_id,
            vm_id=vm_id,
            name=checkpoint.name,
            checkpoint_id=snapshot_unique_id,
            operation_id=operation_id,
        )
    update = {"storage_profile": {"os_disk": {"managed_disk": {"id": replacement_id}}}}
    try:
        compute.virtual_machines.begin_update(resource_group, vm_name, update).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc
    converged = _vm(compute, resource_group, vm_name)
    if _os_disk_id(converged, vm_name).lower() != replacement_id.lower():
        raise StateError(f"Azure VM '{vm_name}' was not proven restored to checkpoint '{checkpoint.name}'")
    _cleanup_superseded_disks(
        compute,
        resource_group,
        vm_id=vm_id,
        checkpoint_id=snapshot_unique_id,
        operation_id=operation_id,
        current_disk_id=replacement_id,
    )


def delete_checkpoint(
    compute: Any,
    *,
    resource_group: str,
    vm_name: str,
    checkpoint: CheckpointDescriptor,
) -> None:
    """Delete an owned snapshot and its detached emergency OS disk."""
    vm = _vm(compute, resource_group, vm_name)
    vm_id = _required_text(vm, "vm_id", f"Azure VM '{vm_name}' has no provider incarnation ID")
    checkpoint_unique_id = _identifier_unique_id(checkpoint.identifier)
    snapshot = _snapshot_by_identifier_optional(compute, resource_group, checkpoint.identifier)
    if snapshot is not None:
        _verify_snapshot_ownership(snapshot, vm_id=vm_id, name=checkpoint.name)
        checkpoint_unique_id = _required_text(snapshot, "unique_id", "Azure snapshot has no provider incarnation ID")
    _release_checkpoint_disks(
        compute,
        resource_group,
        vm_id=vm_id,
        checkpoint_id=checkpoint_unique_id,
        current_disk_id=_os_disk_id(vm, vm_name),
    )

    if snapshot is not None:
        snapshot_name = _required_text(snapshot, "name", "Azure snapshot has no name")
        try:
            compute.snapshots.begin_delete(resource_group, snapshot_name).result()
        except Exception as exc:
            if not _is_not_found(exc):
                raise wrap_azure_error(exc) from exc
    if _snapshot_by_identifier_optional(compute, resource_group, checkpoint.identifier) is not None:
        raise StateError(f"Azure checkpoint '{checkpoint.name}' was not proven absent")


def _vm(compute: Any, resource_group: str, vm_name: str) -> Any:
    try:
        return compute.virtual_machines.get(resource_group, vm_name)
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _disk(compute: Any, resource_group: str, disk_id: str) -> Any:
    name = disk_id.rstrip("/").rsplit("/", 1)[-1]
    try:
        return compute.disks.get(resource_group, name)
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _os_disk_id(vm: Any, vm_name: str) -> str:
    try:
        disk_id = vm.storage_profile.os_disk.managed_disk.id
    except AttributeError:
        disk_id = None
    if not disk_id:
        raise StateError(f"Azure VM '{vm_name}' has no identifiable managed OS disk")
    return str(disk_id)


def _get_optional(group: Any, resource_group: str, name: str) -> Any | None:
    try:
        return group.get(resource_group, name)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise wrap_azure_error(exc) from exc


def _wait_for_resource(group: Any, resource_group: str, name: str, *, resource_label: str) -> Any:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while True:
        resource = _get_optional(group, resource_group, name)
        if resource is None:
            raise StateError(f"{resource_label} disappeared before completion")
        state = str(getattr(resource, "provisioning_state", "")).lower()
        if state == "succeeded":
            return resource
        if state in {"failed", "canceled"}:
            raise StateError(f"{resource_label} ended in provisioning state '{state}'")
        if time.monotonic() >= deadline:
            raise StateError(
                f"{resource_label} did not complete in time",
                hint="inspect the resource before retrying because its outcome is not yet known",
            )
        time.sleep(_READY_POLL_SECONDS)


def _snapshot_by_identifier(compute: Any, resource_group: str, identifier: str) -> Any:
    snapshot = _snapshot_by_identifier_optional(compute, resource_group, identifier)
    if snapshot is None:
        raise StateError(f"Azure checkpoint artifact '{identifier}' no longer exists")
    return snapshot


def _snapshot_by_identifier_optional(compute: Any, resource_group: str, identifier: str) -> Any | None:
    resource_id, unique_id = _split_identifier(identifier)
    name = resource_id.rstrip("/").rsplit("/", 1)[-1]
    snapshot = _get_optional(compute.snapshots, resource_group, name)
    if snapshot is None:
        return None
    if _required_text(snapshot, "id", "Azure snapshot has no resource ID").lower() != resource_id.lower():
        raise StateError("Azure checkpoint resource identity does not match its persisted descriptor")
    if _required_text(snapshot, "unique_id", "Azure snapshot has no provider incarnation ID") != unique_id:
        raise StateError("Azure checkpoint was replaced by another provider resource with the same name")
    return snapshot


def _verify_snapshot(
    snapshot: Any,
    *,
    vm_id: str,
    name: str,
    expected_source_disk_id: str | None = None,
) -> None:
    _verify_snapshot_ownership(snapshot, vm_id=vm_id, name=name)
    if str(getattr(snapshot, "provisioning_state", "")).lower() != "succeeded":
        raise StateError(f"Azure checkpoint '{name}' is not complete")
    if expected_source_disk_id is not None:
        try:
            source_disk_id = str(snapshot.creation_data.source_resource_id)
        except AttributeError:
            source_disk_id = ""
        if source_disk_id.rstrip("/").lower() != expected_source_disk_id.rstrip("/").lower():
            raise StateError(f"Azure checkpoint '{name}' does not match the VM's current OS disk")


def _verify_snapshot_ownership(snapshot: Any, *, vm_id: str, name: str) -> None:
    tags = _resource_tags(snapshot)
    if tags.get(_MANAGED_TAG) != _MANAGED_VALUE or tags.get(_VM_ID_TAG) != vm_id or tags.get(_NAME_TAG) != name:
        raise StateError(f"Azure checkpoint '{name}' does not match the Agentworks VM incarnation")


def _verify_replacement(
    disk: Any,
    *,
    vm_id: str,
    name: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    _verify_replacement_ownership(
        disk,
        vm_id=vm_id,
        name=name,
        checkpoint_id=checkpoint_id,
        operation_id=operation_id,
    )
    if str(getattr(disk, "provisioning_state", "")).lower() != "succeeded":
        raise StateError("Azure checkpoint replacement disk is not complete")


def _verify_replacement_ownership(
    disk: Any,
    *,
    vm_id: str,
    name: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    tags = _resource_tags(disk)
    if (
        tags.get(_MANAGED_TAG) != _MANAGED_VALUE
        or tags.get(_VM_ID_TAG) != vm_id
        or tags.get(_NAME_TAG) != name
        or tags.get(_CHECKPOINT_TAG) != checkpoint_id
        or tags.get(_ROLE_TAG) != "restored"
        or tags.get(_OPERATION_TAG) != operation_id
    ):
        raise StateError("Azure checkpoint replacement disk does not match its persisted provider identities")


def _snapshot_identifier(snapshot: Any, fallback_name: str) -> str:
    resource_id = _required_text(snapshot, "id", f"Azure snapshot '{fallback_name}' has no resource ID")
    unique_id = _required_text(
        snapshot,
        "unique_id",
        f"Azure snapshot '{fallback_name}' has no provider incarnation ID",
    )
    return f"{resource_id}|{unique_id}"


def _split_identifier(identifier: str) -> tuple[str, str]:
    resource_id, separator, unique_id = identifier.rpartition("|")
    if not separator or not resource_id or not unique_id:
        raise StateError("Azure checkpoint descriptor is incomplete")
    return resource_id, unique_id


def _identifier_unique_id(identifier: str) -> str:
    return _split_identifier(identifier)[1]


def _resource_tags(resource: Any) -> Mapping[str, str]:
    tags = getattr(resource, "tags", None)
    return tags if isinstance(tags, Mapping) else {}


def _tags(*, vm_id: str, name: str) -> dict[str, str]:
    return {_MANAGED_TAG: _MANAGED_VALUE, _VM_ID_TAG: vm_id, _NAME_TAG: name}


def _artifact_name(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _tag_emergency_disk(
    compute: Any,
    resource_group: str,
    disk_id: str,
    *,
    vm_id: str,
    name: str,
    checkpoint_id: str,
) -> None:
    disk = _disk(compute, resource_group, disk_id)
    tags = {
        **dict(_resource_tags(disk)),
        **_tags(vm_id=vm_id, name=name),
        _CHECKPOINT_TAG: checkpoint_id,
        _ROLE_TAG: "emergency",
    }
    disk_name = _required_text(disk, "name", "Azure OS disk has no name")
    try:
        compute.disks.begin_update(resource_group, disk_name, {"tags": tags}).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _tag_superseded_disk(
    compute: Any,
    resource_group: str,
    disk_id: str,
    *,
    vm_id: str,
    name: str,
    checkpoint_id: str,
    operation_id: str,
) -> None:
    disk = _disk(compute, resource_group, disk_id)
    tags = {
        **dict(_resource_tags(disk)),
        **_tags(vm_id=vm_id, name=name),
        _CHECKPOINT_TAG: checkpoint_id,
        _ROLE_TAG: "superseded",
        _OPERATION_TAG: operation_id,
    }
    disk_name = _required_text(disk, "name", "Azure OS disk has no name")
    try:
        compute.disks.begin_update(resource_group, disk_name, {"tags": tags}).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _cleanup_superseded_disks(
    compute: Any,
    resource_group: str,
    *,
    vm_id: str,
    checkpoint_id: str,
    operation_id: str,
    current_disk_id: str,
) -> None:
    for disk in _checkpoint_disks(compute, resource_group):
        tags = _resource_tags(disk)
        if (
            tags.get(_MANAGED_TAG) != _MANAGED_VALUE
            or tags.get(_VM_ID_TAG) != vm_id
            or tags.get(_CHECKPOINT_TAG) != checkpoint_id
            or tags.get(_ROLE_TAG) != "superseded"
            or tags.get(_OPERATION_TAG) != operation_id
        ):
            continue
        disk_id = _required_text(disk, "id", "Azure superseded disk has no resource ID")
        if disk_id.lower() == current_disk_id.lower() or getattr(disk, "managed_by", None):
            raise StateError("Azure could not safely release a superseded checkpoint disk")
        try:
            compute.disks.begin_delete(resource_group, _required_text(disk, "name", "Azure disk has no name")).result()
        except Exception as exc:
            if not _is_not_found(exc):
                raise wrap_azure_error(exc) from exc


def _checkpoint_disks(compute: Any, resource_group: str) -> list[Any]:
    try:
        return list(compute.disks.list_by_resource_group(resource_group))
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _release_checkpoint_disks(
    compute: Any,
    resource_group: str,
    *,
    vm_id: str,
    checkpoint_id: str,
    current_disk_id: str,
) -> None:
    for disk in _owned_checkpoint_disks(
        compute,
        resource_group,
        vm_id=vm_id,
        checkpoint_id=checkpoint_id,
    ):
        disk_id = _required_text(disk, "id", "Azure checkpoint disk has no resource ID")
        disk_name = _required_text(disk, "name", "Azure checkpoint disk has no name")
        if disk_id.lower() == current_disk_id.lower():
            _clear_checkpoint_tags(
                compute,
                resource_group,
                disk,
                vm_id=vm_id,
                checkpoint_id=checkpoint_id,
            )
            continue
        if getattr(disk, "managed_by", None):
            raise StateError(
                f"Azure checkpoint disk '{disk_name}' is attached outside the VM's current OS disk",
                hint="Detach or recover the disk before deleting the managed checkpoint.",
            )
        try:
            compute.disks.begin_delete(resource_group, disk_name).result()
        except Exception as exc:
            if not _is_not_found(exc):
                raise wrap_azure_error(exc) from exc
    if _owned_checkpoint_disks(
        compute,
        resource_group,
        vm_id=vm_id,
        checkpoint_id=checkpoint_id,
    ):
        raise StateError("Azure checkpoint disks were not fully released")


def _clear_checkpoint_tags(
    compute: Any,
    resource_group: str,
    disk: Any,
    *,
    vm_id: str,
    checkpoint_id: str,
) -> None:
    tags = dict(_resource_tags(disk))
    if (
        tags.get(_MANAGED_TAG) != _MANAGED_VALUE
        or tags.get(_VM_ID_TAG) != vm_id
        or tags.get(_CHECKPOINT_TAG) != checkpoint_id
    ):
        raise StateError("Azure current OS disk does not match the checkpoint being deleted")
    for key in (_MANAGED_TAG, _VM_ID_TAG, _NAME_TAG, _CHECKPOINT_TAG, _ROLE_TAG, _OPERATION_TAG):
        tags.pop(key, None)
    disk_name = _required_text(disk, "name", "Azure OS disk has no name")
    try:
        compute.disks.begin_update(resource_group, disk_name, {"tags": tags}).result()
    except Exception as exc:
        raise wrap_azure_error(exc) from exc


def _owned_checkpoint_disks(
    compute: Any,
    resource_group: str,
    *,
    vm_id: str,
    checkpoint_id: str,
) -> list[Any]:
    return [
        disk
        for disk in _checkpoint_disks(compute, resource_group)
        if _resource_tags(disk).get(_MANAGED_TAG) == _MANAGED_VALUE
        and _resource_tags(disk).get(_VM_ID_TAG) == vm_id
        and _resource_tags(disk).get(_CHECKPOINT_TAG) == checkpoint_id
    ]


def _existing_emergency_disk(compute: Any, resource_group: str, *, vm_id: str, checkpoint_id: str) -> Any | None:
    disks = _emergency_disks(compute, resource_group, vm_id=vm_id, checkpoint_id=checkpoint_id)
    if len(disks) > 1:
        raise StateError("Azure has multiple emergency disks for one Agentworks checkpoint")
    return disks[0] if disks else None


def _emergency_disks(compute: Any, resource_group: str, *, vm_id: str, checkpoint_id: str) -> list[Any]:
    try:
        disks = list(compute.disks.list_by_resource_group(resource_group))
    except Exception as exc:
        raise wrap_azure_error(exc) from exc
    result = []
    for disk in disks:
        tags = _resource_tags(disk)
        if (
            tags.get(_MANAGED_TAG) == _MANAGED_VALUE
            and tags.get(_VM_ID_TAG) == vm_id
            and tags.get(_CHECKPOINT_TAG) == checkpoint_id
            and tags.get(_ROLE_TAG) == "emergency"
        ):
            result.append(disk)
    return result


def _required_text(resource: Any, field: str, message: str) -> str:
    value = getattr(resource, field, None)
    if not value:
        raise StateError(message)
    return str(value)


def _is_not_found(exc: Exception) -> bool:
    from azure.core.exceptions import ResourceNotFoundError

    return isinstance(exc, ResourceNotFoundError)


def _require_deallocated(compute: Any, resource_group: str, vm_name: str) -> None:
    try:
        view = compute.virtual_machines.instance_view(resource_group, vm_name)
    except Exception as exc:
        raise wrap_azure_error(exc) from exc
    codes = {str(getattr(status, "code", "")).lower() for status in view.statuses or []}
    if "powerstate/deallocated" not in codes:
        raise StateError(f"Azure VM '{vm_name}' must be deallocated before checkpoint operations")
