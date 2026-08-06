"""The capability-kind descriptor table matches the live wiring it
describes (declarative-schema SDD step 2.0).

Every field is checked against the thing it claims to describe, by object
IDENTITY wherever an object is at stake (``kind_strategy is
KIND_REGISTRY[kind]``, ``registry() is`` the live dict), so a record cannot
quietly drift from the wiring while looking plausible.

The table was introduced additively, when no switchboard site derived from
it yet and nothing else would notice a record that lied. As each site
derives, these checks stop being the only guard and become the regression
lock underneath the derivation. Where a check would go tautological on
derivation (the row content and provenance labels a derived publisher now
produces from the very fields it is being compared to), the expectation is
spelled out here instead, so the assertion keeps holding the operator-facing
behavior rather than holding a mirror up to the table.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    RegistryPolicy,
    capability_descriptors,
    descriptor_for,
)
from agentworks.capabilities.publish import publish_capability_rows
from agentworks.errors import StateError
from agentworks.manifests.decode import CAPABILITY_FIELDS
from agentworks.resources.graph import Readiness, _capability_node_readiness
from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.registry import Registry
from tests.plugins._fixtures import ConformingVMPlatform

_KNOWN_KINDS = ("vm-platform", "harness-integration", "git-credential-provider", "secret-backend")

_BUILTIN_PUBLISHER_SOURCES = {
    "vm-platform": "agentworks.capabilities.vm_platform",
    "harness-integration": "agentworks.capabilities.harness_integration",
    "git-credential-provider": "agentworks.capabilities.git_credential",
    "secret-backend": "agentworks.secrets",
}
"""The ``Origin.built_in`` source label each kind's built-in rows carry, as
operators see it in ``agw resource describe``. Held here as the expectation,
not read off the descriptor."""

_ROWS_CARRY_DESCRIPTION = {
    "vm-platform": True,
    "harness-integration": False,
    "git-credential-provider": False,
    "secret-backend": True,
}
"""Which kinds' rows carry a ``description``. The split is a latent
inconsistency (recorded as a follow-up candidate, not fixed at 2.0, because
levelling it would change row content), so it is pinned rather than
described."""


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


def _published_rows(descriptor: CapabilityKindDescriptor) -> list[tuple[str, Any]]:
    """The kind's built-in rows, published into a fresh Registry exactly as
    ``bootstrap`` publishes them."""
    registry = Registry.empty()
    publish_capability_rows(registry, descriptor)
    return list(registry.iter_kind_items(descriptor.kind))


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
    seated_impls = list(descriptor.registry().items())
    assert seated_impls, f"{descriptor.kind} has an empty registry; this test would prove nothing"
    for name, seated in seated_impls:
        assert isinstance(seated, type) is holds_classes, f"{descriptor.kind} {name!r}"


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_publisher_source_is_the_label_the_builtin_rows_carry(
    descriptor: CapabilityKindDescriptor,
) -> None:
    """Publish the kind's built-in rows and read the label back off them.

    The expected labels are spelled out here rather than read from the
    descriptor: they are operator-visible provenance that the switchboard
    collapse had to preserve byte-for-byte (secret-backend in particular
    publishes as ``agentworks.secrets``, the package, not the ``backends``
    module that used to hold its publisher). Comparing the rows against the
    field that produced them would only prove the publisher can copy a
    string."""
    published = _published_rows(descriptor)
    assert published, f"{descriptor.kind} published no built-in rows"
    expected = _BUILTIN_PUBLISHER_SOURCES[descriptor.kind]
    assert descriptor.publisher_source == expected
    for name, row in published:
        assert row.origin.variant == "built-in", name
        assert row.origin.source == expected, name


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_entry_factory_builds_the_kinds_current_row(descriptor: CapabilityKindDescriptor) -> None:
    """The published row is the kind's own row type, carrying ``description``
    only for the two kinds whose rows have ever carried it.

    Today's four rows differ (vm-platform and secret-backend carry a
    description, harness-integration and git-credential-provider do not), and
    the generic publisher must not quietly level them: unifying the rows
    would change row content, which is row-semantics work rather than
    switchboard work. That split is asserted from the expectation table
    below, so a factory that starts (or stops) carrying a description fails
    here rather than in whatever surface renders the row."""
    published = _published_rows(descriptor)
    assert published, f"{descriptor.kind} published no built-in rows; this test would prove nothing"
    carries_description = _ROWS_CARRY_DESCRIPTION[descriptor.kind]
    for name, row in published:
        impl = descriptor.registry()[name]
        assert type(row) is type(descriptor.entry_factory(name, impl, None))
        assert row.name == name
        if carries_description:
            assert row.description == impl.description, name
        else:
            assert not hasattr(row, "description"), name


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_publication_covers_every_registered_impl(descriptor: CapabilityKindDescriptor) -> None:
    """The generic publisher has no static structure to compare against, so
    it is pinned by its output: one row per registered impl, minus the impls
    a system plugin seated (whose rows ``publish_plugins`` owns).

    Publication is UNCONDITIONAL for the rest (R13): host support is the
    row's folded readiness, never its absence, so no impl may be filtered out
    here on any other ground."""
    from agentworks.plugins.registration import plugin_seated_names

    expected = set(descriptor.registry()) - set(plugin_seated_names(descriptor.kind))
    assert {name for name, _row in _published_rows(descriptor)} == expected


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_readiness_reproduces_the_graphs_per_kind_dispatch(descriptor: CapabilityKindDescriptor) -> None:
    """Same verdict AND same sentence as the graph's capability-node
    readiness branches, which the reason strings are pinned on elsewhere."""
    seated_impls = list(descriptor.registry().items())
    assert seated_impls, f"{descriptor.kind} has an empty registry; this test would prove nothing"
    for name, seated in seated_impls:
        assert descriptor.readiness(name, seated) == _capability_node_readiness(descriptor.kind, name)


class _UnsupportedPlatform(ConformingVMPlatform):
    """A platform that is never host-supported, anywhere."""

    name = "unsupported-fixture"
    description = "a platform with a known host-support verdict"

    @classmethod
    def unsupported_reason(cls) -> str | None:
        return "the fixture says so"


class _SupportedPlatform(ConformingVMPlatform):
    """A platform that is always host-supported, anywhere."""

    name = "supported-fixture"
    description = "a platform with a known host-support verdict"


def test_vm_platform_readiness_carries_the_host_support_sentence() -> None:
    """Pin BOTH vm-platform readiness branches, and the blocked branch's exact
    sentence, on every host.

    The shipped platforms cannot do this: whether `wsl2` is host-supported
    depends on the machine the suite runs on, so on Linux the blocked branch is
    exercised by accident and on Windows it would not be exercised at all. The
    sentence is operator-facing and the vm-site that depends on a platform
    propagates this same verdict, so it is pinned against stubs with known
    answers instead.
    """
    descriptor = descriptor_for("vm-platform")
    assert descriptor.readiness("supported-fixture", _SupportedPlatform) == Readiness.ready()
    assert descriptor.readiness("unsupported-fixture", _UnsupportedPlatform) == Readiness.blocked(
        "platform 'unsupported-fixture' is unsupported here: the fixture says so"
    )


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
