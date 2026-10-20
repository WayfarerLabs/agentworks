"""Bounded provider observations for the concrete creation rollback sequence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentworks.errors import StateError
from agentworks.execution.carriers._proxmox_http import _valid_digest
from agentworks.plugins.proxmox._creation import CloneAssociation, _canonical_uuid, _smbios_uuid

_DRIVE = re.compile(r"(?:(?:scsi|virtio|sata|ide|unused)[0-9]+|efidisk0|tpmstate0)")
_MAX_DISKS = 128


@dataclass(frozen=True, slots=True)
class RollbackDisk:
    key: str
    volume: str
    raw: str


def associated_config(
    config: dict[str, object], association: CloneAssociation, marker: str, generation: str | None
) -> str:
    """Bind an external current config to the original clone and captured generation."""
    if (
        len(config) > 512
        or "lock" in config
        or config.get("description") != f"agentworks-instance:{marker}"
        or _smbios_uuid(config) != association.smbios_uuid
        or not _valid_digest(config.get("digest"))
    ):
        raise StateError("Proxmox rollback clone association is unproved")
    observed = _canonical_uuid(config.get("vmgenid"))
    if generation is not None and observed != generation:
        raise StateError("Proxmox rollback generation changed")
    return observed


def disks(config: dict[str, object], storage: str) -> tuple[RollbackDisk, ...]:
    """Decode exact current config drives, refusing non-image and ambiguous shapes."""
    result: list[RollbackDisk] = []
    if "vmstate" in config or config.get("args"):
        raise StateError("Proxmox rollback cannot inventory saved state or custom drive arguments")
    for key, raw in sorted(config.items()):
        if _DRIVE.fullmatch(key) is None:
            if key.startswith(("scsi", "virtio", "sata", "ide", "unused", "efidisk", "tpmstate")) and key not in (
                "scsihw",
                "ide",
                "sata",
                "scsi",
                "virtio",
            ):
                raise StateError("Proxmox rollback drive key is unsupported")
            continue
        if type(raw) is not str or not raw or len(raw) > 2048:
            raise StateError("Proxmox rollback drive is unavailable")
        properties: dict[str, str] = {}
        for index, part in enumerate(raw.split(",")):
            name, separator, value = part.partition("=")
            if not separator and index == 0:
                name, value = "file", part
            elif not separator:
                raise StateError("Proxmox rollback drive is ambiguous")
            if not name or name in properties or not value:
                raise StateError("Proxmox rollback drive is ambiguous")
            properties[name] = value
        volume = properties.get("file", "")
        if volume == "none" and properties.get("media") == "cdrom":
            continue
        if (
            not volume.startswith(storage + ":")
            or len(volume) > 512
            or re.fullmatch(r"[A-Za-z0-9_.:/-]+", volume) is None
            or any(part in (".", "..") for part in volume.split("/"))
            or properties.get("media", "disk") not in ("disk", "cdrom")
            or properties.get("shared", "0") != "0"
        ):
            raise StateError("Proxmox rollback requires images on the configured creation storage")
        result.append(RollbackDisk(key, volume, raw))
    if len(result) > _MAX_DISKS or len({disk.volume for disk in result}) != len(result):
        raise StateError("Proxmox rollback drive inventory is ambiguous or exceeds its bound")
    return tuple(result)


def require_no_snapshots_or_pending(snapshots: object, pending: object) -> None:
    """Reject snapshots and actual pending/deleted values, allowing unchanged pending rows."""
    if (
        type(snapshots) is not list
        or len(snapshots) != 1
        or any(type(row) is not dict or row.get("name") != "current" for row in snapshots)
    ):
        raise StateError("Proxmox rollback snapshot inventory is unsupported")
    if type(pending) is not list or len(pending) > 512:
        raise StateError("Proxmox rollback pending inventory is unsupported")
    for row in pending:
        if (
            type(row) is not dict
            or type(row.get("key")) is not str
            or "pending" in row
            or ("delete" in row and (type(row["delete"]) is not int or row["delete"] != 0))
        ):
            raise StateError("Proxmox rollback has unproved pending allocations")


def require_permissions(value: object, path: str, privileges: tuple[str, ...]) -> None:
    """Interpret external effective privileges as membership; zero means non-propagating grant."""
    if type(value) is not dict or len(value) > 1 or type(value.get(path)) is not dict:
        raise StateError("Proxmox rollback effective permissions are unavailable")
    grants = value[path]
    if any(type(grants.get(privilege)) is not int or grants[privilege] not in (0, 1) for privilege in privileges):
        raise StateError("Proxmox rollback effective permissions are insufficient")


def image_volumes(value: object, storage: str, vmid: int) -> frozenset[str]:
    """Read positive owner-image evidence from the fixed content=images,vmid listing.

    PVE 8/9 Content.pm returns volid, optional integer vmid, format and optional
    parent. A full creation clone must expose every exact config volume here.
    The caller checks effective permissions around observations, before deleting
    the VM whose pool membership provides those volume permissions.
    """
    if type(value) is not list or len(value) > _MAX_DISKS:
        raise StateError("Proxmox rollback image inventory is unavailable or exceeds its bound")
    result: set[str] = set()
    for row in value:
        if type(row) is not dict:
            raise StateError("Proxmox rollback image row is invalid")
        volume = row.get("volid")
        if (
            type(volume) is not str
            or len(volume) > 512
            or not volume.startswith(storage + ":")
            or type(row.get("vmid")) is not int
            or row["vmid"] != vmid
            or row.get("format") not in ("raw", "qcow2", "vmdk")
            or row.get("parent") not in (None, "")
            or volume in result
        ):
            raise StateError("Proxmox rollback image ownership or shape is unproved")
        result.add(volume)
    return frozenset(result)
