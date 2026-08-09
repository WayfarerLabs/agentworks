"""The capability-kind descriptor table: one frozen record per kind.

This table IS the enumeration of the four capability kinds
(``vm-platform``, ``harness-integration``, ``git-credential-provider``,
``secret-backend``). Seven sites used to enumerate them independently: the
adapter table, the graph's kind set and readiness dispatch, the per-kind
registry loaders, bootstrap publication, the plugin snapshot/restore tuple,
and manifest decode's capability-field map. Each derives from here now, so a
kind is described once and a fifth would be added in one place rather than
found in seven.

Two structural rules make the table safe to consume from anywhere:

- **Per-package contribution.** Each capability package builds its own record
  beside its kind strategy (``capabilities/vm_platform/kinds.py``,
  ``capabilities/harness_integration/kinds.py``,
  ``capabilities/git_credential/kinds.py``,
  ``capabilities/secret_backend/kinds.py``), the way
  each package used to carry its own publisher. Nothing here knows a
  kind's internals.
- **Lazy collection, for cycle safety.** :func:`capability_descriptors`
  imports the four contributing modules INSIDE the function, and consumers
  call it inside their own functions rather than binding the table at module
  import. Early modules (``resources/graph.py``, ``plugins/adapters.py``,
  ``manifests/decode.py``) load before the capability packages, so this
  inherits the cycle discipline the graph builder's per-kind registry
  loaders already used rather than inventing a new one (those loaders now
  derive from this table). The ``registry`` field is a callable for
  the same reason. Cycle safety is the whole benefit: it buys no import
  DEFERRAL, because ``resources/kinds/__init__`` already imports all four
  contributing modules, so anything reaching the resource machinery has
  loaded them regardless.

``KIND_REGISTRY`` stays the all-kinds runtime map and each capability's
``KIND_REGISTRY[...] = ...`` line stays co-located with its kind, exactly
like every declarable kind. The relationship is pinned, not merged: a
descriptor's ``kind_strategy`` IS the object in ``KIND_REGISTRY``, and the
table's self-test asserts that identity so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cache
from typing import TYPE_CHECKING, Any

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from agentworks.origin import Origin
    from agentworks.resources.graph import Readiness
    from agentworks.resources.kind import ResourceKind
    from agentworks.schema import ResourceRef


class RegistryPolicy(Enum):
    """What a capability kind's code registry stores under each name."""

    CLASS_BY_NAME = "class-by-name"
    """The implementation class itself."""


class ModelInputDomain(Enum):
    """The values a declared model may accept at its raw input boundary."""

    PYTHON = "python"
    """The ordinary Pydantic input vocabulary, constrained by the model."""

    JSON_NATIVE = "json-native"
    """Only values representable by Agentworks' JSON-native manifest carrier."""


@dataclass(frozen=True)
class ConfigContract:
    """What a config model offered for this kind must BE.

    The kind states the contract; IMPLEMENTATIONS declare the models. This
    is the thing registration conformance checks a declared model against,
    so a model that could never be reached from a manifest is refused where
    its author can see it rather than going quietly unaddressable.
    """

    base: type[BaseModel]
    """The base every offered model extends. ``AgwModel`` where the config
    is mapping-shaped, ``AgwRootModel`` where it is not: a secret backend's
    per-secret mapping may be a bare string, which no ``BaseModel`` can
    be."""

    discriminator: str | None
    """The field carrying the capability's own name, for a kind whose
    config is dispatched by a DISCRIMINATED UNION (``"name"``: a vm-site
    writes ``platform: {name: lima, ...}``). ``None`` for a kind dispatched
    by a MAP KEY, whose models carry no tag because the key already is one
    map key rather than a tag inside the value."""

    input_domain: ModelInputDomain = ModelInputDomain.PYTHON
    """The annotation vocabulary the model may expose at its input boundary."""

    forbidden_reference_kinds: frozenset[str] = frozenset()
    """Reference-marker kinds this config layer is not allowed to contain."""


@dataclass(frozen=True)
class HostSurface:
    """How a capability kind is selected inside a declarable kind's spec.

    The manifest shape is one tagged table on the naming field
    (``platform: {name: lima, placement: {...}}``), and it is the only shape:
    the legacy sibling pair (``platform: lima`` plus
    ``platform_config: {...}``) is a hard error on every surface. Decode
    reads the two field names off this record, so there is one refusal
    rather than one per host, and the row carries the table as written.

    The record carried a ``legacy_string_shape`` field while the two folds
    differed, because session-template hardened ahead of its siblings.
    Nothing distinguishes the surfaces now, so the field is gone rather
    than left describing a difference that no longer exists.
    """

    host_kind: str
    """The DECLARABLE kind whose spec selects this capability
    (``"vm-site"``, ``"git-credential"``, ``"session-template"``)."""

    naming_field: str
    """The spec field naming the capability (``"platform"``)."""

    config_field: str | None
    """The RETIRED sibling field that used to hold the capability's config
    blob (``"platform_config"``).

    There is no such field any more: the row carries one tagged block, so
    this exists for exactly one reader, ``decode._reject_legacy_shape``,
    which names the retired field in the error that tells an operator how
    to rewrite the 0.14 shape. ``None`` means the host never had a retired
    sibling field."""


