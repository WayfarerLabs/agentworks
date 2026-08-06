"""The plugin framework, provable in isolation against a fixture (Phase 2).

Covers the ``Plugin`` descriptor and its immutability, the atomic validating
``register_plugin`` and its seating-guard collision layer, the per-kind
``CapabilityAdapter`` table, the ``seated_plugin`` snapshot helper, and the
inverted installed index. No ``build_registry`` wiring, no publish, no
enablement: the fixture proves the framework.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import cast

import pytest

import agentworks.plugins as plugins_pkg
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.errors import StateError
from agentworks.plugins import (
    CAPABILITY_ADAPTERS,
    SYSTEM_PLUGINS,
    Plugin,
    PluginError,
    register_plugin,
    seated_plugin,
)
from agentworks.resources.graph import Readiness
from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.origin import Origin
from tests.plugins._fixtures import (
    ConformingSecretBackend,
    ConformingVMPlatform,
    FixtureBackend,
    FixtureHarnessIntegration,
    FixtureVMPlatform,
    conforming_impl,
    fixture_plugin,
)


def _snapshot_registries() -> dict[str, dict[str, object]]:
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    return {
        "vm-platform": dict(VM_PLATFORM_REGISTRY),
        "harness-integration": dict(HARNESS_INTEGRATION_REGISTRY),
        "git-credential-provider": dict(GIT_CREDENTIAL_PROVIDER_REGISTRY),
        "secret-backend": dict(SECRET_BACKEND_REGISTRY),
    }


def _plugin_origin() -> Origin:
    return Origin.system_plugin(plugin="fixture", source="agentworks.plugins.fixture")


# -- Descriptor immutability (data invariant, enforced at construction) -----


def test_capabilities_normalized_to_immutable_mapping_of_tuples() -> None:
    plugin = Plugin(name="p", capabilities={"vm-platform": (FixtureVMPlatform,)})
    assert isinstance(plugin.capabilities, MappingProxyType)
    assert plugin.capabilities["vm-platform"] == (FixtureVMPlatform,)
    with pytest.raises(TypeError):
        plugin.capabilities["harness-integration"] = (FixtureHarnessIntegration,)  # type: ignore[index]


def test_descriptor_is_constructible_without_a_registry() -> None:
    # Immutability is enforced at construction; VALIDITY is not (that is
    # register_plugin's contract), so a bogus descriptor still constructs.
    plugin = Plugin(name="", capabilities={"nope": (object,)})
    assert plugin.name == ""


# -- Descriptor validation rejections (typed PluginError naming plugin) -----


@pytest.mark.parametrize("bad_name", ["", "has/slash"])
def test_rejects_bad_plugin_name(bad_name: str) -> None:
    plugin = Plugin(name=bad_name)
    with pytest.raises(PluginError, match=repr(bad_name)):
        register_plugin(plugin)


def test_rejects_unknown_capability_kind() -> None:
    plugin = Plugin(name="p", capabilities={"not-a-kind": (FixtureVMPlatform,)})
    with pytest.raises(PluginError) as exc:
        register_plugin(plugin)
    message = str(exc.value)
    assert "'p'" in message
    assert "not-a-kind" in message


def test_rejects_instance_instead_of_class_the_secret_backend_trap() -> None:
    # The natural trap: passing EnvVarBackend() rather than EnvVarBackend.
    # The cast models an author who typed the instance; register_plugin
    # catches it as a typed error, not a later AttributeError.
    trap = cast("type", FixtureBackend())
    plugin = Plugin(name="p", capabilities={"secret-backend": (trap,)})
    with pytest.raises(PluginError, match="is not a class"):
        register_plugin(plugin)


def test_rejects_impl_with_missing_name() -> None:
    class NoName:
        pass

    plugin = Plugin(name="p", capabilities={"vm-platform": (NoName,)})
    with pytest.raises(PluginError, match="invalid capability name"):
        register_plugin(plugin)


def test_rejects_impl_with_slash_bearing_name() -> None:
    class SlashName:
        name = "a/b"

    plugin = Plugin(name="p", capabilities={"vm-platform": (SlashName,)})
    with pytest.raises(PluginError, match="a/b"):
        register_plugin(plugin)


def test_rejects_intra_descriptor_collision() -> None:
    one = conforming_impl("vm-platform", "dup")
    two = conforming_impl("vm-platform", "dup")

    plugin = Plugin(name="p", capabilities={"vm-platform": (one, two)})
    with pytest.raises(PluginError, match="intra-descriptor collision"):
        register_plugin(plugin)


# -- Contract conformance (descriptor-derived, before any registry write) ---


class _NotAPlatform:
    """Plausible from a distance: the metadata is there, but it implements
    nothing of the vm-platform contract. This is the class the old
    ``isinstance(impl, type)`` gate and ``cast`` waved through."""

    contract_version = 1
    name = "not-a-platform"
    description = "has the metadata and none of the contract"


class _AbstractPlatform(VMPlatform):
    """Derives from the contract but implements none of its power ops, so it
    can never be constructed."""

    contract_version = 1
    name = "abstract-platform"
    description = "abstract: no power ops implemented"


class _PlatformWithoutADescription(ConformingVMPlatform):
    name = "no-description-platform"
    # ``description`` is what the published capability row carries, so an
    # impl that omits it would publish a row with a missing field.


class _BackendMissingItsOperations:
    """Structurally NOT a ``SecretBackend``. The Protocol kind cannot be
    checked nominally (``issubclass`` against a Protocol with property
    members raises ``TypeError``), so member presence IS its conformance
    check. Carries ``interactive`` so this case isolates the missing
    OPERATIONS, leaving the missing-attribute case to the next class."""

    contract_version = 1
    name = "barely-a-backend"
    description = "none of the backend operations"
    interactive = False


class _BackendWithoutInteractive:
    """Every operation implemented, but not the ``interactive`` member the
    resolve loop reads on every chain pass. Nominally unenforceable (the
    contract is a Protocol) and not an operation, so without the descriptor's
    ``required_attributes`` this would seat and then raise ``AttributeError``
    deep in resolution.

    Spelled out rather than derived from ``ConformingSecretBackend``: the
    defect is an ABSENT member, and a subclass cannot un-inherit one.
    """

    contract_version = 1
    name = "no-interactive-backend"
    description = "omits the interactive member"

    def not_ready(self) -> Readiness:
        raise NotImplementedError

    def validate_mapping(self, owner: str, mapping: object) -> None:
        raise NotImplementedError

    def dependencies(self, mapping: object) -> tuple[object, ...]:
        raise NotImplementedError

    def would_attempt(self, secret: object, mapping: object) -> bool:
        raise NotImplementedError

    def describe_lookup(self, secret: object, mapping: object) -> str | None:
        raise NotImplementedError

    def batch_get(self, wants: object) -> dict[str, str]:
        raise NotImplementedError


class _PlatformOnAnOldContract(ConformingVMPlatform):
    name = "old-contract-platform"
    description = "written against a contract this build no longer supports"
    contract_version = 0


@pytest.mark.parametrize(
    ("kind", "impl", "expected"),
    [
        ("vm-platform", _NotAPlatform, "does not derive from VMPlatform"),
        ("vm-platform", _AbstractPlatform, "it is abstract"),
        ("vm-platform", _PlatformWithoutADescription, "'description' class attribute"),
        ("secret-backend", _BackendMissingItsOperations, "does not implement the required"),
        ("secret-backend", _BackendWithoutInteractive, "missing the required secret-backend attributes"),
        ("vm-platform", _PlatformOnAnOldContract, "declares contract_version 0"),
    ],
    ids=[
        "wrong-base",
        "abstract",
        "missing-metadata",
        "missing-operations",
        "missing-attribute",
        "unsupported-version",
    ],
)
def test_rejects_a_non_conforming_impl_naming_the_plugin(kind: str, impl: type, expected: str) -> None:
    """Each conformance check refuses its own defect with a ``PluginError``
    that names the plugin, the kind, and the impl."""
    plugin = Plugin(name="p", capabilities={kind: (impl,)})
    with pytest.raises(PluginError) as exc:
        register_plugin(plugin)
    message = str(exc.value)
    assert "'p'" in message
    assert impl.__name__ in message
    assert expected in message


def test_a_non_conforming_impl_is_refused_before_any_registry_write() -> None:
    """Conformance runs in the VALIDATING pass, so a descriptor whose first
    impl would seat cleanly and whose second does not conform seats nothing.
    Deferring the check to seating time would break that: the first impl
    would already be in its registry with no rollback path."""
    plugin = Plugin(
        name="p",
        capabilities={
            "vm-platform": (FixtureVMPlatform,),  # would seat fine
            "secret-backend": (_BackendMissingItsOperations,),  # does not conform
        },
    )
    before = _snapshot_registries()
    with pytest.raises(PluginError, match="does not satisfy"):
        register_plugin(plugin)
    assert _snapshot_registries() == before


# -- Atomicity: a mid-descriptor collision seats NOTHING --------------------


def test_atomic_registration_seats_nothing_on_a_mid_descriptor_collision() -> None:
    # A core built-in harness integration's name, different class.
    colliding = conforming_impl("harness-integration", "shell")

    # vm-platform seats cleanly first; harness-integration collides at the precheck.
    plugin = Plugin(
        name="p",
        capabilities={
            "vm-platform": (FixtureVMPlatform,),
            "harness-integration": (colliding,),
        },
    )
    before = _snapshot_registries()
    with pytest.raises(PluginError):
        register_plugin(plugin)
    assert _snapshot_registries() == before


def test_atomic_registration_survives_a_throwing_backend_constructor() -> None:
    # The secret-backend impl constructs during the precheck (before any
    # registry write), so a throwing __init__ leaves the earlier-planned
    # vm-platform UNSEATED and raises a typed PluginError, not a raw error.
    class ThrowingBackend(ConformingSecretBackend):
        name = "throwing-backend"
        description = "boom on construct"

        def __init__(self) -> None:
            raise RuntimeError("cannot construct")

    plugin = Plugin(
        name="p",
        capabilities={
            "vm-platform": (FixtureVMPlatform,),  # would seat fine, planned first
            "secret-backend": (ThrowingBackend,),  # constructor throws
        },
    )
    before = _snapshot_registries()
    with pytest.raises(PluginError, match="could not construct"):
        register_plugin(plugin)
    assert _snapshot_registries() == before


# -- Idempotency ------------------------------------------------------------


def test_registering_the_same_plugin_twice_is_a_no_op() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    plugin = fixture_plugin()
    with seated_plugin(plugin):
        register_plugin(plugin)  # second time
        assert cast("object", VM_PLATFORM_REGISTRY["fixture-vm"]) is FixtureVMPlatform


def test_a_different_impl_under_a_taken_name_is_a_typed_error() -> None:
    other = conforming_impl("vm-platform", "fixture-vm")  # same name, different class

    with seated_plugin(fixture_plugin()):
        clash = Plugin(name="other", capabilities={"vm-platform": (other,)})
        with pytest.raises(PluginError):
            register_plugin(clash)


def test_secret_backend_subclass_under_a_taken_name_is_not_idempotent() -> None:
    # matches() is exact identity, not isinstance: a subclass occupant must
    # not be silently merged. Seat the subclass first, then a registration of
    # the base class under the same name must collide (isinstance would have
    # wrongly treated it as an idempotent match).
    class BaseBackend(ConformingSecretBackend):
        name = "shared-backend"
        description = "base"

    class SubBackend(BaseBackend):
        name = "shared-backend"

    first = Plugin(name="first", capabilities={"secret-backend": (SubBackend,)})
    second = Plugin(name="second", capabilities={"secret-backend": (BaseBackend,)})
    with seated_plugin(first), pytest.raises(PluginError, match="already published by system plugin 'first'"):
        register_plugin(second)


# -- The seating guard IS the capability-clash layer ------------------------


def test_capability_clash_with_a_core_builtin_names_it_as_such() -> None:
    # Collides with the CORE built-in LimaPlatform.
    lima_like = conforming_impl("vm-platform", "lima")

    plugin = Plugin(name="p", capabilities={"vm-platform": (lima_like,)})
    with pytest.raises(PluginError) as exc:
        register_plugin(plugin)
    message = str(exc.value)
    assert "core built-in" in message
    assert "lima" in message


def test_capability_clash_between_two_plugins_names_the_other_plugin() -> None:
    first_impl = conforming_impl("vm-platform", "x")
    second_impl = conforming_impl("vm-platform", "x")  # same name, different class

    first = Plugin(name="first", capabilities={"vm-platform": (first_impl,)})
    second = Plugin(name="second", capabilities={"vm-platform": (second_impl,)})
    with seated_plugin(first):
        with pytest.raises(PluginError) as exc:
            register_plugin(second)
        message = str(exc.value)
        assert "already published by system plugin 'first'" in message


# -- Adapters: seat + build a row for all four kinds ------------------------


@pytest.mark.parametrize(
    ("kind", "seated_name", "expects_description"),
    [
        ("vm-platform", "fixture-vm", True),
        ("harness-integration", "fixture-harness", False),
        ("git-credential-provider", "fixture-provider", False),
        ("secret-backend", "fixture-backend", True),
    ],
)
def test_adapter_seats_and_builds_a_row(kind: str, seated_name: str, expects_description: bool) -> None:
    adapter = CAPABILITY_ADAPTERS[kind]
    origin = _plugin_origin()
    with seated_plugin(fixture_plugin()):
        assert adapter.peek(seated_name) is not None
        row = adapter.build_row(seated_name, origin)
        assert row.name == seated_name
        assert row.origin is origin
        assert row.origin.variant == "system-plugin"
        if expects_description:
            assert "Fixture" in row.description


def test_build_row_on_an_unseated_name_raises_state_error() -> None:
    adapter = CAPABILITY_ADAPTERS["vm-platform"]
    with pytest.raises(StateError):
        adapter.build_row("definitely-not-seated", _plugin_origin())


def test_capability_adapters_keys_match_the_capability_category_kinds() -> None:
    capability_kinds = {kind for kind, handler in KIND_REGISTRY.items() if handler.category == "capability"}
    assert set(CAPABILITY_ADAPTERS) == capability_kinds


# -- The installed index (inverted registration) ----------------------------


def test_shipped_index_ships_migrated_plugins() -> None:
    """The shipped index carries the migrated system plugins. Phase 8 ships
    ``onepassword`` (a capability-only secret-backend plugin, no manifests); the
    empty-index invariant held only for the framework phase, before any bundle
    migrated. As later phases migrate claude / proxmox / azure this set grows."""
    assert "onepassword" in SYSTEM_PLUGINS
    op = SYSTEM_PLUGINS["onepassword"]
    assert op.name == "onepassword"
    assert set(op.capabilities) == {"secret-backend"}
    assert op.manifests is None


class _FakeModule:
    """A stand-in for a shipped plugin module (structurally a
    ``_PluginModule``): a ``__name__`` and a ``PLUGIN`` descriptor."""

    def __init__(self, name: str, plugin: Plugin) -> None:
        self.__name__ = name
        self.PLUGIN = plugin


def test_index_rejects_a_duplicate_plugin_name_with_module_attribution() -> None:
    # Empty-capability plugins seat nothing, so this exercises the dedup
    # check without touching the capability registries.
    modules = [
        _FakeModule("tests.fake.a", Plugin(name="dup")),
        _FakeModule("tests.fake.b", Plugin(name="dup")),
    ]
    with pytest.raises(PluginError) as exc:
        plugins_pkg._build_installed_index(modules)
    message = str(exc.value)
    assert "duplicate system plugin name 'dup'" in message
    assert "tests.fake.b" in message


def test_index_wraps_a_registration_failure_with_the_module_name() -> None:
    bad = _FakeModule("tests.fake.bad", Plugin(name="p", capabilities={"unknown-kind": (FixtureVMPlatform,)}))
    with pytest.raises(PluginError) as exc:
        plugins_pkg._build_installed_index([bad])
    message = str(exc.value)
    assert "tests.fake.bad" in message
    assert "failed to register" in message


# -- seated_plugin round-trips, even on exception ---------------------------


def test_seated_plugin_round_trips_on_exception() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    before = _snapshot_registries()
    with pytest.raises(RuntimeError, match="boom"), seated_plugin(fixture_plugin()):
        assert "fixture-vm" in VM_PLATFORM_REGISTRY  # seated inside
        raise RuntimeError("boom")
    assert _snapshot_registries() == before
    assert "fixture-vm" not in VM_PLATFORM_REGISTRY
