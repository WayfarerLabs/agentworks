"""``_VMPlatformKind``: the framework strategy for the ``"vm-platform"``
kind, plus the kind's ``CapabilityKindDescriptor``.

Lives in the ``capabilities.vm_platform`` package next to the platform
implementations, mirroring ``capabilities.harness_integration.kinds`` and
``capabilities.git_credential.kinds``, so all four capability-kind
strategies sit beside their capability code;
``agentworks.resources.kinds.__init__`` imports this module so the kind
self-registers into ``KIND_REGISTRY`` at load.

``_VMPlatformKind`` gives the framework a name-keyed marker so a
``vm-site`` ``spec.platform`` value typo surfaces uniformly. The platform
implementations live in ``agentworks.capabilities.vm_platform``; the
companion publisher there adds one ``VMPlatformEntry`` row per installed
platform, built-in with source
``"agentworks.capabilities.vm_platform"``. The row (``VMPlatformEntry``)
stays in the package ``__init__`` beside the registry it mirrors, which
is where the ``vm-site`` era put it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    HostSurface,
    RegistryPolicy,
)
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.resources.graph import Readiness
from agentworks.resources.kind import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.capabilities.vm_platform import VMPlatformEntry
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class _VMPlatformKind:
    """Implementation of ``ResourceKind`` for ``"vm-platform"``."""

    kind: str = "vm-platform"
    description: str = "Capability for running VMs on one backend kind (lima, wsl2, azure-vm, aws-ec2, proxmox)"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    # Not load-bearing: manifests of a capability kind are rejected
    # wholesale by category before the override policy is consulted.
    # Set to the conservative value for uniformity with vm-site.
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        # Unreachable under the error miss policy; honors the
        # empty-references contract via the typed framework error.
        from agentworks.resources.kind import NoUnreferencedDefaultError

        raise NoUnreferencedDefaultError(
            "the vm-platform kind has miss_policy='error'; synthesize "
            "should never be invoked (the framework raises ConfigError "
            "first)"
        )

    # No per-kind readiness hook: readiness projection is unified on
    # ``inspect.not_ready_reason_for`` reading ``graph.readiness_of`` directly
    # (Phase 4 retired the per-kind shim, including the Phase-3 vm-platform
    # projection pulled forward to render the now-published not-ready row).


KIND_REGISTRY["vm-platform"] = _VMPlatformKind()


def _registry() -> dict[str, Any]:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    return VM_PLATFORM_REGISTRY


def _entry(name: str, impl: Any, origin: Origin | None) -> VMPlatformEntry:
    from agentworks.capabilities.vm_platform import VMPlatformEntry

    return VMPlatformEntry(name=name, description=impl.description, origin=origin)


def _readiness(name: str, impl: Any) -> Readiness:
    """The platform's config-independent host-support verdict.

    "unsupported here" is the readiness phrasing; "disabled" is reserved for
    the opt-in axis and never used for host support. The platform row's own
    projection renders this sentence directly, and the vm-site depending on
    the platform propagates the same verdict.
    """
    reason = impl.unsupported_reason()
    if reason is None:
        return Readiness.ready()
    return Readiness.blocked(f"platform '{name}' is unsupported here: {reason}")


VM_PLATFORM_DESCRIPTOR = CapabilityKindDescriptor(
    kind="vm-platform",
    contract_version=1,
    implementation_contract=VMPlatform,
    registry_policy=RegistryPolicy.CLASS_BY_NAME,
    registry=_registry,
    required_operations=frozenset(
        {"create", "start", "stop", "delete", "status", "display_backend_name"},
    ),
    # Empty: VMPlatform supplies every non-operation member a subclass needs.
    required_attributes=frozenset(),
    config_slots={},  # step 2.3 registers the platform config model here
    entry_factory=_entry,
    kind_strategy=KIND_REGISTRY["vm-platform"],
    readiness=_readiness,
    publisher_source="agentworks.capabilities.vm_platform",
    manifest_section=HostSurface(
        host_kind="vm-site",
        naming_field="platform",
        config_field="platform_config",
        legacy_string_shape="accept-warn",
    ),
)
"""The vm-platform record in the capability-kind descriptor table
(``agentworks.capabilities.descriptor``)."""
