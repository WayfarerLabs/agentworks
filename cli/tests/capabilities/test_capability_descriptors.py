"""The capability-kind descriptor table matches the live wiring it
describes (declarative-schema SDD step 2.0).

Every field is checked against the thing it claims to describe, by object
IDENTITY wherever an object is at stake (``registry() is`` the live dict),
so a record cannot quietly drift from the wiring while looking plausible.

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

from dataclasses import replace
from typing import Any

import pytest

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    ModelInputDomain,
    _validate_mapping_descriptors,
    capability_descriptors,
    descriptor_for,
)
from agentworks.capabilities.git_credential.kinds import GitCredentialProviderEntry
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.publish import publish_capability_rows
from agentworks.capabilities.secret_backend.kinds import SecretBackendEntry
from agentworks.capabilities.vm_platform import VMPlatformEntry
from agentworks.errors import StateError
from agentworks.manifests.decode import _hosting_descriptors
from agentworks.resources.graph import Readiness, _capability_node_readiness
from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.registry import Registry
from agentworks.schema import AgwModel, AgwRootModel, ResourceRef
from agentworks.schema.reference import RefRelationship
from tests.plugins._fixtures import ConformingVMPlatform

_KNOWN_KINDS = ("vm-platform", "harness-integration", "git-credential-provider", "secret-backend")

_BUILTIN_PUBLISHER_SOURCES = {
    "vm-platform": "agentworks.capabilities.vm_platform",
    "harness-integration": "agentworks.capabilities.harness_integration",
    "git-credential-provider": "agentworks.capabilities.git_credential",
    "secret-backend": "agentworks.capabilities.secret_backend",
}
"""The ``Origin.built_in`` source label each kind's built-in rows carry.

