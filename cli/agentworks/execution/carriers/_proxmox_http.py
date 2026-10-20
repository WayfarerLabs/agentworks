"""Private HTTP worker whose owning carrier enforces its total lifetime.

Connection authority and request bodies arrive on stdin, never process argv.
Only a bounded provider response leaves stdout; errors produce no provider text.
This module exposes no production CLI or credential discovery.
"""

from __future__ import annotations

import json
import math
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from agentworks.naming import VM_USER_RE
from agentworks.ssh_identity import SSHIdentityReadError, parse_public_ssh_identity

if TYPE_CHECKING:
    from http.client import HTTPMessage

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_WORKER_INPUT_BYTES = 8 * 1024 * 1024
# PVE stores the entire UPID as a task-log filename. This is a conservative
# Linux filename-component capacity, not an API schema or identity constraint.
_MAX_TASK_ID_BYTES = 255


class _Endpoint(StrEnum):
    GUEST_AGENT = "guest-agent"
    POWER = "power"
    CURRENT_CONFIG = "current-config"
    VM_START = "vm-start"
    VM_STOP = "vm-stop"
    VM_DESTROY = "vm-destroy"
    VMID_FREE = "vmid-free"
    TASK_STATUS = "task-status"
    GUEST_INFO = "guest-info"
    NEXT_ID = "next-id"
    VM_CLONE = "vm-clone"
    VM_CONFIGURE = "vm-configure"
    VM_RESIZE = "vm-resize"
    VM_REMOVE_DISKS = "vm-remove-disks"
    SNAPSHOTS = "snapshots"
    PENDING = "pending"
    VM_PERMISSIONS = "vm-permissions"
    STORAGE_PERMISSIONS = "storage-permissions"
    STORAGE_IMAGES = "storage-images"


def _valid_storage(value: object) -> bool:
    return type(value) is str and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is not None


def _valid_remove_disks_body(value: object) -> bool:
    """Accept only bounded exact drive keys and the provider's force-delete schema."""
    if type(value) is not dict or set(value) != {"delete", "force", "digest"}:
        return False
    keys = value["delete"]
    if type(keys) is not str or len(keys) > 2048:
        return False
    parts = keys.split(",")
    return (
        0 < len(parts) <= 128
        and len(set(parts)) == len(parts)
        and all(re.fullmatch(r"(?:(?:scsi|virtio|sata|ide|unused)[0-9]+|efidisk0|tpmstate0)", key) for key in parts)
        and type(value["force"]) is int
        and value["force"] == 1
        and _valid_digest(value["digest"])
    )


def _valid_clone_body(value: object) -> bool:
    """Check the fixed clone schema at the HTTP worker input boundary."""
    if type(value) is not dict or set(value) != {"newid", "name", "description", "storage", "pool", "full"}:
        return False
    return (
        type(value["newid"]) is int
        and value["newid"] > 0
        and type(value["full"]) is int
        and value["full"] == 1
        and type(value["name"]) is str
        and len(value["name"]) <= 253
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value["name"]) is not None
        and type(value["description"]) is str
        and re.fullmatch(r"agentworks-instance:[0-9a-f]{32}", value["description"]) is not None
        and all(
            type(value[key]) is str
            and len(value[key]) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value[key]) is not None
            for key in ("storage", "pool")
        )
    )


