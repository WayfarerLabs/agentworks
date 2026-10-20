"""Public GCE creation requests and exact provider receipt projections."""

from __future__ import annotations

import re
import shlex
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.gcp._activation_operation import ActivationState, operation_state
from agentworks.plugins.gcp._native_identity import canonical_path
from agentworks.plugins.gcp._native_route_evidence import HOSTS, OPERATION, numeric_id
from agentworks.vms.identity import VM_INSTANCE_MARKER_PATH, validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.plugins.gcp.names import GceNames

STARTUP_MARKER = "/var/lib/agentworks/gce-creation-v1.complete"
ACCESS_CONFIG = "External NAT"


def component(value: object) -> str:
    """Validate config/provider components before they enter fixed REST routes."""
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value) is None:
        raise ValidationError("GCE creation resource component is invalid")
    return value


def exact_resource(value: dict[str, Any], path: str, kind: str, resource_id: str | None = None) -> str:
    """Select a provider resource, optionally requiring its receipt numeric ID."""
    found = numeric_id(value.get("id"))
    if (
        value.get("kind") != f"compute#{kind}"
        or value.get("name") != path.rsplit("/", 1)[-1]
        or value.get("selfLink") not in tuple(f"{host}/compute/v1/{path}" for host in HOSTS)
        or (resource_id is not None and found != resource_id)
    ):
        raise StateError("GCE creation resource identity changed")
    return found


def receipt(
    value: dict[str, Any],
    *,
    scope: str,
    path: str,
    request_id: str,
    deleting: bool,
    resource_id: str | None,
    operation: str | None = None,
) -> tuple[str, str]:
    """Associate only the request UUID, exact target path and provider target ID."""
    name = value.get("name")
    target = numeric_id(value.get("targetId"))
    if (
        value.get("kind") != "compute#operation"
        or type(name) is not str
        or OPERATION.fullmatch(name) is None
        or (operation is not None and name != operation)
        or value.get("clientOperationId") != request_id
        or value.get("operationType") != ("delete" if deleting else "insert")
        or value.get("targetLink") not in tuple(f"{host}/compute/v1/{path}" for host in HOSTS)
        or (resource_id is not None and target != resource_id)
        or "region" in value
        or (
            "selfLink" in value
            and value["selfLink"] not in tuple(f"{host}/compute/v1/{scope}/operations/{name}" for host in HOSTS)
        )
        or ("/zones/" not in scope and "zone" in value)
        or ("zone" in value and value["zone"] not in tuple(f"{host}/compute/v1/{scope}" for host in HOSTS))
    ):
        raise ValidationError("GCE creation operation is not associated with this request")
    return name, target


def completion(value: dict[str, Any]) -> ActivationState:
    try:
        return operation_state(value)
    except (ValueError, TypeError, RecursionError):
        raise ValidationError("GCE creation operation completion is invalid") from None


def startup_script(request: ProvisionRequest) -> str:
    """Install only public early launch prerequisites through GCE startup metadata."""
    marker = validate_vm_instance_marker(request.instance_marker)
    user = request.admin_username
    if type(user) is not str or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) is None:
        raise ValidationError("GCE creation administrative account is invalid")
    script = f"""#!/bin/bash
set -euo pipefail
if test -f {STARTUP_MARKER}; then
    test "$(cat {VM_INSTANCE_MARKER_PATH})" = {marker}
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 sudo util-linux openssh-server
/usr/bin/python3 -I -S -B -c 'import sys; assert sys.version_info >= (3,11)'
id -u {shlex.quote(user)} >/dev/null 2>&1 || useradd -m -s /bin/bash {shlex.quote(user)}
printf '%s\\n' {shlex.quote(user + " ALL=(ALL) NOPASSWD: ALL")} > /etc/sudoers.d/90-agentworks-creation
chmod 0440 /etc/sudoers.d/90-agentworks-creation
visudo -cf /etc/sudoers.d/90-agentworks-creation
install -d -m 0755 /var/lib/agentworks
if test -e {VM_INSTANCE_MARKER_PATH}; then
    test "$(cat {VM_INSTANCE_MARKER_PATH})" = {marker}
else
    printf '%s\\n' {marker} > {VM_INSTANCE_MARKER_PATH}
    chmod 0644 {VM_INSTANCE_MARKER_PATH}
fi
test -x /usr/bin/setsid
test -x /usr/bin/setpriv
test -x /usr/bin/flock
test -d /tmp -a -w /tmp
touch {STARTUP_MARKER}
chmod 0644 {STARTUP_MARKER}
"""
    if len(script.encode("utf-8")) > 256 * 1024:
        raise ValidationError("GCE public creation startup script exceeds its bound")
    return script


