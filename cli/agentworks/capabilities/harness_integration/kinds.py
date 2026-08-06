"""``_HarnessIntegrationKind``: the framework strategy for the ``"harness-integration"`` kind,
plus the ``HarnessIntegrationEntry`` capability row and the kind's
``CapabilityKindDescriptor``.

Lives in the ``capabilities.harness_integration`` package next to the harness integration
implementations; ``agentworks.resources.kinds.__init__`` imports this
module so the kind self-registers into ``KIND_REGISTRY`` at load.

``_HarnessIntegrationKind`` gives the framework a name-keyed marker so a
``session-template`` ``spec.harness_integration`` value typo surfaces uniformly. The
harness integration implementations live in ``agentworks.capabilities.harness_integration``; the
companion publisher there adds one ``HarnessIntegrationEntry`` row per known
harness integration, built-in with source ``"agentworks.capabilities.harness_integration"``. It
mirrors ``_GitCredentialProviderKind`` exactly (``category="capability"``,
``miss_policy="error"``, ``builtin_override="reserved"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    ConfigContract,
    HostSurface,
    RegistryPolicy,
)
from agentworks.capabilities.harness_integration.base import HarnessIntegration
from agentworks.resources.graph import Readiness
from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.schema import AgwModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.origin import Origin
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class HarnessIntegrationEntry:
    """A name-keyed marker for one harness integration capability (``"shell"``,
    ``"claude-code"``).

    The actual harness integration class (``ShellIntegration``, ``ClaudeCodeIntegration``)
    lives beside this in ``agentworks.capabilities.harness_integration``; this row is
    what a ``session-template`` ``spec.harness_integration`` reference resolves
    against in the framework.

    Inbound references live on the dependency graph
    (``Registry.graph.dependents_of``), not on this row.
    """

    name: str
    origin: Origin | None = None


@dataclass(frozen=True)
class _HarnessIntegrationKind:
    """Implementation of ``ResourceKind`` for ``"harness-integration"``."""

    kind: str = "harness-integration"
    description: str = "Capability for running a session workload (shell, claude-code)"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        # Unreachable under the error miss policy; honors the
        # empty-references contract via the typed framework error so a
        # future change that gives the kind a reserved default has an
        # obvious landing pad.
        raise NoUnreferencedDefaultError(
            "the harness integration kind has miss_policy='error'; synthesize should "
            "never be invoked (the framework raises ConfigError first)"
        )


KIND_REGISTRY["harness-integration"] = _HarnessIntegrationKind()


def _registry() -> dict[str, Any]:
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY

    return HARNESS_INTEGRATION_REGISTRY


def _entry(name: str, impl: Any, origin: Origin | None) -> HarnessIntegrationEntry:
    # The impl's ``description`` is deliberately not carried: this row is
    # name-and-origin only, and giving it one would change row content.
    return HarnessIntegrationEntry(name=name, origin=origin)


def _readiness(name: str, impl: Any) -> Readiness:
    """Always ready: a harness integration has no host-support concept (it
    runs wherever the session's transport reaches)."""
    return Readiness.ready()


HARNESS_INTEGRATION_DESCRIPTOR = CapabilityKindDescriptor(
    kind="harness-integration",
    contract_version=1,
    implementation_contract=HarnessIntegration,
    registry_policy=RegistryPolicy.CLASS_BY_NAME,
    registry=_registry,
    required_operations=frozenset({"start", "resume"}),
    # Empty: HarnessIntegration supplies every non-operation member a
    # subclass needs.
    required_attributes=frozenset(),
    entry_factory=_entry,
    kind_strategy=KIND_REGISTRY["harness-integration"],
    readiness=_readiness,
    publisher_source="agentworks.capabilities.harness_integration",
    config_schema=ConfigContract(base=AgwModel, discriminator="name"),
    manifest_section=HostSurface(
        host_kind="session-template",
        naming_field="harness_integration",
        config_field="harness_integration_config",
    ),
)
"""The harness-integration record in the capability-kind descriptor table
(``agentworks.capabilities.descriptor``)."""
