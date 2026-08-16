"""Resource-kind strategy and descriptor for ``secret-backend``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    ConfigContract,
    HostSurface,
    MappingHost,
    ModelInputDomain,
)
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.schema import AgwModel, AgwRootModel, ResourceRef
from agentworks.schema.reference import RefRelationship
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.origin import Origin
    from agentworks.resources.graph import Readiness
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class SecretBackendEntry:
    """The read-only capability row for one registered backend class."""

    name: str
    description: str = ""
    origin: Origin | None = None


@dataclass(frozen=True)
class _SecretBackendKind:
    kind: str = "secret-backend"
    description: str = "Capability for resolving secret values"
    prose: TopicProse = TopicProse(
        title="Secret backends",
        overview="""
        A secret-backend is provider code for resolving secret values. The active
        source chain determines precedence, and the first source with a value wins.
        Each implementation declares its source config and per-secret mapping models.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretBackendEntry:
        raise NoUnreferencedDefaultError(
            "the secret-backend kind has miss_policy='error'; synthesize should never be dispatched"
        )


KIND_REGISTRY["secret-backend"] = _SecretBackendKind()


def _backend_registry() -> dict[str, Any]:
    from agentworks.capabilities.secret_backend.base import SECRET_BACKEND_REGISTRY

    return SECRET_BACKEND_REGISTRY


def _backend_entry(name: str, impl: Any, origin: Origin | None) -> SecretBackendEntry:
    return SecretBackendEntry(name=name, description=impl.description, origin=origin)


def _backend_readiness(name: str, impl: Any) -> Readiness:
    return cast("Readiness", impl.backend_readiness())


SECRET_BACKEND_DESCRIPTOR = CapabilityKindDescriptor(
    kind="secret-backend",
    contract_version=2,
    implementation_contract=SecretBackend,
    registry=_backend_registry,
    required_operations=frozenset(
        {
            "backend_readiness",
            "would_attempt",
            "describe_lookup",
            "create_client",
        },
    ),
    required_attributes=frozenset({"interactive", "config_model", "mapping_model"}),
    entry_factory=_backend_entry,
    readiness=_backend_readiness,
    publisher_source="agentworks.capabilities.secret_backend",
    manifest_section=HostSurface(
        host_kind="secret-source",
        naming_field="backend",
    ),
    config_schema=ConfigContract(
        base=AgwModel,
        discriminator="name",
        forbidden_reference_kinds=frozenset({"secret"}),
    ),
    mapping_schema=ConfigContract(
        base=AgwRootModel,
        discriminator=None,
        input_domain=ModelInputDomain.JSON_NATIVE,
    ),
    mapping_host=MappingHost(
        host_kind="secret",
        field_name="backend_mappings",
        key_reference=ResourceRef(
            kind="secret-source",
            usage="a source for resolving this secret",
            relationship=RefRelationship.USES,
        ),
        false_opt_out=True,
    ),
)
