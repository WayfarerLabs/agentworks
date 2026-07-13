"""The vm-platform capability: code that runs VMs on one backend kind.

``VM_PLATFORM_REGISTRY`` holds the code behind the read-only
``vm-platform`` capability resources: one :class:`VMPlatform` subclass
per backend kind (lima, wsl2, azure-vm, proxmox; plugin-registered
platforms later). The declarable ``vm-site`` kind exposes a configured
platform, and site resolution (``agentworks.vms.sites``) is the only
consumer that constructs platform instances; manager code never
imports this registry or the concrete classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.azure_vm import AzureVMPlatform
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
    "AzureVMPlatform",
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
    AzureVMPlatform.name: AzureVMPlatform,
    ProxmoxPlatform.name: ProxmoxPlatform,
}
"""Every platform this BUILD ships (INSTALLED, in doctor's vocabulary).
Which of them are usable on this host is the platform's own call:
:meth:`VMPlatform.unsupported_reason` gates the capability row, and
every site (bundled and declared alike) registers unconditionally and
self-disables when its platform is missing/unsupported or the bound
instance reports a missing requirement
(:meth:`Capability.disabled_reason`). The knowledge lives on the
platform class (no config knob, no host sniffing anywhere else),
which is exactly the shape a plugin's platform brings along. Future
plugins register here (and publish their own capability resources with
plugin origins).
"""


@dataclass(frozen=True)
class VMPlatformEntry:
    """A name-keyed marker for one VM platform capability (``"lima"``,
    ``"azure-vm"``, ...).

    The actual platform class (``LimaPlatform``, ``AzureVMPlatform``)
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
    it is installed but disabled on this host and listed only by
    doctor. Sites referencing it still register; they self-disable
    with the platform's reason in the chain (and emit no capability
    edge, so the missing row never trips finalize).
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