@dataclass(frozen=True)
class MappingHost:
    """Where a capability's map-key-selected model is hosted."""

    host_kind: str
    """The declarable resource kind carrying the mapping field."""

    field_name: str
    """The host row field whose keys select configured implementations."""

    key_reference: ResourceRef
    """What each authored mapping key references."""

    false_opt_out: bool
    """Whether singleton ``False`` is framework-owned opt-out vocabulary."""


@dataclass(frozen=True)
class CapabilityKindDescriptor:
    """Everything the framework needs to know about one capability kind.

    Core-owned and frozen: plugins contribute IMPLEMENTATIONS of existing
    kinds, never new kinds. Domain operations stay domain-owned: nothing
    here touches ``VMPlatform.create``, ``SecretBackend.create_client``, or
    credential fill. The descriptor wires a kind into the framework; it does
    not absorb what makes the kind itself.
    """

    kind: str
    """The capability kind identifier, matching ``KIND_REGISTRY``'s key."""

    contract_version: int
    """The single implementation contract version this build supports. Every
    impl declares its own ``contract_version`` and registration requires an
    exact match, so a contract change is a HARD CUTOVER: bumping this number
    refuses every impl still on the old one until each is migrated. That is
    the intent at 2.0 (one version, nothing to straddle); supporting two
    versions at once would need a supported-range field and a compatibility
    rule, which is a decision to make when a real migration needs it."""

    implementation_contract: type
    """The nominal base class an implementation must derive from."""

    registry_policy: RegistryPolicy
    """Whether the kind's registry stores classes. Every current kind is
    class-by-name."""

    registry: Callable[[], dict[str, Any]]
    """Lazy accessor for the kind's live code registry. A callable, not the
    dict, so this module never imports the capability packages at load. The
    returned object is the LIVE registry: callers that mutate it (seating,
    snapshot restore) are mutating the real thing, by design."""

    required_operations: frozenset[str]
    """The domain operations the framework depends on being callable."""

    required_attributes: frozenset[str]
    """The kind-specific non-operation members consumers read off an impl."""

    entry_factory: Callable[[str, Any, Origin | None], Any]
    """Builds the kind's read-only resource row from ``(name, seated impl,
    origin)``. Per-kind rather than one generic row because today's four
    rows differ (vm-platform and secret-backend carry ``description``,
    harness-integration and git-credential-provider do not); unifying them
    would change row content, which is row-semantics work, not switchboard
    work."""

    kind_strategy: ResourceKind
    """The kind's ``ResourceKind`` strategy: the SAME object registered in
    ``KIND_REGISTRY[kind]``, referenced here rather than duplicated."""

    readiness: Callable[[str, Any], Readiness]
    """The capability NODE's readiness from ``(name, impl)``: a
    config-independent host-support verdict. Distinct from the
    config-dependent ``Capability.not_ready(config)`` a CONSUMING resource
    (vm-site) uses."""

    publisher_source: str
    """The ``Origin.built_in`` source label the kind's built-in rows carry."""

    manifest_section: HostSurface | None
    """How the kind is selected in its host's manifest spec, or ``None``
    when the host declaration has not landed."""

    config_schema: ConfigContract
    """What a config model offered for this kind must be.

    The framework never asks an implementation for "its schema"; it asks
    for the config it offers at a FACET (``Capability.config_for``), and
    every model that comes back has to satisfy this contract. A capability
    whose methods run at several levels with different config is then a
    per-capability declaration rather than a framework change."""

    mapping_schema: ConfigContract | None = None
    """The model contract for a map-key-selected consuming surface."""

    mapping_host: MappingHost | None = None
    """The declarable map field hosting :attr:`mapping_schema`."""

    # One field is deliberately NOT added yet, recorded with the trigger
    # that would create it so it is neither built early nor reinvented:
    #
    #   consumer_gating -> the first NEW consuming surface that
    #                      consolidates gating derivation. Nothing here
    #                      changes gating behavior today, so there is
    #                      nothing to carry.
    #
    # Snapshot/restore needs no field at all: it iterates the table, so
    # participation is membership and a flag could only be wrong.


