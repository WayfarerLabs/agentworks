"""The capability-kind descriptor table: one frozen record per kind.

This table enumerates ``vm-platform``, ``harness-integration``,
``git-credential-provider``, and ``secret-backend`` for framework consumers.

Two structural rules make the table safe to consume from anywhere:

- **Per-package contribution.** Each capability package builds its record
  beside its kind strategy. Nothing here knows a kind's internals.
- **Lazy collection, for cycle safety.** :func:`capability_descriptors`
  imports contributing modules inside the function. The ``registry`` field
  is callable for the same reason.

Each descriptor's ``kind_strategy`` is the same object registered in
``KIND_REGISTRY``; the table self-test asserts that identity.
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


class RegistryPolicy(Enum):
    """What a capability kind's code registry stores under each name."""

    CLASS_BY_NAME = "class-by-name"
    """The impl CLASS itself (vm-platform, harness-integration,
    git-credential-provider)."""

    CONSTRUCTED_SINGLETON = "constructed-singleton"
    """One instance built at seating time. Used by ``secret-backend``."""


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
    (``secret-backend``, keyed by ``backend_mappings``)."""


@dataclass(frozen=True)
class HostSurface:
    """How a capability kind is selected inside a declarable kind's spec.

    The live manifest shape is one tagged table on ``naming_field``. Decode
    also uses ``config_field`` to recognize the release-scoped sibling shape
    and provide its migration error.
    """

    host_kind: str
    """The DECLARABLE kind whose spec selects this capability
    (``"vm-site"``, ``"git-credential"``, ``"session-template"``)."""

    naming_field: str
    """The spec field naming the capability (``"platform"``)."""

    config_field: str
    """The RETIRED sibling field that used to hold the capability's config
    blob (``"platform_config"``).

    There is no such field any more: the row carries one tagged block, so
    this exists for exactly one reader, ``decode._reject_legacy_shape``,
    which names the retired field in the error that tells an operator how
    to rewrite the 0.14 shape. It goes when that guard does, and the
    guard's own docstring says when."""


@dataclass(frozen=True)
class CapabilityKindDescriptor:
    """Everything the framework needs to know about one capability kind.

    Core-owned and frozen: plugins contribute IMPLEMENTATIONS of existing
    kinds, never new kinds. Domain operations stay domain-owned: nothing
    here touches ``VMPlatform.create``, ``SecretBackend.batch_get``, or
    credential fill. The descriptor wires a kind into the framework; it does
    not absorb what makes the kind itself.
    """

    kind: str
    """The capability kind identifier, matching ``KIND_REGISTRY``'s key."""

    contract_version: int
    """The implementation contract version this build accepts. Registration
    requires an exact match."""

    implementation_contract: type
    """The base class or protocol an impl must satisfy. NOT uniform in
    shape: three kinds declare an ABC and are checked nominally, while
    ``SecretBackend`` is a ``Protocol`` whose members include properties, so
    it is checked structurally (``required_attributes`` plus
    ``required_operations``) and this field is documentary for that kind. See
    :func:`agentworks.capabilities.conformance.conformance_error`."""

    registry_policy: RegistryPolicy
    """Whether the kind's registry stores classes or constructed
    instances."""

    registry: Callable[[], dict[str, Any]]
    """Lazy accessor for the kind's live code registry. A callable, not the
    dict, so this module never imports the capability packages at load. The
    returned object is the LIVE registry: callers that mutate it (seating,
    snapshot restore) are mutating the real thing, by design."""

    required_operations: frozenset[str]
    """The domain operations the framework depends on being present and
    callable on an impl. For the ABC kinds this restates what
    ``inspect.isabstract`` already enforces; for ``secret-backend`` (a
    Protocol, with nothing to leave abstract) it IS the enforcement."""

    required_attributes: frozenset[str]
    """The non-operation members the framework reads off an impl, beyond the
    universal ``name`` / ``description``. EMPTY for the ABC kinds, whose base
    class supplies every such member to any subclass. For ``secret-backend``
    it carries ``interactive``, which the resolve loop reads on every chain
    pass: a Protocol declares it but cannot supply it, so without this field
    a backend omitting it seats cleanly and raises ``AttributeError`` deep in
    resolution, which is the exact failure this module exists to end."""

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
    """The ``Origin.built_in`` source label the kind's built-in rows carry
    (``"agentworks.secrets"`` for secret-backend, whose publisher fronts
    ``secrets/backends.py``)."""

    manifest_section: HostSurface | None
    """How the kind is selected in its host's manifest spec, or ``None``
    when no declarable kind hosts it (``secret-backend``, whose
    ``backend_mappings`` map key already names the capability)."""

    config_schema: ConfigContract
    """What a config model offered for this kind must be.

    The framework never asks an implementation for "its schema"; it asks
    for the config it offers at a FACET (``Capability.config_for``), and
    every model that comes back has to satisfy this contract. A capability
    whose methods run at several levels with different config is then a
    per-capability declaration rather than a framework change."""

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
    from agentworks.capabilities.vm_platform.kinds import VM_PLATFORM_DESCRIPTOR
    from agentworks.secrets.kinds import SECRET_BACKEND_DESCRIPTOR

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

    Nominal, so it answers for the three ABC kinds and not for the Protocol
    kind, whose implementations derive from nothing. That is the boundary
    of what needs it: secret backends never construct through the shared
    capability lifecycle, and every caller reaches them by kind and name.

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
        if not getattr(contract, "_is_protocol", False) and issubclass(impl, contract):
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
