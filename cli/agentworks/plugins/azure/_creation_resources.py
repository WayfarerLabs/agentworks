"""Public Azure creation inputs and exact, attempt-specific resource paths."""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.bootstrap_script import generate_instance_marker_installer
from agentworks.capabilities.vm_platform.cloud_init import generate_cloud_init
from agentworks.capabilities.vm_platform.debian_release import code_owned_release_value
from agentworks.errors import ValidationError
from agentworks.naming import VM_USER_RE
from agentworks.plugins.azure._identity import parse_resource_id
from agentworks.plugins.azure.config import AZURE_IMAGES, _select_vm_size, _size_catalog
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.plugins.azure.config import AzureVMConfig


def public_startup(admin_username: str, marker: str) -> str:
    """Install only the public prerequisites needed before core guest helpers."""
    if VM_USER_RE.fullmatch(admin_username) is None:
        raise ValidationError("Azure creation requires a valid administrative account")
    marker = validate_vm_instance_marker(marker)
    return (
        "set -euo pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -qq\n"
        "apt-get install -y -qq --no-install-recommends python3 sudo util-linux\n"
        "/usr/bin/python3 -I -c 'import base64,bz2,hashlib,json,os,sys,types; "
        "assert sys.version_info >= (3,11)'\n"
        "[ ! -L /tmp ] && [ -d /tmp ]\n"
        "chown 0:0 /tmp\nchmod 1777 /tmp\n"
        f"admin={shlex.quote(admin_username)}\n"
        "id -- \"$admin\" >/dev/null\n"
        "[ ! -L /etc/sudoers.d ] && [ -d /etc/sudoers.d ]\n"
        "[ ! -L /etc/sudoers.d/agentworks-creation ]\n"
        "printf '%s ALL=(ALL) NOPASSWD:ALL\\n' \"$admin\" > /etc/sudoers.d/agentworks-creation\n"
        "chown 0:0 /etc/sudoers.d/agentworks-creation\n"
        "chmod 0440 /etc/sudoers.d/agentworks-creation\n"
        "/usr/sbin/visudo -cf /etc/sudoers.d/agentworks-creation\n"
        + generate_instance_marker_installer(instance_marker=marker)
    )


@dataclass(frozen=True, slots=True)
class CreationResource:
    resource_id: str
    body: dict[str, Any]


def resource_plan(request: ProvisionRequest, config: AzureVMConfig) -> tuple[CreationResource, ...]:
    """Copy nonsecret input; auxiliary names identify only this creation attempt."""
    image = code_owned_release_value(AZURE_IMAGES, request.debian_release, platform_name="azure-vm")
    size = _select_vm_size(_size_catalog(config), cpus=request.cpus, memory_gib=request.memory_gib)
    name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name
    for component in (config.subscription_id, config.resource_group, name):
        if (
            not component
            or component in {".", ".."}
            or any(char in component for char in "/\\?#%")
            or any(ord(char) < 32 or ord(char) == 127 for char in component)
        ):
            raise ValidationError("Azure creation resource path is invalid")
    prefix = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}/providers/"
    attempt = "agw-" + uuid4().hex
    network = prefix + "Microsoft.Network/"
    ip, nsg, vnet, nic = (
        network + kind + "/" + attempt + suffix
        for kind, suffix in (
            ("publicIPAddresses", "-ip"),
            ("networkSecurityGroups", "-nsg"),
            ("virtualNetworks", "-vnet"),
            ("networkInterfaces", "-nic"),
        )
    )
    vm = prefix + "Microsoft.Compute/virtualMachines/" + name
    parse_resource_id(vm)
    disk_name = attempt + "-os"
    common = {"location": config.region, "tags": {"owner": "agentworks", "agentworks-creation": attempt}}
    baseline = {
        "protocol": "*",
        "sourcePortRange": "*",
        "destinationPortRange": "*",
        "sourceAddressPrefix": "*",
        "destinationAddressPrefix": "*",
        "access": "Deny",
        "priority": 200,
        "direction": "Inbound",
    }
    startup = generate_cloud_init(public_startup(request.admin_username, request.instance_marker))
    return (
        CreationResource(
            ip, {**common, "sku": {"name": "Standard"}, "properties": {"publicIPAllocationMethod": "Static"}}
        ),
        CreationResource(
            nsg, {**common, "properties": {"securityRules": [{"name": "deny-all-inbound", "properties": baseline}]}}
        ),
        CreationResource(vnet, {
            **common,
            "properties": {
                "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
                "subnets": [{"name": "default", "properties": {"addressPrefix": "10.0.0.0/24"}}],
            },
        }),
        CreationResource(nic, {
            **common,
            "properties": {
                "networkSecurityGroup": {"id": nsg},
                "ipConfigurations": [{
                    "name": "default",
                    "properties": {"subnet": {"id": vnet + "/subnets/default"}, "publicIPAddress": {"id": ip}},
                }],
            },
        }),
        CreationResource(vm, {
            **common,
            "properties": {
                "hardwareProfile": {"vmSize": size.name},
                "storageProfile": {
                    "imageReference": {
                        "publisher": image.publisher, "offer": image.offer,
                        "sku": image.sku, "version": image.version,
                    },
                    "osDisk": {
                        "name": disk_name, "createOption": "FromImage",
                        "diskSizeGB": max(request.disk_gib, image.os_disk_floor_gib), "deleteOption": "Delete",
                        "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                    },
                },
                "osProfile": {
                    "computerName": request.hostname, "adminUsername": request.admin_username,
                    "customData": base64.b64encode(startup.encode()).decode("ascii"),
                    "linuxConfiguration": {
                        "disablePasswordAuthentication": True,
                        "ssh": {"publicKeys": [{
                            "path": f"/home/{request.admin_username}/.ssh/authorized_keys",
                            "keyData": request.ssh_public_key,
                        }]},
                    },
                },
                "networkProfile": {"networkInterfaces": [{"id": nic}]},
            },
        }),
    )


def planned_disk_id(plan: tuple[CreationResource, ...]) -> str:
    vm = plan[-1]
    prefix = vm.resource_id.split("/Microsoft.Compute/", 1)[0]
    return prefix + "/Microsoft.Compute/disks/" + str(vm.body["properties"]["storageProfile"]["osDisk"]["name"])
