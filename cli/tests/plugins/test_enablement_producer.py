"""The ``_node_enablement`` producer + consumer gating (Phase 4, LLD b).

Proves the load-bearing piece: a not-opted-in system plugin's capability rows
become present-but-DISABLED with a reason, exercised via a fixture plugin whose
rows are published by the test and finalized through the shipped
``finalize(enablement_sources=[...])`` seam (Phase 5 wires the source into
``build_registry``; this phase proves the producer + consumers directly).

Coverage, kind by kind through the ACTUAL consumer (R9 capability side, R14):

- vm-platform: a ``vm-site`` on a disabled plugin platform is not-ready with the
  enable-plugin hint (existing fold), and ``resolve_site`` refuses it.
- secret-backend: a disabled plugin backend is excluded from ``active_sources``
  but NOT from secret-mapping validation, which is unconditional over
  enablement (inert for resolution and validated are separate properties).
- git-credential-provider: a ``git-credential`` on a disabled plugin provider is
  not-ready (new propagate hook), ``resolve_git_credential_providers`` refuses
  it, and ``remote_advisories`` skips it.
- harness integration: a ``session-template`` on a disabled plugin harness integration lists ready, but
  ``ensure_harness_integration_enabled`` raises the enable-plugin error while the read-only
  ``_display_harness_integration`` still shows the name.

Plus the R13 multi-source seam (a stub second source composes; first-source-wins
the reason) and additive-ness (no source fires -> all-enabled, as the landed
refactor).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, cast

import pytest
from pydantic import BaseModel

from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
from agentworks.capabilities.secret_backend import LookupDescription, LookupDisposition
from agentworks.errors import ConfigError, StateError
from agentworks.git_credentials import remote_advisories
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.origin import Origin
from agentworks.plugins import Plugin, capability_adapters, seated_plugin
from agentworks.plugins.enablement import plugin_enablement_source
from agentworks.resources.graph import (
    DependencyState,
    DisabledMark,
    Enablement,
    compose_enablement,
)
from agentworks.resources.registry import Registry
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock, NonEmptyStr, SecretRef
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.resolve import active_sources
from agentworks.secrets.sources import SecretSourceDecl
from agentworks.sessions.manager._env import _display_harness_integration
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.initializer.credentials import resolve_git_credential_providers
from agentworks.vms.sites import VMSiteDecl, resolve_site
from tests.plugins._fixtures import (
    ConformingGitCredentialProvider,
    ConformingHarnessIntegration,
    ConformingSecretBackend,
    ConformingVMPlatform,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config
    from agentworks.resources.graph import EnablementSource
    from agentworks.secrets.base import MappingValue

PLUGIN = "cap-plugin"


# -- Fixture capability impls (REAL subclasses, so they fold through their
#    consumers and pass registration's conformance check). Never instantiated
#    by these tests except the secret backend, which the adapter constructs at
#    seating; the other three are used as classes (host-support / dependencies
#    classmethods only). ---------------------------------------------------


class _FixturePlatformConfig(AgwModel):
    """A config with a secret-naming field (like proxmox's
    ``token_secret``), so the real producer can be shown to WITHHOLD the
    implied secret when the platform is disabled: the site that names it
    goes not-ready, and ``has_ready_referrer`` excludes a not-ready
    referrer."""

    name: Literal["fixture-platform"]
    token_secret: Annotated[NonEmptyStr, SecretRef(usage="the fixture API token")] | None = None


class _FixtureVMPlatform(ConformingVMPlatform):
    name = "fixture-platform"
    description = "Fixture VM platform (test plugin)"
    config_model = _FixturePlatformConfig
    # The power ops come from the conforming base, which raises on each: the
    # fold never builds an instance.


class _FixtureHarnessIntegration(ConformingHarnessIntegration):
    name = "fixture-harness"
    description = "Fixture harness (test plugin)"


class _FixtureProvider(ConformingGitCredentialProvider):
    name = "fixture-provider"
    description = "Fixture git credential provider (test plugin)"
    # Its generated config model has no reference-marked field, so this
    # provider declares no token secret and the fixture credential's edge
    # set stays just the provider edge.


class _FixtureBackendMapping(AgwRootModel[Literal["good"]]):
    """A mapping vocabulary of exactly one accepted value, so the sentinel
    ``"bad"`` is rejected and the disabled-backend
    mapping-validation exclusion is provable."""


class _FixtureBackend(ConformingSecretBackend):
    """A nominal ``SecretBackend`` with a narrow mapping vocabulary."""

    name = "fixture-backend"
    description = "Fixture secret backend (test plugin)"
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = _FixtureBackendMapping

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(
            LookupDisposition.CANDIDATE if mapping is not None else LookupDisposition.NOT_APPLICABLE,
            None,
        )


def _capable_plugin(name: str = PLUGIN) -> Plugin:
    return Plugin(
        name=name,
        description="a capable test fixture plugin",
        capabilities={
            "vm-platform": (_FixtureVMPlatform,),
            "harness-integration": (_FixtureHarnessIntegration,),
            "git-credential-provider": (_FixtureProvider,),
            "secret-backend": (_FixtureBackend,),
        },
    )


def _publish_capability(registry: Registry, kind: str, name: str, plugin: str = PLUGIN) -> None:
    """Publish a seated fixture impl's capability row with a system-plugin
    origin (the shape ``publish_plugins`` will produce in Phase 5)."""
    origin = Origin.system_plugin(plugin=plugin, source=f"agentworks.plugins.{plugin}")
    row = capability_adapters()[kind].build_row(name, origin)
    registry.add(kind, name, row, origin)


def _publish_builtin_backend(registry: Registry, name: str) -> None:
    """Publish a single built-in ``secret-backend`` row (its impl is always
    seated in ``SECRET_BACKEND_REGISTRY``). Used instead of the whole-registry
    ``secret_backends.publish_to``, which would also sweep the plugin-seated
    fixture backend in under a built-in origin (Phase 5's publisher split, not
    this phase's concern)."""
    origin = Origin.built_in(source="agentworks.capabilities.secret_backend")
    row = capability_adapters()["secret-backend"].build_row(name, origin)
    registry.add("secret-backend", name, row, origin)


def _operator(name: str = "op.yaml") -> Origin:
    return Origin.operator_declared(file=Path(name), line=1)


def _plugin_source(*enabled: str) -> EnablementSource:
    config = cast("Config", SimpleNamespace(enabled_system_plugins=tuple(enabled)))
    return plugin_enablement_source(config)


def _present(registry: Registry, kind: str, name: str) -> bool:
    return any(n == name for n, _ in registry.iter_kind_items(kind))


# -- R9 capability side: the plugin source marks not-opted-in rows disabled -----


def test_not_opted_in_plugin_capability_is_disabled_opted_in_is_enabled() -> None:
    with seated_plugin(_capable_plugin()):
        disabled = Registry.empty()
        _publish_capability(disabled, "vm-platform", "fixture-platform")
        disabled.finalize(enablement_sources=[_plugin_source()])  # PLUGIN not enabled
        assert disabled.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.disabled

        enabled = Registry.empty()
        _publish_capability(enabled, "vm-platform", "fixture-platform")
        enabled.finalize(enablement_sources=[_plugin_source(PLUGIN)])  # opted in
        assert enabled.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.enabled


def test_no_source_leaves_a_plugin_row_enabled_the_landed_default() -> None:
    """Additive-ness: ``finalize()`` with no sources behaves exactly as the
    landed refactor (all-enabled), even for a system-plugin row."""
    with seated_plugin(_capable_plugin()):
        registry = Registry.empty()
        _publish_capability(registry, "vm-platform", "fixture-platform")
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        registry.finalize()  # no sources -> all enabled
        assert registry.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.enabled
        assert registry.graph.is_ready("vm-site", "s")


# -- vm-platform through vm-site ------------------------------------------------


def test_vm_site_on_disabled_plugin_platform_is_not_ready_with_enable_plugin() -> None:
    with seated_plugin(_capable_plugin()):
        registry = Registry.empty()
        _publish_capability(registry, "vm-platform", "fixture-platform")
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        registry.finalize(enablement_sources=[_plugin_source()])

        verdict = registry.graph.readiness_of("vm-site", "s")
        # The existing fold produces this, via the carried mark reason: NOT an
        # unknown-name hard error (the platform row is present, just disabled).
        assert verdict.reason == (
            "depends on vm-platform 'fixture-platform', which is disabled; enable plugin `cap-plugin`"
        )


def test_disabled_plugin_platform_withholds_its_config_implied_secret() -> None:
    """R12 under the REAL producer: a vm-site on a disabled plugin platform is
    not-ready, so ``has_ready_referrer`` withholds the secret its platform_config
    implies; opting the plugin in makes the site ready and the secret
    materializes. This closes the acceptance item through ``plugin_enablement_source``,
    not only the stub source in test_readiness_fold.py."""

    def _build(*, enabled: bool) -> Registry:
        registry = Registry.empty()
        _publish_capability(registry, "vm-platform", "fixture-platform")
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(
                name="s",
                platform=CapabilityBlock.of("fixture-platform", **{"token_secret": "fixture-token"}),
            ),
            _operator(),
        )
        registry.finalize(enablement_sources=[_plugin_source(*([PLUGIN] if enabled else []))])
        return registry

    with seated_plugin(_capable_plugin()):
        disabled = _build(enabled=False)
        assert not disabled.graph.is_ready("vm-site", "s")  # platform disabled -> site not-ready
        assert not _present(disabled, "secret", "fixture-token")  # withheld: no ready referrer

        enabled = _build(enabled=True)
        assert enabled.graph.is_ready("vm-site", "s")
        assert _present(enabled, "secret", "fixture-token")  # materializes when opted in

    with seated_plugin(_capable_plugin()):
        registry = Registry.empty()
        _publish_capability(registry, "vm-platform", "fixture-platform")
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        registry.finalize(enablement_sources=[_plugin_source()])

        with pytest.raises(StateError, match="enable plugin `cap-plugin`"):
            resolve_site("s", registry)