def firewall_body(names: GceNames, network: str, prefixes: tuple[str, ...], *, allow: bool) -> dict[str, Any]:
    return {
        "name": names.allow_rule if allow else names.deny_rule,
        "network": network,
        "direction": "INGRESS",
        "disabled": False,
        "priority": 0 if allow else 1,
        "targetTags": [names.network_tag],
        "sourceRanges": list(prefixes) if allow else ["0.0.0.0/0"],
        "allowed" if allow else "denied": (
            [{"IPProtocol": "tcp", "ports": ["22"]}] if allow else [{"IPProtocol": "all"}]
        ),
    }


def instance_body(
    request: ProvisionRequest,
    names: GceNames,
    *,
    machine: str,
    image: str,
    disk_type: str,
    network: str,
    subnet: str | None,
    disk_name: str,
) -> dict[str, Any]:
    if (
        type(request.ssh_public_key) is not str
        or not request.ssh_public_key.strip()
        or any(character in request.ssh_public_key for character in ("\r", "\n", "\0"))
        or len(request.ssh_public_key.encode("utf-8")) > 16_384
    ):
        raise ValidationError("GCE creation requires one bounded public SSH key")
    interface: dict[str, Any] = {
        "stackType": "IPV4_ONLY",
        "network": network,
        "accessConfigs": [{"name": ACCESS_CONFIG, "type": "ONE_TO_ONE_NAT", "networkTier": "PREMIUM"}],
    }
    if subnet is not None:
        interface["subnetwork"] = subnet
    return {
        "name": names.backend_name,
        "machineType": machine,
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "type": "PERSISTENT",
                "initializeParams": {
                    "diskName": disk_name,
                    "sourceImage": image,
                    "diskType": disk_type,
                    "diskSizeGb": str(request.disk_gib),
                },
            }
        ],
        "networkInterfaces": [interface],
        "tags": {"items": [names.network_tag]},
        "metadata": {
            "items": [
                {"key": "ssh-keys", "value": f"{request.admin_username}:{request.ssh_public_key}"},
                {"key": "block-project-ssh-keys", "value": "TRUE"},
                {"key": "enable-oslogin", "value": "FALSE"},
                {"key": "startup-script", "value": startup_script(request)},
            ]
        },
        "serviceAccounts": [],
    }


def instance_endpoint(
    value: dict[str, Any],
    *,
    project: str,
    zone: str,
    name: str,
    resource_id: str,
    network: str,
    subnet: str | None,
    tag: str,
    disk_path: str,
) -> tuple[str | None, str]:
    """Read the receipt-associated VM's one primary NAT and persistent boot disk."""
    exact_resource(value, f"projects/{project}/zones/{zone}/instances/{name}", "instance", resource_id)
    interfaces = value.get("networkInterfaces")
    tags = value.get("tags", {}).get("items") if type(value.get("tags")) is dict else None
    if (
        type(interfaces) is not list
        or len(interfaces) != 1
        or type(interfaces[0]) is not dict
        or tags != [tag]
        or value.get("serviceAccounts", []) != []
    ):
        raise StateError("GCE created VM network, disks or service identity changed")
    interface = interfaces[0]
    if canonical_path(interface.get("network"), project_id=project, kind="network") != network:
        raise StateError("GCE created VM network changed")
    actual_subnet = interface.get("subnetwork")
    if subnet is not None and canonical_path(actual_subnet, project_id=project, kind="subnet") != subnet:
        raise StateError("GCE created VM subnet changed")
    configs = interface.get("accessConfigs")
    boot_disk_path(value, disk_path)
    if (
        type(configs) is not list
        or len(configs) != 1
        or type(configs[0]) is not dict
        or configs[0].get("name") != ACCESS_CONFIG
        or configs[0].get("type") != "ONE_TO_ONE_NAT"
    ):
        raise StateError("GCE created VM primary NAT or boot disk changed")
    endpoint = configs[0].get("natIP")
    if endpoint is not None:
        try:
            if type(endpoint) is not str or str(IPv4Address(endpoint)) != endpoint:
                raise ValueError("invalid IPv4")
        except ValueError:
            raise ValidationError("GCE created VM public endpoint is invalid") from None
    return endpoint, disk_path


def boot_disk_path(value: dict[str, Any], expected: str) -> str:
    """Require exactly the original auto-delete persistent boot attachment."""
    disks = value.get("disks")
    if type(disks) is not list or len(disks) != 1 or type(disks[0]) is not dict:
        raise StateError("GCE created VM boot attachment changed")
    disk = disks[0]
    if (
        disk.get("boot") is not True
        or disk.get("autoDelete") is not True
        or disk.get("type") != "PERSISTENT"
        or disk.get("source") not in tuple(f"{host}/compute/v1/{expected}" for host in HOSTS)
    ):
        raise StateError("GCE created VM boot disk changed")
    return expected
