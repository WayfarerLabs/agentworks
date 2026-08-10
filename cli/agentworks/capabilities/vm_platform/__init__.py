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
(the built-in publisher skips it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import (
    BootstrapProgress,
    ProvisionRequest,
    ProvisionResult,
    VMPlatform,
)
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

if TYPE_CHECKING:
    from agentworks.origin import Origin

__all__ = [
    "BootstrapProgress",
    "VM_PLATFORM_REGISTRY",
    "LimaPlatform",
    "ProvisionRequest",
    "ProvisionResult",
    "VMPlatform",
    "VMPlatformEntry",
    "WSL2Platform",
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