# -- secret-backend -------------------------------------------------------------


# That a disabled plugin's backend is dropped from the resolution chain is
# ``test_a_valid_mapping_to_a_disabled_backend_builds_and_stays_inert``
# below, which makes the same two assertions (the node is disabled, and the
# chain comes back as ``["prompt"]`` alone) over a registry that also
# carries a well-formed mapping to the dormant backend, so it pins the
# harder half of the same seam.


def _registry_mapping_fixture_backend(mapping: MappingValue, *, publish_source: bool = True) -> Registry:
    """A registry with one secret mapping ``fixture-source``, unfinalized.

    ``publish_source=False`` omits the configured source row, producing the
    source-first dangling-key case.
    """
    registry = Registry.empty()
    if publish_source:
        _publish_capability(registry, "secret-backend", "fixture-backend")
        registry.add(
            "secret-source",
            "fixture-source",
            SecretSourceDecl(name="fixture-source", backend=CapabilityBlock.of("fixture-backend")),
            _operator("sources.yaml"),
        )
    registry.add(
        "secret",
        "vaulted",
        SecretDecl(name="vaulted", description="a vaulted key", backend_mappings={"fixture-source": mapping}),
        _operator("c.toml"),
    )
    return registry


def test_mapping_to_a_disabled_plugin_backend_is_validated_like_any_other() -> None:
    """A mapping addressed to a PRESENT-but-DISABLED backend is validated
    exactly as one addressed to an enabled backend.

    The invariant is that the verdict does not move when enablement does:
    ``"bad"`` is not in the fixture backend's vocabulary, so it is refused on
    both branches. Validity is the model's answer, and an operator must not be
    able to bank a mapping no model would accept and have it detonate at the
    moment they enable the backend.

    The backend row is PUBLISHED on both branches (only the plugin opt-in
    moves), so neither branch can pass through the absent-backend path below,
    which would raise a different error for a different reason. Each branch
    also PROVES which side of the axis it is on rather than assuming the
    opt-in plumbing fired: it finalizes the same shape with a valid mapping
    first and reads the axis off the graph. Without that, a source that
    silently stopped disabling would leave this test green for the wrong
    reason.

    The two branches run in one body because "the verdict does not move" is
    a claim ABOUT the pair, and reading it as one test is what says so.
    """
    for enabled, expected in ((False, Enablement.disabled), (True, Enablement.enabled)):
        sources = [_plugin_source(*([PLUGIN] if enabled else []))]
        with seated_plugin(_capable_plugin()):
            precondition = _registry_mapping_fixture_backend("good")
            precondition.finalize(enablement_sources=sources)
            assert precondition.graph.enablement_of("secret-backend", "fixture-backend") is expected

            registry = _registry_mapping_fixture_backend("bad")
            with pytest.raises(ConfigError, match="backend_mappings.fixture-source: must be one of"):
                registry.finalize(enablement_sources=sources)


