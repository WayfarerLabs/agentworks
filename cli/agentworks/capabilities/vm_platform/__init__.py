"""The vm-platform capability: code that runs VMs on one backend kind.

``VM_PLATFORM_REGISTRY`` holds the code behind the read-only
``vm-platform`` capability resources: one :class:`VMPlatform` subclass
per backend kind (``lima``, ``wsl2`` as core built-ins). The declarable
``vm-site`` kind exposes a configured platform, and site resolution
(``agentworks.vms.sites``) is the only consumer that constructs platform
instances; manager code never imports this registry or the concrete
classes.

The ``proxmox`` platform (``agentworks.plugins.proxmox``) and the
``azure-vm`` platform (``agentworks.plugins.azure``) now ship in opt-in
system plugins; each plugin's adapter re-seats its class into
``VM_PLATFORM_REGISTRY`` at import, so site resolution still finds it by
registry name, while its ROW publishes with a ``system-plugin`` origin
(see ``publish_to``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import (
    ProvisionRequest,
    ProvisionResult,
    VMPlatform,
)
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

if TYPE_CHECKING:
    from agentworks.resources.origin import Origin
    from agentworks.resources.registry import Registry

__all__ = [
    "VM_PLATFORM_REGISTRY",
    "LimaPlatform",
    "ProvisionRequest",
    "ProvisionResult",
    "VMPlatform",
    "VMPlatformEntry",
    "WSL2Platform",
    "publish_to",
]

VM_PLATFORM_REGISTRY: dict[str, type[VMPlatform]] = {
    LimaPlatform.name: LimaPlatform,
    WSL2Platform.name: WSL2Platform,
}
"""Every platform this BUILD ships (INSTALLED, in doctor's vocabulary).
Which of them are usable on this host is the platform's own call, but
that call is READINESS, not presence: every installed platform publishes
a ``vm-platform`` row (R13), and :meth:`VMPlatform.unsupported_reason`
feeds the row's readiness verdict rather than gating publication, so a
host-unsupported platform (wsl2 off Windows) is a present-but-not-ready
node, not an absent one. Every site (bundled and declared alike)
registers unconditionally and is not-ready when its platform is
host-unsupported or the bound config reports a missing requirement
(:meth:`Capability.not_ready`). The knowledge lives on the platform
class (no config knob, no host sniffing anywhere else), which is exactly
the shape a plugin's platform brings along. Future plugins register here
(and publish their own capability resources with plugin origins).
"""


@dataclass(frozen=True)
class VMPlatformEntry:
    """A name-keyed marker for one VM platform capability (``"lima"``,
    ``"wsl2"``, ...).

    The actual platform class (``LimaPlatform`` in core, ``AzureVMPlatform``
    in the ``azure`` plugin) lives beside its module; a core platform lives in
    ``agentworks.capabilities.vm_platform``, a plugin platform in its plugin
    package. This row is what ``vm-site`` ``spec.platform`` references resolve
    against in the framework. Lives with the capability (not ``vms/kinds.py``)
    so publishing never imports the consuming domain.

    Inbound references live on the dependency graph
    (``Registry.graph.dependents_of``), not on this row. The row publishes
    for every installed platform regardless of host support (R13); the
    node's readiness (from ``unsupported_reason``) carries the host-support
    verdict.
    """

    name: str
    description: str = ""
    origin: Origin | None = None


def publish_to(registry: Registry) -> None:
    """Publish one ``vm-platform`` capability resource per registered
    platform, UNCONDITIONALLY (R13), ``built-in`` origin. Read-only
    rows: ``vm-site`` ``spec.platform`` references validate against
    them uniformly, and the platforms list/describe like every other
    resource.

    Host support is READINESS, not absence: an installed-but-unsupported
    platform (wsl2 off Windows) still publishes a row, and its
    ``unsupported_reason`` becomes the readiness fold's input for that
    node (a present, not-ready ``vm-platform``), so a site referencing it
    is not-ready rather than dangling on an absent row. This replaces the
    old edge-suppression's job of hiding the missing capability row.

    A platform seated by a system plugin (``proxmox`` via the ``proxmox``
    plugin, ``azure-vm`` via the ``azure`` plugin) keeps its impl in
    ``VM_PLATFORM_REGISTRY`` so site resolution can construct it, but its row is
    published by ``plugins.publish_plugins`` with a ``system-plugin`` origin.
    Skip those names here so the plugin is the sole publisher of the row;
    publishing it here too would collide (built-in vs system-plugin) at
    ``Registry.add``.
    """
    from agentworks.plugins.registration import plugin_seated_names
    from agentworks.resources import Origin

    seated_by_plugin = plugin_seated_names("vm-platform")
    origin = Origin.built_in(source="agentworks.capabilities.vm_platform")
    for name, platform_cls in VM_PLATFORM_REGISTRY.items():
        if name in seated_by_plugin:
            continue
        registry.add(
            "vm-platform",
            name,
            VMPlatformEntry(name=name, description=platform_cls.description),
            origin,
        )
