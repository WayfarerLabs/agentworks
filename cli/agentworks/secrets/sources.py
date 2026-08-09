"""Declarable secret sources and source-to-backend selection.

A ``secret-source`` is one named, configured instance of exactly one
``secret-backend`` implementation. This module owns source declarations and
selection; the capability package remains independent of the consuming
secrets domain.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.declared_resource import DeclaredResource
from agentworks.errors import StateError
from agentworks.naming import MAX_FREEFORM_NAME_LENGTH
from agentworks.origin import Origin
from agentworks.schema import CapabilityBlock, RefOwner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.resources.graph import DependencyState, FinalizeContext, Readiness
    from agentworks.resources.reference import ConfigReference, ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.source_location import SourceLocation


SECRET_SOURCE_KIND_NAME = "secret-source"
_BUILTIN_SOURCE = "agentworks.secrets.sources"
_DEFAULT_SOURCE_NAMES = ("env-var", "prompt")


class SecretSourceDecl(DeclaredResource):
    """A named configured instance of one secret backend."""

    NAME_MAX_LENGTH: ClassVar[int | None] = MAX_FREEFORM_NAME_LENGTH

    backend: CapabilityBlock
    """The backend implementation and its per-source configuration."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """Emit the selected backend first, then config-implied references."""
        from agentworks.capabilities.config import capability_config_references
        from agentworks.resources.reference import ResourceReference, sourced_references

        source = (SECRET_SOURCE_KIND_NAME, self.name)
        refs = [
            ResourceReference(
                name=self.backend.name,
                kind="secret-backend",
                usage="the source's backend implementation",
                source=source,
            )
        ]
        refs.extend(
            sourced_references(
                capability_config_references(
                    kind="secret-backend",
                    config=self.backend.tagged,
                    owner=RefOwner(kind=SECRET_SOURCE_KIND_NAME, name=self.name),
                ),
                source,
            )
        )
        return refs

    def validate_config(self, context: FinalizeContext) -> None:
        """Validate the tagged source config against its selected backend."""
        from agentworks.capabilities.config import validate_capability_config

        validate_capability_config(
            kind="secret-backend",
            config=self.backend.tagged,
            owner=RefOwner(kind=SECRET_SOURCE_KIND_NAME, name=self.name),
            location=self.error_location,
        )

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness:
        """Fold backend enablement, readiness, and source config readiness."""
        from agentworks.resources.graph import Enablement, Readiness

        dependency = deps.get(("secret-backend", self.backend.name))
        if dependency is None:
            return Readiness.ready()
        if dependency.enablement is Enablement.disabled:
            tail = dependency.disabled_reason or "enable its unit"
            return Readiness.blocked(f"depends on secret-backend '{self.backend.name}', which is disabled; {tail}")
        if dependency.readiness is not None and not dependency.readiness.is_ready:
            return dependency.readiness
        impl = dependency.impl
        if impl is None:
            return Readiness.ready()
        if not isinstance(impl, type) or not issubclass(impl, SecretBackend):
            raise StateError(
                f"secret-backend/{self.backend.name} carries {type(impl).__name__}, not a SecretBackend class"
            )
        return impl.not_ready(self.backend.config)


class SourceBackendLookup(Protocol):
    """The two narrow reads needed to select a source's backend class."""

    def source_row(self, name: str) -> object | None: ...

    def backend_class(self, name: str) -> type | None: ...


def source_backend_class(
    lookup: SourceBackendLookup,
    source_name: str,
) -> tuple[SecretSourceDecl, type[SecretBackend]] | None:
    """Select ``source_name`` and its backend class, source-first.

    A missing source returns without probing the backend namespace. There is
    no same-name backend fallback.
    """
    source = lookup.source_row(source_name)
    if source is None:
        return None
    if not isinstance(source, SecretSourceDecl):
        raise StateError(f"secret-source/{source_name} carries {type(source).__name__}, not SecretSourceDecl")
    backend = lookup.backend_class(source.backend.name)
    if backend is None:
        return None
    if not isinstance(backend, type) or not issubclass(backend, SecretBackend):
        raise StateError(
            f"secret-backend/{source.backend.name} carries {type(backend).__name__}, not a SecretBackend class"
        )
    return source, backend


def validate_source_mapping(
    *,
    lookup: SourceBackendLookup,
    source_name: str,
    mapping: object,
    owner: RefOwner,
    location: SourceLocation | None,
) -> BaseModel | None:
    """Validate a mapping through the backend selected by its source."""
    selected = source_backend_class(lookup, source_name)
    if selected is None:
        return None
    _source, backend = selected
    from agentworks.capabilities.config import validate_capability_mapping

    return validate_capability_mapping(
        kind="secret-backend",
        name=backend.name,
        mapping=mapping,
        owner=owner,
        location=location,
    )


def source_mapping_references(
    *,
    lookup: SourceBackendLookup,
    source_name: str,
    mapping: object,
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """Extract mapping references through the same source selector."""
    selected = source_backend_class(lookup, source_name)
    if selected is None:
        return ()
    _source, backend = selected
    from agentworks.capabilities.config import capability_mapping_references

    return capability_mapping_references(
        kind="secret-backend",
        name=backend.name,
        mapping=mapping,
        owner=owner,
    )


class _RegistrySourceBackendLookup:
    """Retrieval-only adapter over a finalized Registry and graph."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def source_row(self, name: str) -> object | None:
        try:
            return cast("object", self._registry.lookup(SECRET_SOURCE_KIND_NAME, name))
        except KeyError:
            return None

    def backend_class(self, name: str) -> type | None:
        try:
            impl = self._registry.graph.impl_of("secret-backend", name)
        except KeyError:
            return None
        return cast("type | None", impl)


def registry_source_backend_lookup(registry: Registry) -> SourceBackendLookup:
    """Return the source selector adapter for a finalized registry."""
    return _RegistrySourceBackendLookup(registry)


class SourceProvenance(Enum):
    """How a source row relates to the two synthesized defaults."""

    SYNTHESIZED_DEFAULT = "synthesized-default"
    OPERATOR_OVERRIDE = "operator-override-of-synthesized-default"
    DECLARED = "declared"


def source_provenance(source: SecretSourceDecl) -> SourceProvenance:
    """Derive override provenance solely from the surviving registry row."""
    if source.name not in _DEFAULT_SOURCE_NAMES:
        return SourceProvenance.DECLARED
    if source.origin == Origin.built_in(source=_BUILTIN_SOURCE):
        return SourceProvenance.SYNTHESIZED_DEFAULT
    if source.origin is not None and source.origin.variant == "operator-declared":
        return SourceProvenance.OPERATOR_OVERRIDE
    return SourceProvenance.DECLARED


def publish_builtin_secret_sources(registry: Registry) -> None:
    """Publish exactly ``env-var`` then ``prompt`` as normal built-in rows."""
    from agentworks.capabilities.config import registered_implementation

    origin = Origin.built_in(source=_BUILTIN_SOURCE)
    for name in _DEFAULT_SOURCE_NAMES:
        impl = registered_implementation("secret-backend", name)
        if impl is None:
            raise StateError(
                f"cannot publish built-in secret-source/{name}: its secret-backend class is not registered"
            )
        registry.add(
            SECRET_SOURCE_KIND_NAME,
            name,
            SecretSourceDecl(name=name, backend=CapabilityBlock.of(name)),
            origin,
        )
