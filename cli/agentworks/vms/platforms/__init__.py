"""The vm-platform capability registry.

``VM_PLATFORM_REGISTRY`` holds the code behind the read-only
``vm-platform`` capability resources: one :class:`VMPlatform` subclass
per backend kind (lima, wsl2, azure, proxmox; plugin-registered
platforms later). The declarable ``vm-site`` kind exposes a configured
platform, and site resolution (``agentworks.vms.sites``) is the only
consumer that constructs platform instances -- manager code never
imports this registry or the concrete classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.vms.platforms.azure import AzurePlatform
from agentworks.vms.platforms.lima import LimaPlatform
from agentworks.vms.platforms.proxmox import ProxmoxPlatform
from agentworks.vms.platforms.wsl2 import WSL2Platform

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry
    from agentworks.vms.base import VMPlatform

VM_PLATFORM_REGISTRY: dict[str, type[VMPlatform]] = {
    LimaPlatform.name: LimaPlatform,
    WSL2Platform.name: WSL2Platform,
    AzurePlatform.name: AzurePlatform,
    ProxmoxPlatform.name: ProxmoxPlatform,
}
"""The capability registry. Future plugins register here (and publish
their own capability resources with plugin origins)."""


def publish_to(registry: Registry) -> None:
    """Publish one ``vm-platform`` capability resource per registered
    platform, ``built-in`` origin. Read-only rows: ``vm-site``
    ``spec.platform`` references validate against them uniformly, and
    the platforms list/describe like every other resource.
    """
    from agentworks.resources import Origin
    from agentworks.vms.kinds import VMPlatformEntry

    origin = Origin.built_in(source="agentworks.vms")
    for name, platform_cls in VM_PLATFORM_REGISTRY.items():
        registry.add(
            "vm-platform",
            name,
            VMPlatformEntry(name=name, description=platform_cls.description),
            origin,
        )