def test_a_valid_mapping_to_a_disabled_backend_builds_and_stays_inert() -> None:
    """The other half of the same seam: validating a disabled backend's mapping
    does NOT make that backend live.

    A WELL-FORMED mapping to a disabled backend builds (validation had nothing
    to complain about), and the backend is still dropped from the resolution
    chain, so the mapping is never selected or resolved through. Being
    validated and being live are separate properties, and only the second one
    tracks enablement.
    """
    with seated_plugin(_capable_plugin()):
        registry = _registry_mapping_fixture_backend("good")
        _publish_builtin_backend(registry, "prompt")
        registry.add(
            "secret-source",
            "prompt",
            SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
            Origin.built_in(source="agentworks.secrets.sources"),
        )
        registry.finalize(enablement_sources=[_plugin_source()])  # PLUGIN not opted in

        assert registry.graph.enablement_of("secret-backend", "fixture-backend") is Enablement.disabled
        config = cast(
            "Config",
            SimpleNamespace(secret_config_data=SimpleNamespace(sources=("fixture-source", "prompt"))),
        )
        sources = active_sources(config, registry)
        assert [source.name for source in sources] == ["fixture-source", "prompt"]
        assert not sources[0].readiness.is_ready


def test_mapping_to_an_absent_backend_reports_the_dangling_edge_not_a_shape_error() -> None:
    """An ABSENT backend is a different case from a disabled one and keeps its
    own answer.

    No row and no seated impl means no declared model, so there is nothing the
    mapping could be checked against; ``validate_capability_config`` no-ops.
    The mapping is not silently accepted, though: the secret's dangling
    ``secret-backend`` edge reports it once as a hard finalize miss (R9.11),
    in the "no such backend" vocabulary rather than a shape complaint about a
    model that does not exist here. The mapping used is one the fixture
    backend's model would REJECT, so a shape error would surface if the absent
    path had been folded into the validated one.
    """
    registry = _registry_mapping_fixture_backend("bad", publish_source=False)
    with pytest.raises(ConfigError, match="references unknown secret-source 'fixture-source'") as exc:
        registry.finalize(enablement_sources=[_plugin_source()])
    assert "must be one of" not in str(exc.value)


