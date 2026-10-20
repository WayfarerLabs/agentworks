"""Read-only provider shape and existing-network selection before GCE inserts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import AlreadyExistsError, ConfigError, ValidationError
from agentworks.plugins.gcp._creation_network import check_policy
from agentworks.plugins.gcp._creation_shapes import component, exact_resource
from agentworks.plugins.gcp._native_identity import canonical_path
from agentworks.plugins.gcp.config import IMAGE_FAMILIES, IMAGE_PROJECT, machine_catalog, select_machine_type

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp._creation_api import CreationAPI
    from agentworks.plugins.gcp.config import GcpGCEConfig
    from agentworks.plugins.gcp.names import GceNames


@dataclass(frozen=True, slots=True)
class CreationSelection:
    project: str
    zone: str
    network: str
    subnet: str | None
    machine: str
    image: str
    disk_type: str
    disk_path: str


def select(
    api: CreationAPI, config: GcpGCEConfig, request: ProvisionRequest, names: GceNames,
    prefixes: tuple[str, ...], deadline: Deadline,
    *, disk_name: str,
) -> CreationSelection:
    project, zone = component(config.project_id), component(config.zone)
    scope = f"projects/{project}/zones/{zone}"
    disk_path = f"{scope}/disks/{component(disk_name)}"
    if api.get(f"projects/{project}", deadline) is None:
        raise ConfigError("GCE creation project is unavailable")
    live_zone = api.get(scope, deadline)
    assert live_zone is not None
    exact_resource(live_zone, scope, "zone")
    region = zone.rsplit("-", 1)[0]
    subnet: str | None = None
    if config.subnet is None:
        network = f"projects/{project}/global/networks/default"
    else:
        subnet = f"projects/{project}/regions/{region}/subnetworks/{component(config.subnet)}"
        value = api.get(subnet, deadline)
        assert value is not None
        exact_resource(value, subnet, "subnetwork")
        network = canonical_path(value.get("network"), project_id=project, kind="network")
    check_policy(api, project, network, names.network_tag, prefixes, deadline, owned={})
    for path in (
        f"{scope}/instances/{names.backend_name}",
        disk_path,
        f"projects/{project}/global/firewalls/{names.deny_rule}",
        f"projects/{project}/global/firewalls/{names.allow_rule}",
    ):
        if api.get(path, deadline, missing=True) is not None:
            raise AlreadyExistsError("GCE creation resource name is already occupied")
    selected = select_machine_type(machine_catalog(config), cpus=request.cpus, memory_gib=request.memory_gib)
    machine = f"{scope}/machineTypes/{component(selected.type)}"
    shape = api.get(machine, deadline)
    assert shape is not None
    exact_resource(shape, machine, "machineType")
    if (
        type(shape.get("guestCpus")) is not int or shape["guestCpus"] != selected.cpus
        or type(shape.get("memoryMb")) is not int or shape["memoryMb"] != selected.memory_gib * 1024
        or ("architecture" in shape and (
            type(shape["architecture"]) is not str or shape["architecture"].lower() != selected.arch
        ))
        or shape.get("maximumPersistentDisks") == 0 or shape.get("accelerators", []) != []
    ):
        raise ConfigError("GCE selected machine does not match the declared CPU, memory, architecture or disk contract")
    family = IMAGE_FAMILIES[request.debian_release][selected.arch]
    image = api.get(f"projects/{IMAGE_PROJECT}/global/images/family/{family}", deadline)
    assert image is not None
    image_name = component(image.get("name"))
    image_path = f"projects/{IMAGE_PROJECT}/global/images/{image_name}"
    exact_resource(image, image_path, "image")
    if type(image.get("architecture")) is not str or image["architecture"].lower() != selected.arch:
        raise ConfigError("GCE public Debian image architecture does not match the selected machine")
    disk_type = f"{scope}/diskTypes/pd-balanced"
    disk = api.get(disk_type, deadline)
    assert disk is not None
    exact_resource(disk, disk_type, "diskType")
    if any(type(value) is not int or value <= 0 for value in (request.cpus, request.memory_gib, request.disk_gib)):
        raise ValidationError("GCE creation requires positive CPU, memory and persistent disk sizes")
    return CreationSelection(project, zone, network, subnet, machine, image_path, disk_type, disk_path)
