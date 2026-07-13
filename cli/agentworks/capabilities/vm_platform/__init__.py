"""The vm-platform capability: code that runs VMs on one backend kind.

``VM_PLATFORM_REGISTRY`` holds the code behind the read-only
``vm-platform`` capability resources: one :class:`VMPlatform` subclass
per backend kind (lima, wsl2, azure, proxmox; plugin-registered
platforms later). The declarable ``vm-site`` kind exposes a configured
platform, and site resolution (``agentworks.vms.sites``) is the only
consumer that constructs platform instances -- manager code never
imports this registry or the concrete classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.azure import AzurePlatform
from agentworks.capabilities.vm_platform.base import (
    ProvisionRequest,
    ProvisionResult,
    VMPlatform,
)
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

if TYPE_CHECKING:
    from agentworks.resources.origin import Origin
    from agentworks.resources.registry import Registry

__all__ = [
    "VM_PLATFORM_REGISTRY",
    "AzurePlatform",
    "LimaPlatform",
    "ProvisionRequest",
    "ProvisionResult",
    "ProxmoxPlatform",
    "VMPlatform",
    "VMPlatformEntry",
    "WSL2Platform",
    "publish_to",
]

VM_PLATFORM_REGISTRY: dict[str, type[VMPlatform]] = {
    LimaPlatform.name: LimaPlatform,
    WSL2Platform.name: WSL2Platform,
    AzurePlatform.name: AzurePlatform,
    ProxmoxPlatform.name: ProxmoxPlatform,
}
"""Every platform this BUILD ships (INSTALLED, in doctor's vocabulary).
Which of them are usable on this host is the platform's own call:
:meth:`VMPlatform.unsupported_reason` gates registration (the capability
row and everything downstream), and
:meth:`VMPlatform.bundled_site_unsupported_reason` additionally gates
the zero-config bundled site. The knowledge lives on the platform class
-- no config knob, no host sniffing anywhere else -- which is exactly
the shape a plugin's platform brings along. Future plugins register
here (and publish their own capability resources with plugin origins).
"""


def unsupported_platforms() -> dict[str, str]:
    """``{platform_name: reason}`` for every installed platform that
    cannot run on this host at all (``unsupported_reason``). These
    publish no capability row; doctor lists them as installed-but-
    disabled with the reason."""
    return {
        name: reason
        for name, cls in VM_PLATFORM_REGISTRY.items()
        if (reason := cls.unsupported_reason()) is not None
    }


def bundled_site_platform(site_name: str) -> str | None:
    """The platform whose bundled site is named ``site_name``, or
    ``None`` if no platform bundles a site by that name. Drives the
    targeted hint on a bundled-site lookup miss."""
    for name, cls in VM_PLATFORM_REGISTRY.items():
        if cls.bundled_site == site_name:
            return name
    return None


@dataclass(frozen=True)
class VMPlatformEntry:
    """A name-keyed marker for one VM platform capability (``"lima"``,
    ``"azure"``, ...).

    The actual platform class (``LimaPlatform``, ``AzurePlatform``)
    lives beside this in ``agentworks.capabilities.vm_platform``; this
    row is what ``vm-site`` ``spec.platform`` references resolve against
    in the framework. Lives with the capability (not ``vms/kinds.py``)
    so publishing never imports the consuming domain.
    """

    name: str
    description: str = ""
    origin: Origin | None = None
    references: tuple[Any, ...] = ()


def publish_to(registry: Registry) -> None:
    """Publish one ``vm-platform`` capability resource per registered
    platform SUPPORTED on this host, ``built-in`` origin. Read-only
    rows: ``vm-site`` ``spec.platform`` references validate against
    them uniformly, and the platforms list/describe like every other
    resource.

    An unsupported platform (``unsupported_reason``) publishes nothing:
    it is installed but disabled on this host, invisible to the
    resource graph and listed only by doctor. A declared site
    referencing one gets the friendly requirements error from
    ``bootstrap.build_registry``'s pre-finalize guard, before the
    framework's generic reference miss could fire.
    """
    from agentworks.resources import Origin

    origin = Origin.built_in(source="agentworks.capabilities.vm_platform")
    for name, platform_cls in VM_PLATFORM_REGISTRY.items():
        if platform_cls.unsupported_reason() is not None:
            continue
        registry.add(
            "vm-platform",
            name,
            VMPlatformEntry(name=name, description=platform_cls.description),
            origin,
        )
