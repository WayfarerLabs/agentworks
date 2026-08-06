"""The capability-kind descriptor table matches the live wiring it
describes (declarative-schema SDD step 2.0).

The table is introduced ADDITIVELY: at this commit no switchboard site
derives from it yet, so nothing else would notice a record that lies. These
assertions are that notice. Every field is checked against the thing it
claims to describe, by object IDENTITY wherever an object is at stake
(``kind_strategy is KIND_REGISTRY[kind]``, ``registry() is`` the live dict),
so a record cannot quietly drift from the wiring while looking plausible.

As each site derives (later commits in step 2.0), these checks stop being
the only guard and become the regression lock underneath the derivations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    RegistryPolicy,
    capability_descriptors,
    descriptor_for,
)
from agentworks.errors import StateError
from agentworks.manifests.decode import CAPABILITY_FIELDS
from agentworks.resources.graph import _capability_node_readiness
from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.registry import Registry

if TYPE_CHECKING:
    from collections.abc import Callable

_KNOWN_KINDS = ("vm-platform", "harness-integration", "git-credential-provider", "secret-backend")


def _descriptors() -> tuple[CapabilityKindDescriptor, ...]:
    return capability_descriptors()


def _live_registry(kind: str) -> dict[str, Any]:
    """The live code registry for ``kind``, reached the long way round (the
    module that owns it), so the identity assertions compare the descriptor
    against the real object rather than against itself."""
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    registries: dict[str, dict[str, Any]] = {
        "vm-platform": VM_PLATFORM_REGISTRY,
        "harness-integration": HARNESS_INTEGRATION_REGISTRY,
        "git-credential-provider": GIT_CREDENTIAL_PROVIDER_REGISTRY,
        "secret-backend": SECRET_BACKEND_REGISTRY,
    }
    return registries[kind]


def _builtin_publisher(kind: str) -> Callable[[Registry], None]:
    """The kind's current built-in publisher, as ``bootstrap`` calls it."""
    from agentworks import secrets
    from agentworks.capabilities import git_credential, harness_integration
    from agentworks.capabilities import vm_platform as vm_platforms

    return {
        "vm-platform": vm_platforms.publish_to,
        "harness-integration": harness_integration.publish_to,
        "git-credential-provider": git_credential.publish_to,
        "secret-backend": secrets.publish_to,
    }[kind]


def _impl_class(impl: Any) -> type:
    """The impl CLASS, whichever policy its registry stores it under."""
    return impl if isinstance(impl, type) else type(impl)


# -- The table itself -------------------------------------------------------


def test_table_carries_exactly_the_four_known_capability_kinds() -> None:
    """Non-vacuity: a scan that silently sees nothing (a lazily-collected
    table read too early, a contributor that stopped being imported) fails
    loudly here rather than passing every other assertion trivially."""
    assert tuple(d.kind for d in _descriptors()) == _KNOWN_KINDS


def test_table_kinds_are_the_capability_category_kinds() -> None:
    """The descriptor table and ``KIND_REGISTRY``'s capability category are
    the same set of kinds. A new capability kind added without its record
    fails here."""
    capability_kinds = {kind for kind, strategy in KIND_REGISTRY.items() if strategy.category == "capability"}
    assert {d.kind for d in _descriptors()} == capability_kinds


def test_descriptor_for_raises_on_a_kind_with_no_record() -> None:
    with pytest.raises(StateError, match="no capability-kind descriptor"):
        descriptor_for("vm-site")


def test_descriptor_for_returns_the_table_record_itself() -> None:
    for descriptor in _descriptors():
        assert descriptor_for(descriptor.kind) is descriptor