# -- git-credential-provider ----------------------------------------------------


def _git_registry() -> Registry:
    registry = Registry.empty()
    _publish_capability(registry, "git-credential-provider", "fixture-provider")
    registry.add(
        "git-credential",
        "cred",
        GitCredentialConfig(name="cred", provider=CapabilityBlock.of("fixture-provider", **{})),
        _operator(),
    )
    return registry


def test_git_credential_on_disabled_plugin_provider_is_not_ready() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _git_registry()
        registry.finalize(enablement_sources=[_plugin_source()])

        verdict = registry.graph.readiness_of("git-credential", "cred")
        assert verdict.reason == (
            "depends on git-credential-provider 'fixture-provider', which is disabled; enable plugin `cap-plugin`"
        )


def test_git_credential_not_ready_falls_back_when_mark_absent() -> None:
    """The propagate hook's mark-absent fallback (mirrors the vm-site leaf test):
    a disabled provider ``DependencyState`` with no carried reason yields the
    generic "enable its unit" tail. Direct call, no source involved."""
    cred = GitCredentialConfig(name="c", provider=CapabilityBlock.of("p", **{}))
    deps = {
        ("git-credential-provider", "p"): DependencyState(
            enablement=Enablement.disabled,
            readiness=None,  # None iff disabled
            impl=None,
        )  # disabled_reason defaults to None
    }
    verdict = cred.not_ready(deps)
    assert verdict.reason == "depends on git-credential-provider 'p', which is disabled; enable its unit"


def test_git_credential_ready_when_provider_enabled() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _git_registry()
        registry.finalize(enablement_sources=[_plugin_source(PLUGIN)])
        assert registry.graph.is_ready("git-credential", "cred")


def test_resolve_git_credential_providers_refuses_a_disabled_provider() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _git_registry()
        registry.finalize(enablement_sources=[_plugin_source()])
        with pytest.raises(StateError, match="enable plugin `cap-plugin`"):
            resolve_git_credential_providers(registry, ["cred"])


def test_remote_advisories_skips_a_disabled_git_credential() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _git_registry()
        registry.finalize(enablement_sources=[_plugin_source()])
        # No crash constructing the disabled provider, and no advisories from it.
        assert remote_advisories(registry, "https://github.com/acme/repo.git") == []


