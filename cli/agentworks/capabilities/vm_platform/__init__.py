"""The vm-platform capability: code that runs VMs on one backend kind.

``registry.VM_PLATFORM_REGISTRY`` holds the code behind the read-only
``vm-platform`` capability resources: one :class:`VMPlatform` subclass
per backend kind (``lima``, ``wsl2`` as core built-ins). The declarable
``vm-site`` kind exposes a configured platform, and site resolution
(``agentworks.vms.sites``) is the only consumer that constructs platform
instances; manager code never imports this registry or the concrete
classes.

The ``proxmox``, ``azure-vm``, ``aws-ec2``, and ``gcp-gce`` platforms ship in opt-in system
plugins; each plugin's adapter re-seats its class into
``registry.VM_PLATFORM_REGISTRY`` at import, so site resolution still finds it by
registry name, while its ROW publishes with a ``system-plugin`` origin
(the built-in publisher skips it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import (
    MAX_PROVIDER_LOCATOR_BYTES,
    BootstrapProgress,
    ProviderLocator,
    ProviderLocatorObservation,
    ProviderLocatorUnavailable,
    ProvisionRequest,
    ProvisionResult,
    RetainedProvisioningError,
    VMPlatform,
    provider_locator_remaining,
)

if TYPE_CHECKING:
    from agentworks.origin import Origin

__all__ = [
    "BootstrapProgress",
    "MAX_PROVIDER_LOCATOR_BYTES",
    "ProvisionRequest",
    "ProvisionResult",
    "ProviderLocator",
    "ProviderLocatorObservation",
    "ProviderLocatorUnavailable",
    "RetainedProvisioningError",
    "VMPlatform",
    "VMPlatformEntry",
    "provider_locator_remaining",
]


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