@cache
def capability_descriptors() -> tuple[CapabilityKindDescriptor, ...]:
    """The four capability-kind records: the switchboard's single
    enumeration.

    Order is the historical registry order (vm-platform,
    harness-integration, git-credential-provider, secret-backend), which the
    plugin snapshot/restore tuple preserves.

    Imports its contributors lazily (inside the function) so this module
    stays importable from the early framework modules; call it inside your
    own functions for the same reason. Cached because the records are frozen
    module-level constants, so collecting them once is enough.
    """
    from agentworks.capabilities.git_credential.kinds import GIT_CREDENTIAL_PROVIDER_DESCRIPTOR
    from agentworks.capabilities.harness_integration.kinds import HARNESS_INTEGRATION_DESCRIPTOR
    from agentworks.capabilities.secret_backend.kinds import SECRET_BACKEND_DESCRIPTOR
    from agentworks.capabilities.vm_platform.kinds import VM_PLATFORM_DESCRIPTOR

    return (
        VM_PLATFORM_DESCRIPTOR,
        HARNESS_INTEGRATION_DESCRIPTOR,
        GIT_CREDENTIAL_PROVIDER_DESCRIPTOR,
        SECRET_BACKEND_DESCRIPTOR,
    )


def descriptor_for_impl(impl: type) -> CapabilityKindDescriptor | None:
    """The descriptor of the kind ``impl`` implements, or ``None`` when it
    implements none.

    Derived from the implementation contract rather than from a
    ``capability_kind`` attribute each implementation would restate: the
    descriptor already names the base an implementation must derive from,
    and registration conformance already enforces it, so asking which base
    this class satisfies is reading one fact rather than declaring a second
    one that could disagree.

    ``None`` rather than an error for a class of no kind, and that is not
    a tolerance: registration refuses any implementation that does not
    derive from its kind's contract, so the only classes reaching here
    with no kind are ones that never register (a bare ``Capability``
    subclass in a test, exercising the base's own contract). Such a class
    has no kind, so it has no kind-level config contract either, which is
    exactly what the caller needs to know.
    """
    for descriptor in capability_descriptors():
        contract = descriptor.implementation_contract
        if issubclass(impl, contract):
            return descriptor
    return None


def descriptor_for(kind: str) -> CapabilityKindDescriptor:
    """The descriptor for ``kind``.

    Raises ``StateError`` for a kind with no descriptor: the table is the
    capability-kind enumeration, so asking for a kind it does not carry is a
    framework bug (a new capability kind added without its record), never
    operator data.
    """
    for descriptor in capability_descriptors():
        if descriptor.kind == kind:
            return descriptor
    known = ", ".join(d.kind for d in capability_descriptors())
    raise StateError(f"no capability-kind descriptor for {kind!r} (known capability kinds: {known})")


def mapping_descriptors_for_host(host_kind: str) -> tuple[CapabilityKindDescriptor, ...]:
    """Descriptors whose map-key-selected config is hosted by ``host_kind``.

    Descriptor registration order is preserved. The table is checked as a
    whole before any record is returned, because duplicate host fields and a
    target kind with the wrong miss policy are framework declaration errors,
    not malformed operator data.
    """
    descriptors = capability_descriptors()
    _validate_mapping_descriptors(descriptors)
    return tuple(
        descriptor
        for descriptor in descriptors
        if descriptor.mapping_host is not None and descriptor.mapping_host.host_kind == host_kind
    )


def _validate_mapping_descriptors(descriptors: tuple[CapabilityKindDescriptor, ...]) -> None:
    """Enforce the invariants every map-host consumer relies on."""
    from agentworks.resources.kind import KIND_REGISTRY
    from agentworks.schema._shape import Collection, shape_of
    from agentworks.schema.reference import RefRelationship

    claimed: dict[tuple[str, str], str] = {}
    for descriptor in descriptors:
        schema = descriptor.mapping_schema
        host = descriptor.mapping_host
        if (schema is None) != (host is None):
            raise StateError(f"{descriptor.kind} must declare mapping_schema and mapping_host together")
        if host is None:
            continue
        strategy = KIND_REGISTRY.get(host.host_kind)
        if strategy is None or strategy.category != "declarable":
            raise StateError(
                f"{descriptor.kind} mapping host kind {host.host_kind!r} is not a declarable resource kind"
            )
        model = getattr(strategy, "model", None)
        field = getattr(model, "model_fields", {}).get(host.field_name)
        if field is None or shape_of(field).collection is not Collection.MAPPING:
            raise StateError(
                f"{descriptor.kind} mapping host field {host.host_kind}.{host.field_name} is not mapping-shaped"
            )
        key = (host.host_kind, host.field_name)
        previous = claimed.get(key)
        if previous is not None:
            raise StateError(
                f"{descriptor.kind} and {previous} both claim mapping host {host.host_kind}.{host.field_name}"
            )
        claimed[key] = descriptor.kind
        if host.key_reference.relationship is not RefRelationship.USES:
            raise StateError(f"{descriptor.kind} mapping host keys must use the USES relationship")
        target = KIND_REGISTRY.get(host.key_reference.kind)
        if target is None:
            raise StateError(
                f"{descriptor.kind} mapping host key target {host.key_reference.kind!r} is not a resource kind"
            )
        if target.miss_policy != "error":
            raise StateError(
                f"{descriptor.kind} mapping host key target {host.key_reference.kind!r} must use miss_policy='error'"
            )