def _valid_control_timeout(value: object) -> bool:
    if type(value) is not int and type(value) is not float:
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _valid_digest(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _valid_configuration_body(value: object) -> bool:
    """Validate the public, fixed configuration at the worker input boundary."""
    if type(value) is not dict or set(value) != {
        "cores",
        "memory",
        "ciuser",
        "sshkeys",
        "vmgenid",
        "cpu",
        "agent",
        "boot",
        "ipconfig0",
        "digest",
    }:
        return False
    from uuid import UUID

    try:
        generation = UUID(value["vmgenid"])
        public_key = urllib.parse.unquote(value["sshkeys"], errors="strict")
        parse_public_ssh_identity(public_key)
    except (ValueError, TypeError, AttributeError, SSHIdentityReadError):
        return False
    return (
        type(value["cores"]) is int
        and value["cores"] > 0
        and type(value["memory"]) is str
        and re.fullmatch(r"[1-9][0-9]*", value["memory"]) is not None
        and type(value["ciuser"]) is str
        and VM_USER_RE.fullmatch(value["ciuser"]) is not None
        and generation.int != 0
        and str(generation) == value["vmgenid"]
        and urllib.parse.quote(public_key, safe="") == value["sshkeys"]
        and value["cpu"] == "host"
        and value["agent"] == "enabled=1"
        and value["boot"] == "order=scsi0"
        and value["ipconfig0"] == "ip=dhcp"
        and _valid_digest(value["digest"])
    )


def _valid_resize_body(value: object) -> bool:
    """Accept only a digest-guarded absolute size for the creation disk."""
    return (
        type(value) is dict
        and set(value) == {"disk", "size", "digest"}
        and value["disk"] == "scsi0"
        and type(value["size"]) is str
        and re.fullmatch(r"[1-9][0-9]*G", value["size"]) is not None
        and _valid_digest(value["digest"])
    )


def _valid_task_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > _MAX_TASK_ID_BYTES:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_TASK_ID_BYTES
    except UnicodeEncodeError:
        return False


def _task_component(value: object) -> str:
    if not _valid_task_id(value):
        raise ValueError("Task status requires a bounded nonempty task identifier")
    assert isinstance(value, str)
    # quote always preserves unreserved dots; encode them too so a literal
    # identifier cannot put dot segments on the request path.
    return urllib.parse.quote(value, safe="").replace(".", "%2E")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        """Never forward credentials or repeat a dispatch through a redirect."""
        return None


def _request(payload: dict[str, Any]) -> bytes:
    if set(payload) != {"connection", "endpoint", "method", "suffix", "body", "timeout"}:
        raise ValueError("Invalid fixed worker request")
    endpoint = _Endpoint(payload["endpoint"])
    connection = payload["connection"]
    node = urllib.parse.quote(connection["node"], safe="")
    origin = connection["api_url"].rstrip("/")
    node_base = f"{origin}/api2/json/nodes/{node}"
    if endpoint in (
        _Endpoint.VM_REMOVE_DISKS,
        _Endpoint.SNAPSHOTS,
        _Endpoint.PENDING,
        _Endpoint.VM_PERMISSIONS,
        _Endpoint.STORAGE_PERMISSIONS,
        _Endpoint.STORAGE_IMAGES,
    ) and (type(connection["vmid"]) is not int or connection["vmid"] <= 0):
        raise ValueError("Rollback requires an exact positive integer VM identifier")
    base = f"{node_base}/qemu/{connection['vmid']}"
    method, suffix, body = payload["method"], payload["suffix"], payload["body"]
    if endpoint is _Endpoint.GUEST_AGENT:
        if not (
            method == "POST"
            and suffix == "exec"
            and type(body) is str
            or method == "GET"
            and type(suffix) is str
            and re.fullmatch(r"exec-status\?pid=[1-9][0-9]*", suffix)
            and body is None
        ):
            raise ValueError("Guest execution requires a fixed method, route and body")
        url = f"{base}/agent/{suffix}"
    elif endpoint in (
        _Endpoint.POWER,
        _Endpoint.CURRENT_CONFIG,
        _Endpoint.GUEST_INFO,
        _Endpoint.SNAPSHOTS,
        _Endpoint.PENDING,
    ):
        if suffix is not None or method != "GET" or body is not None:
            raise ValueError("Provider observation requires a fixed body-free GET")
        route = {
            _Endpoint.POWER: "status/current",
            _Endpoint.CURRENT_CONFIG: "config?current=1",
            _Endpoint.GUEST_INFO: "agent/info",
            _Endpoint.SNAPSHOTS: "snapshot",
            _Endpoint.PENDING: "pending",
        }[endpoint]
        url = f"{base}/{route}"
    elif endpoint is _Endpoint.VM_PERMISSIONS:
        if suffix is not None or method != "GET" or body is not None:
            raise ValueError("Effective VM permissions require a fixed GET")
        url = f"{origin}/api2/json/access/permissions?path=%2Fvms%2F{connection['vmid']}"
    elif endpoint in (_Endpoint.STORAGE_PERMISSIONS, _Endpoint.STORAGE_IMAGES):
        if not _valid_storage(suffix) or method != "GET" or body is not None:
            raise ValueError("Storage observation requires an exact storage and fixed GET")
        if endpoint is _Endpoint.STORAGE_PERMISSIONS:
            url = f"{origin}/api2/json/access/permissions?path=%2Fstorage%2F{suffix}"
        else:
            url = f"{node_base}/storage/{suffix}/content?content=images&vmid={connection['vmid']}"
    elif endpoint in (_Endpoint.NEXT_ID, _Endpoint.VMID_FREE):
        if suffix is not None or method != "GET" or body is not None:
            raise ValueError("Candidate VM identifier requires a fixed body-free GET")
        url = f"{origin}/api2/json/cluster/nextid"
        if endpoint is _Endpoint.VMID_FREE:
            if type(connection["vmid"]) is not int or connection["vmid"] <= 0:
                raise ValueError("VM identifier observation requires a positive integer identifier")
            url += f"?vmid={connection['vmid']}"
    elif endpoint is _Endpoint.VM_CLONE:
        if suffix is not None or method != "POST" or type(body) is not str:
            raise ValueError("Clone requires a fixed POST and exact public body")
        clone = json.loads(body)
        if not _valid_clone_body(clone) or clone["newid"] == connection["vmid"]:
            raise ValueError("Invalid clone body")
        url = f"{base}/clone"
    elif endpoint in (_Endpoint.VM_START, _Endpoint.VM_STOP):
        if suffix is not None or method != "POST" or body is not None:
            raise ValueError("VM power control requires a fixed body-free POST")
        url = f"{base}/status/{'start' if endpoint is _Endpoint.VM_START else 'stop'}"
    elif endpoint is _Endpoint.VM_DESTROY:
        if suffix is not None or method != "DELETE" or body is not None:
            raise ValueError("VM destroy requires a fixed body-free DELETE")
        url = base
    elif endpoint is _Endpoint.VM_REMOVE_DISKS:
        if (
            suffix is not None
            or method != "POST"
            or type(body) is not str
            or not _valid_remove_disks_body(json.loads(body))
        ):
            raise ValueError("Disk removal requires an exact digest-guarded POST")
        url = f"{base}/config"
    elif endpoint in (_Endpoint.VM_CONFIGURE, _Endpoint.VM_RESIZE):
        if suffix is not None or method != "PUT" or type(body) is not str:
            raise ValueError("Configuration requires a fixed PUT and exact public body")
        validator = _valid_configuration_body if endpoint is _Endpoint.VM_CONFIGURE else _valid_resize_body
        if not validator(json.loads(body)):
            raise ValueError("Invalid public configuration body")
        url = f"{base}/{'config' if endpoint is _Endpoint.VM_CONFIGURE else 'resize'}"
    else:
        if method != "GET" or body is not None:
            raise ValueError("Task status requires a fixed body-free GET")
        url = f"{node_base}/tasks/{_task_component(suffix)}/status"
    if endpoint in (
        _Endpoint.VM_START,
        _Endpoint.VM_STOP,
        _Endpoint.VM_DESTROY,
        _Endpoint.VMID_FREE,
        _Endpoint.TASK_STATUS,
        _Endpoint.GUEST_INFO,
        _Endpoint.NEXT_ID,
        _Endpoint.VM_CLONE,
        _Endpoint.VM_CONFIGURE,
        _Endpoint.VM_RESIZE,
        _Endpoint.VM_REMOVE_DISKS,
        _Endpoint.SNAPSHOTS,
        _Endpoint.PENDING,
        _Endpoint.VM_PERMISSIONS,
        _Endpoint.STORAGE_PERMISSIONS,
        _Endpoint.STORAGE_IMAGES,
    ) and not _valid_control_timeout(payload["timeout"]):
        raise ValueError("VM control requires a positive finite timeout")
    request = urllib.request.Request(
        url, data=body.encode("ascii") if body is not None else None, method=payload["method"]
    )
    request.add_header("Authorization", f"PVEAPIToken={connection['token_id']}={connection['token_secret']}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    context = ssl.create_default_context(cafile=connection["ca_bundle"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=payload["timeout"]) as response:
            encoded = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("Provider response exceeded the observation bound")
    return bytes(encoded)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > _MAX_WORKER_INPUT_BYTES:
            return 1
        encoded = _request(json.loads(raw))
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