# -- Each field against the wiring it describes -----------------------------


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_kind_strategy_is_the_object_in_kind_registry(descriptor: CapabilityKindDescriptor) -> None:
    """One strategy object per kind, referenced by the descriptor rather
    than duplicated: the co-located ``KIND_REGISTRY[...] = ...`` line stays
    where it is precisely because this identity makes divergence
    impossible."""
    assert descriptor.kind_strategy is KIND_REGISTRY[descriptor.kind]


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_registry_accessor_returns_the_live_registry_object(descriptor: CapabilityKindDescriptor) -> None:
    """Identity, not equality: seating and snapshot/restore mutate the
    returned dict in place, so a copy would silently break both."""
    assert descriptor.registry() is _live_registry(descriptor.kind)


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_registry_policy_matches_what_the_registry_actually_holds(
    descriptor: CapabilityKindDescriptor,
) -> None:
    """``CONSTRUCTED_SINGLETON`` is a claim about the live registry's
    contents (secret-backend holds instances, the other three hold
    classes), so check it against the contents."""
    holds_classes = descriptor.registry_policy is RegistryPolicy.CLASS_BY_NAME
    for name, seated in descriptor.registry().items():
        assert isinstance(seated, type) is holds_classes, f"{descriptor.kind} {name!r}"


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_publisher_source_is_the_label_the_builtin_rows_carry(
    descriptor: CapabilityKindDescriptor,
) -> None:
    """Publish through the kind's real publisher and read the label back off
    the rows. The labels are load-bearing operator-visible provenance, so
    they must survive the switchboard collapse byte-for-byte (secret-backend
    in particular publishes as ``agentworks.secrets``, the package, not the
    ``backends`` module fronting it)."""
    registry = Registry.empty()
    _builtin_publisher(descriptor.kind)(registry)
    published = list(registry.iter_kind_items(descriptor.kind))
    assert published, f"{descriptor.kind} published no built-in rows"
    for name, row in published:
        assert row.origin.variant == "built-in", name
        assert row.origin.source == descriptor.publisher_source, name


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_entry_factory_builds_the_kinds_current_row(descriptor: CapabilityKindDescriptor) -> None:
    """The row the factory builds is the same type, with the same content,
    as the one the kind's publisher builds today. This is what keeps the
    generic publisher (a later commit) from quietly changing row content:
    two of the four rows carry ``description`` and two do not."""
    registry = Registry.empty()
    _builtin_publisher(descriptor.kind)(registry)
    for name, published in registry.iter_kind_items(descriptor.kind):
        built = descriptor.entry_factory(name, descriptor.registry()[name], None)
        assert type(built) is type(published)
        assert built.name == published.name
        assert getattr(built, "description", None) == getattr(published, "description", None)


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_readiness_reproduces_the_graphs_per_kind_dispatch(descriptor: CapabilityKindDescriptor) -> None:
    """Same verdict AND same sentence as the graph's capability-node
    readiness branches, which the reason strings are pinned on elsewhere."""
    for name, seated in descriptor.registry().items():
        assert descriptor.readiness(name, seated) == _capability_node_readiness(descriptor.kind, name)


def test_manifest_sections_match_the_decoders_host_surfaces() -> None:
    """The host pairs are exactly decode's ``CAPABILITY_FIELDS``, and only
    for the accept-warn surfaces: session-template hardened to the tagged
    shape in wave 1 and is handled by its own decoder branch, so folding it
    into ``CAPABILITY_FIELDS`` would be a real behavior change."""
    accept_warn = {
        d.manifest_section.host_kind: (d.manifest_section.naming_field, d.manifest_section.config_field)
        for d in _descriptors()
        if d.manifest_section is not None and d.manifest_section.legacy_string_shape == "accept-warn"
    }
    assert accept_warn == CAPABILITY_FIELDS
    assert set(accept_warn) == {"vm-site", "git-credential"}

    hardened = {d.kind: d.manifest_section for d in _descriptors() if d.manifest_section is not None}
    assert hardened["harness-integration"].host_kind == "session-template"
    assert hardened["harness-integration"].legacy_string_shape == "reject"
    assert descriptor_for("secret-backend").manifest_section is None


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_config_slots_are_empty_and_immutable(descriptor: CapabilityKindDescriptor) -> None:
    """No kind declares a config model until step 2.3. The field exists now
    so wave 4's facet slots need no record reshape; ``frozen=True`` alone
    would not stop the mapping behind it being mutated."""
    assert dict(descriptor.config_slots) == {}
    with pytest.raises(TypeError):
        descriptor.config_slots["default"] = object()  # type: ignore[index]


# -- Conformance of everything this build ships -----------------------------


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_every_registered_builtin_impl_conforms(descriptor: CapabilityKindDescriptor) -> None:
    """Registration-time conformance is only worth wiring in if what we
    already ship passes it."""
    for name, seated in descriptor.registry().items():
        assert conformance_error(descriptor, _impl_class(seated)) is None, f"{descriptor.kind} {name!r}"


def test_every_shipped_plugin_impl_conforms() -> None:
    """The shipped plugins' declared impls conform too, checked off the
    descriptors rather than off the registries, so a plugin impl that never
    seats (because a built-in occupies its name) is still covered."""
    from agentworks.plugins import SYSTEM_PLUGINS

    assert SYSTEM_PLUGINS, "the shipped plugin index is empty; this test would prove nothing"
    for plugin in SYSTEM_PLUGINS.values():
        for kind, impls in plugin.capabilities.items():
            descriptor = descriptor_for(kind)
            for impl in impls:
                assert conformance_error(descriptor, impl) is None, f"{plugin.name} {kind} {impl.__name__}"