# -- harness integration (the secret model: lists ready, gated at use) --------


def _harness_integration_registry() -> Registry:
    registry = Registry.empty()
    _publish_capability(registry, "harness-integration", "fixture-harness")
    registry.add(
        "session-template",
        "tmpl",
        SessionTemplate(name="tmpl", harness_integration=CapabilityBlock(name="fixture-harness")),
        _operator(),
    )
    return registry


def test_session_template_on_disabled_plugin_harness_integration_lists_ready() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _harness_integration_registry()
        registry.finalize(enablement_sources=[_plugin_source()])
        # The template does NOT propagate the harness integration's disabled state.
        assert registry.graph.is_ready("session-template", "tmpl")
        assert registry.graph.enablement_of("harness-integration", "fixture-harness") is Enablement.disabled


def test_ensure_harness_integration_enabled_raises_for_a_disabled_plugin_integration() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _harness_integration_registry()
        registry.finalize(enablement_sources=[_plugin_source()])
        with pytest.raises(StateError, match="enable plugin `cap-plugin`"):
            ensure_harness_integration_enabled(registry, "fixture-harness")


def test_ensure_harness_integration_enabled_passes_when_enabled() -> None:
    with seated_plugin(_capable_plugin()):
        registry = _harness_integration_registry()
        registry.finalize(enablement_sources=[_plugin_source(PLUGIN)])
        ensure_harness_integration_enabled(registry, "fixture-harness")  # no raise


def test_display_harness_integration_still_shows_a_disabled_plugin_integration_name() -> None:
    """The read-only listing path is deliberately UNGATED (R14): an enabled
    session-template referencing a disabled harness integration still shows the name."""
    with seated_plugin(_capable_plugin()):
        registry = _harness_integration_registry()
        registry.finalize(enablement_sources=[_plugin_source()])
        assert _display_harness_integration(registry, "tmpl") == "fixture-harness"


# -- R13 multi-source seam ------------------------------------------------------


def _stub_source(key: tuple[str, str], reason: str) -> EnablementSource:
    def _source(resources: Mapping[str, Mapping[str, object]]) -> dict[tuple[str, str], DisabledMark]:
        return {key: DisabledMark(reason=reason, source="stub")}

    return _source


def test_compose_enablement_unions_sources_and_first_source_wins_the_reason() -> None:
    a = _stub_source(("vm-platform", "x"), "from-a")
    b = _stub_source(("vm-platform", "x"), "from-b")
    c = _stub_source(("harness-integration", "y"), "from-c")
    # Union across sources.
    marks = compose_enablement([a, c], {})
    assert set(marks) == {("vm-platform", "x"), ("harness-integration", "y")}
    # First source in the list wins the reason when two disable the same node.
    assert compose_enablement([a, b], {})[("vm-platform", "x")].reason == "from-a"
    assert compose_enablement([b, a], {})[("vm-platform", "x")].reason == "from-b"


def test_second_stub_source_composes_through_finalize_and_precedence_holds() -> None:
    with seated_plugin(_capable_plugin()):
        registry = Registry.empty()
        _publish_capability(registry, "vm-platform", "fixture-platform")
        _publish_capability(registry, "harness-integration", "fixture-harness")
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        # The plugin source disables both plugin rows. A stub ALSO disables the
        # platform (a shared node, testing precedence) AND is the only source
        # that would disable the harness integration were the plugin opted in; here it stays
        # a union check across two present nodes end to end.
        stub_platform = _stub_source(("vm-platform", "fixture-platform"), "stub-reason")
        stub_harness_integration = _stub_source(("harness-integration", "fixture-harness"), "stub-harness-reason")
        registry.finalize(enablement_sources=[_plugin_source(), stub_platform, stub_harness_integration])

        # Union across sources: both present nodes are disabled through finalize.
        assert registry.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.disabled
        assert registry.graph.enablement_of("harness-integration", "fixture-harness") is Enablement.disabled
        # Precedence: the plugin source (first in the list) wins the shared
        # platform's reason over the stub.
        verdict = registry.graph.readiness_of("vm-site", "s")
        assert verdict.reason is not None
        assert "enable plugin `cap-plugin`" in verdict.reason  # plugin source (first) won
        assert "stub-reason" not in verdict.reason