Held here as the expectation, not read off the descriptor.
"""

_ROW_TYPES: dict[str, type[Any]] = {
    "vm-platform": VMPlatformEntry,
    "harness-integration": HarnessIntegrationEntry,
    "git-credential-provider": GitCredentialProviderEntry,
    "secret-backend": SecretBackendEntry,
}
"""The row type each kind publishes, named here rather than read back off
the descriptor that built it. The generic publisher builds every row through
``entry_factory``, so comparing a published row's type against another call
of the same factory would only prove a factory is deterministic."""

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
    from agentworks.capabilities.secret_backend import SECRET_BACKEND_REGISTRY
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

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
def test_registry_accessor_returns_the_live_registry_object(descriptor: CapabilityKindDescriptor) -> None:
    """Identity, not equality: seating and snapshot/restore mutate the
    returned dict in place, so a copy would silently break both."""
    assert descriptor.registry() is _live_registry(descriptor.kind)


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_every_registry_holds_implementation_classes_by_name(
    descriptor: CapabilityKindDescriptor,
) -> None:
    """Every live registry stores the implementation CLASS under each name.

    What the adapters, the graph projection, and every documentation
    surface assume when they read a registry without constructing anything:
    a registry that started storing instances would break all three, and
    ``test_plugin_framework.py`` pins the non-constructing half from the
    other side.
    """
    seated_impls = list(descriptor.registry().items())
    assert seated_impls, f"{descriptor.kind} has an empty registry; this test would prove nothing"
    for name, seated in seated_impls:
        assert isinstance(seated, type), f"{descriptor.kind} {name!r}"


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_publisher_source_is_the_label_the_builtin_rows_carry(
    descriptor: CapabilityKindDescriptor,
) -> None:
    """Publish the kind's built-in rows and read the label back off them.

    The expected labels are spelled out here rather than read from the
    descriptor: they are operator-visible provenance published from each
    capability package. Comparing the rows against the
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
    switchboard work. Both the type and the split are asserted from the
    expectation tables above rather than from the factory that produced the
    row, so a factory that starts (or stops) carrying a description, or that
    starts building some other row type, fails here rather than in whatever
    surface renders the row."""
    published = _published_rows(descriptor)
    assert published, f"{descriptor.kind} published no built-in rows; this test would prove nothing"
    carries_description = _ROWS_CARRY_DESCRIPTION[descriptor.kind]
    for name, row in published:
        impl = descriptor.registry()[name]
        assert type(row) is _ROW_TYPES[descriptor.kind], name
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
    assert expected, f"{descriptor.kind} has no built-in impls; this test would prove nothing"
    assert {name for name, _row in _published_rows(descriptor)} == expected


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


def test_the_graph_routes_a_capability_node_to_its_own_kinds_verdict() -> None:
    """The graph's capability-node readiness, through the graph's own entry
    point, against verdicts only the right route can produce.

    ``test_plugin_framework.py`` explicitly DELEGATES the readiness
    dispatch here, because it has no static structure to compare and has to
    be pinned by output. What was here compared
    ``descriptor.readiness(name, seated)`` against
    ``_capability_node_readiness(kind, name)``, and the second expression
    expands to the first, so both sides computed the same thing from the
    same table: a mis-route survived it whenever the wrong source happened
    to agree, which for the shipped impls is almost always (nearly all of
    them are ready, so ready == ready proves nothing).

    Literal verdicts instead, through fixtures with known answers. The
    blocked sentence names the fixture, so it cannot be produced by
    dispatching vm-platform to another kind's record (every other kind is
    unconditionally ready) nor by handing the callable a different impl
    (only this one says that).

    BOTH vm-platform branches are pinned here, and the blocked branch's
    exact sentence with them, because ``_capability_node_readiness``
    expands to the descriptor callable: the two verdicts below are what
    that callable answered. The shipped platforms could not pin this at
    all, since whether `wsl2` is host-supported depends on the machine the
    suite runs on, so on Linux the blocked branch would be exercised by
    accident and on Windows not at all. The sentence is operator-facing and
    a vm-site depending on a platform propagates this same verdict.
    """
    from agentworks.plugins import Plugin, seated_plugin

    with seated_plugin(
        Plugin(
            name="descriptor-readiness-fixtures",
            capabilities={"vm-platform": (_SupportedPlatform, _UnsupportedPlatform)},
        )
    ):
        assert _capability_node_readiness("vm-platform", "supported-fixture") == Readiness.ready()
        assert _capability_node_readiness("vm-platform", "unsupported-fixture") == Readiness.blocked(
            "platform 'unsupported-fixture' is unsupported here: the fixture says so"
        )

    # The two kinds with no host-support concept, pinned as the ready they
    # are documented to be rather than against their own callable. Not
    # secret-backend, which HAS one (`onepassword` is blocked where the op
    # CLI is not installed), and so is host-dependent for the same reason
    # the platforms above are done with fixtures.
    for kind in ("harness-integration", "git-credential-provider"):
        registry = descriptor_for(kind).registry()
        assert registry, f"{kind} has an empty registry; this test would prove nothing"
        for name in registry:
            assert _capability_node_readiness(kind, name) == Readiness.ready(), (kind, name)


def test_manifest_sections_match_the_decoders_host_surfaces() -> None:
    """Decode's host dispatch is exactly the four host surfaces, with the
    capability kind and the field each one names.

    Spelled out rather than recomputed from the descriptors, because decode
    now DERIVES this map from them: comparing the derivation against itself
    would only prove a comprehension runs. Membership is the dispatch, so
    the entries are the whole behavior. A kind gaining or losing a host
    surface changes which specs are read as capability blocks, which is why
    this is a literal.
    """
    assert {host: (d.kind, d.manifest_section.naming_field) for host, d in _hosting_descriptors().items()} == {
        "vm-site": ("vm-platform", "platform"),
        "git-credential": ("git-credential-provider", "provider"),
        "session-template": ("harness-integration", "harness_integration"),
        "secret-source": ("secret-backend", "backend"),
    }

    host_kinds = [d.manifest_section.host_kind for d in _descriptors()]
    assert len(host_kinds) == len(set(host_kinds)), (
        f"two capability kinds claim the same host: {host_kinds}. Decode keys its "
        f"fold dispatch by host_kind, so the second record would silently "
        f"overwrite the first and one host's fold would vanish."
    )
    source_host = descriptor_for("secret-backend").manifest_section
    assert (source_host.host_kind, source_host.naming_field) == (
        "secret-source",
        "backend",
    )


# -- The kind's config contract ---------------------------------------------


def test_each_kinds_config_contract_matches_how_its_config_is_dispatched() -> None:
    """The contract's two halves are one fact each, and both are visible in
    a manifest: whether the config is mapping-shaped, and whether it is
    selected by a tag inside the value or by the key it hangs under.

    Spelled out rather than derived from the records, so a record that
    changes shape has to change this expectation too.
    """
    contracts = {d.kind: d.config_schema for d in _descriptors()}

    for tagged in ("vm-platform", "harness-integration"):
        assert contracts[tagged].base is AgwModel, tagged
        assert contracts[tagged].discriminator == "name", tagged

    assert contracts["harness-integration"].layered_merge is True
    assert all(not contract.layered_merge for kind, contract in contracts.items() if kind != "harness-integration")

    assert contracts["git-credential-provider"].base is AgwModel
    assert contracts["git-credential-provider"].discriminator == "name"

    backend = descriptor_for("secret-backend")
    assert backend.config_schema.base is AgwModel
    assert backend.config_schema.discriminator == "name"
    assert backend.config_schema.forbidden_reference_kinds == frozenset({"secret"})
    assert backend.mapping_schema is not None
    assert backend.mapping_schema.base is AgwRootModel
    assert backend.mapping_schema.discriminator is None
    assert backend.mapping_schema.input_domain is ModelInputDomain.JSON_NATIVE
    assert backend.mapping_schema.layered_merge is False
    assert backend.mapping_host is not None
    assert backend.mapping_host.host_kind == "secret"
    assert backend.mapping_host.field_name == "backend_mappings"
    assert backend.mapping_host.key_reference.kind == "secret-source"
    assert backend.mapping_host.key_reference.relationship is RefRelationship.USES
    assert backend.mapping_host.false_opt_out is True


def test_mapping_contract_and_host_are_declared_together() -> None:
    for descriptor in _descriptors():
        assert (descriptor.mapping_schema is None) == (descriptor.mapping_host is None), descriptor.kind


def test_mapping_descriptor_requires_contract_and_host_together() -> None:
    backend = descriptor_for("secret-backend")
    with pytest.raises(StateError, match="mapping_schema and mapping_host together"):
        _validate_mapping_descriptors((replace(backend, mapping_host=None),))


def test_mapping_descriptor_requires_a_declarable_mapping_shaped_host() -> None:
    backend = descriptor_for("secret-backend")
    assert backend.mapping_host is not None

    capability_host = replace(backend.mapping_host, host_kind="secret-backend")
    with pytest.raises(StateError, match="not a declarable resource kind"):
        _validate_mapping_descriptors((replace(backend, mapping_host=capability_host),))

    scalar_field = replace(backend.mapping_host, field_name="hint")
    with pytest.raises(StateError, match="not mapping-shaped"):
        _validate_mapping_descriptors((replace(backend, mapping_host=scalar_field),))


def test_mapping_descriptor_requires_unique_host_field_ownership() -> None:
    backend = descriptor_for("secret-backend")
    with pytest.raises(StateError, match="both claim mapping host secret.backend_mappings"):
        _validate_mapping_descriptors((backend, replace(backend, kind="other-mapping-kind")))


def test_mapping_descriptor_key_is_a_hard_uses_reference() -> None:
    backend = descriptor_for("secret-backend")
    assert backend.mapping_host is not None

    inherits = replace(
        backend.mapping_host,
        key_reference=replace(
            backend.mapping_host.key_reference,
            relationship=RefRelationship.INHERITS,
        ),
    )
    with pytest.raises(StateError, match="must use the USES relationship"):
        _validate_mapping_descriptors((replace(backend, mapping_host=inherits),))

    auto_declared_target = replace(
        backend.mapping_host,
        key_reference=ResourceRef(kind="vm-template", usage="a fixture target"),
    )
    with pytest.raises(StateError, match="must use miss_policy='error'"):
        _validate_mapping_descriptors((replace(backend, mapping_host=auto_declared_target),))


# -- Conformance of everything this build ships -----------------------------


@pytest.mark.parametrize("descriptor", _descriptors(), ids=lambda d: d.kind)
def test_every_registered_builtin_impl_conforms(descriptor: CapabilityKindDescriptor) -> None:
    """Registration-time conformance is only worth wiring in if what we
    already ship passes it.

    Every check the contract makes, over every seated implementation,
    plugin-supplied ones included: a shipped plugin's impls are seated by
    the time this runs, so iterating the registry covers them. That is why
    "the models a kind registers satisfy its ``config_schema.base``" is not
    a test of its own; ``conformance_error`` makes exactly that
    ``issubclass`` check (``capabilities/conformance.py:193``) and, unlike a
    bare subclass assertion, fails rather than skips when an impl declares
    no model at all.
    """
    for name, seated in descriptor.registry().items():
        assert conformance_error(descriptor, seated) is None, f"{descriptor.kind} {name!r}"
