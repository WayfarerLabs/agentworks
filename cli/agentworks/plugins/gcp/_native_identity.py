"""Frozen selection from persisted GCE native-access identity fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.errors import StateError, ValidationError

if TYPE_CHECKING:
    from agentworks.db import VMRow

_COMPONENT = re.compile(r"[a-z0-9-]{1,63}")
_RFC1035 = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PREFIXES = ("https://compute.googleapis.com/compute/v1/", "https://www.googleapis.com/compute/v1/")


def _component(value: object) -> str:
    if type(value) is not str or _COMPONENT.fullmatch(value) is None:
        raise ValueError("invalid resource component")
    return value


def _resource_id(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1:
        raise ValueError("invalid resource ID")
    return value


def canonical_path(value: object, *, project_id: str, kind: Literal["network", "subnet"]) -> str:
    """Validate a provider/persisted resource path for the exact selected project."""
    if type(value) is not str or len(value) > 512:
        raise ValidationError("GCE native resource path is invalid")
    path = value
    for prefix in _PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    parts = path.split("/")
    try:
        _component(project_id)
        if kind == "network":
            if len(parts) != 5 or parts[:4] != ["projects", project_id, "global", "networks"]:
                raise ValueError("foreign network")
            _component(parts[4])
        elif kind == "subnet":
            if len(parts) != 6 or parts[:3] != ["projects", project_id, "regions"] or parts[4] != "subnetworks":
                raise ValueError("foreign subnet")
            _component(parts[3])
            _component(parts[5])
        else:
            raise ValueError("unsupported resource kind")
    except ValueError:
        raise ValidationError("GCE native resource path is invalid") from None
    return path


@dataclass(frozen=True)
class NativeIdentity:
    project_id: str
    zone: str
    instance_name: str
    instance_id: str
    network_url: str
    subnet_url: str | None
    network_tag: str
    deny_rule: str
    deny_rule_id: str
    access_config_name: str

    @classmethod
    def from_row(cls, vm: VMRow) -> NativeIdentity:
        """Validate cross-execution selection without changing persisted bytes."""
        metadata = vm.platform_metadata
        try:
            project = _component(metadata.get("project_id"))
            tag = metadata.get("network_tag")
            deny = metadata.get("deny_rule")
            access = metadata.get("access_config_name")
            if (
                type(tag) is not str
                or _RFC1035.fullmatch(tag) is None
                or type(deny) is not str
                or _RFC1035.fullmatch(deny) is None
                or type(access) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}", access) is None
            ):
                raise ValueError("invalid native access selection")
            subnet = metadata.get("subnet_url")
            return cls(
                project_id=project,
                zone=_component(metadata.get("zone")),
                instance_name=_component(metadata.get("instance_name")),
                instance_id=_resource_id(metadata.get("instance_id")),
                network_url=canonical_path(metadata.get("network_url"), project_id=project, kind="network"),
                subnet_url=None if subnet is None else canonical_path(subnet, project_id=project, kind="subnet"),
                network_tag=tag,
                deny_rule=deny,
                deny_rule_id=_resource_id(metadata.get("deny_rule_id")),
                access_config_name=access,
            )
        except (ValueError, ValidationError):
            raise StateError(
                f"VM '{vm.name}' has invalid GCE native identity metadata", entity_kind="vm", entity_name=vm.name
            ) from None

    @property
    def locator(self) -> ProviderLocator:
        return ProviderLocator(f"gcp-gce:{self.project_id}:{self.zone}:{self.instance_id}")
