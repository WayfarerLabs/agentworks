"""``publish_plugins`` + the shared manifest loader body (Phase 5, LLD c).

The first phase where a real ``[plugins]`` config changes behavior. These
tests drive ``publish_plugins`` directly (a hand-built ``Registry`` finalized
with the plugin enablement source, the shape ``build_registry`` wires up),
against a FIXTURE plugin injected two ways at once:

- ``seated_plugin(fixture)`` seats its impls into the four capability
  registries, exactly as the installed index does at import (so ``build_row``
  finds a seated occupant), and
- ``monkeypatch``-ing ``SYSTEM_PLUGINS`` to ``{fixture.name: fixture}`` makes
  ``publish_plugins`` iterate the fixture only (replacing the shipped index, so
  the migrated plugins like ``onepassword`` do not interfere).

The fixture impls are REAL capability subclasses so they fold through their
consumers (a ``vm-site`` goes not-ready, a ``session-template`` reaches the
harness integration use-gate); the fixture's bundled manifest is a self-contained
``apt-source`` in the ``_manifest_fixture`` package beside this file.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled, harness_integration_for
from agentworks.errors import ConfigError, StateError
from agentworks.manifests.package import publish_manifest_package
from agentworks.origin import Origin
from agentworks.plugins import Plugin, plugin_enablement_source, publish_plugins, seated_plugin
from agentworks.plugins.registration import _capability_registries
from agentworks.resources.graph import Enablement
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityBlock
from agentworks.sessions.manager._env import _resolve_template
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.sites import VMSiteDecl
from tests.plugins._fixtures import ConformingHarnessIntegration, ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config

PLUGIN = "pub-plugin"

# The manifest fixture package (a `manifests/` subdir of yaml beside this
# test); its bare package name is the importlib-resources anchor a plugin
# stores in ``Plugin.manifests``.
_MANIFEST_ANCHOR = f"{__package__}._manifest_fixture"
_DIRTY_ANCHOR = f"{__package__}._manifest_dirty_fixture"
# A real, importable package that ships NO ``manifests/`` subdir: the anchor
# resolves (so the unimportable-anchor guard never fires) but its curated
# bundle is missing.
_NO_SUBDIR_ANCHOR = f"{__package__}._manifest_no_subdir_fixture"


# -- Real fixture impls (subclasses, so they fold through their consumers, and
#    so registration's conformance check accepts them) ---------------------------


class _FixtureVMPlatform(ConformingVMPlatform):
    name = "fixture-platform"
    description = "Fixture VM platform (plugin publish test)"

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        # Accept the fixture's (empty) config blob; the inherited default
        # rejects any non-empty config.
        return None


class _FixtureHarnessIntegration(ConformingHarnessIntegration):
    name = "fixture-harness"
    description = "Fixture harness (plugin publish test)"


def _fixture_plugin(name: str = PLUGIN, *, with_manifests: bool = True) -> Plugin:
    return Plugin(
        name=name,
        description="a publish-test fixture plugin",
        capabilities={
            "vm-platform": (_FixtureVMPlatform,),
            "harness-integration": (_FixtureHarnessIntegration,),
        },
        manifests=_MANIFEST_ANCHOR if with_manifests else None,
    )


def _config(*enabled: str) -> Config:
    return cast("Config", SimpleNamespace(enabled_system_plugins=tuple(enabled)))


def _operator() -> Origin:
    from pathlib import Path

    return Origin.operator_declared(file=Path("op.yaml"), line=1)


def _present(registry: Registry, kind: str, name: str) -> bool:
    return any(n == name for n, _ in registry.iter_kind_items(kind))


# -- Enabled plugin: capability row + manifest present, enabled, consumable -----


def test_enabled_plugin_publishes_capability_and_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config(PLUGIN)  # opted in
    with seated_plugin(plugin):
        registry = Registry.empty()
        publish_plugins(registry, config)
        # An operator vm-site consumes the plugin's platform at its site.
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])

        # Capability row: present, ENABLED, system-plugin origin.
        assert registry.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.enabled
        platform_origin = registry.lookup("vm-platform", "fixture-platform").origin
        assert platform_origin.variant == "system-plugin"
        assert platform_origin.plugin == PLUGIN
        assert platform_origin.source == f"agentworks.plugins.{PLUGIN}"
        # Consumable at its site: the vm-site referencing it is ready.
        assert registry.graph.is_ready("vm-site", "s")

        # Manifest resource: present, ENABLED, system-plugin origin whose source
        # points at the bundled file.
        assert registry.graph.enablement_of("apt-source", "fixture-apt-source") is Enablement.enabled
        source_origin = registry.lookup("apt-source", "fixture-apt-source").origin
        assert source_origin.variant == "system-plugin"
        assert source_origin.plugin == PLUGIN
        assert source_origin.source == f"{_MANIFEST_ANCHOR}/manifests/fixture-source.yaml"


# -- Not-enabled plugin: row present-but-disabled, manifest absent --------------


def test_not_enabled_plugin_row_and_manifest_present_but_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config()  # NOT opted in
    with seated_plugin(plugin):
        registry = Registry.empty()
        publish_plugins(registry, config)
        registry.add(
            "vm-site",
            "s",
            VMSiteDecl(name="s", platform=CapabilityBlock.of("fixture-platform", **{})),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])

        # The capability row is PRESENT-BUT-DISABLED (published unconditionally,
        # then marked disabled by the enablement source), never absent.
        assert _present(registry, "vm-platform", "fixture-platform")
        assert registry.graph.enablement_of("vm-platform", "fixture-platform") is Enablement.disabled
        # A reference to it is not-ready with the enable hint, NOT an
        # unknown-name hard error.
        verdict = registry.graph.readiness_of("vm-site", "s")
        assert verdict.reason == (
            "depends on vm-platform 'fixture-platform', which is disabled; enable plugin `pub-plugin`"
        )
        # Its manifest resources are now PRESENT-BUT-DISABLED too (Phase 7
        # parity): published weak, disabled by the same overlay, never absent.
        assert _present(registry, "apt-source", "fixture-apt-source")
        assert registry.graph.enablement_of("apt-source", "fixture-apt-source") is Enablement.disabled


# -- Unknown enabled name: typed ConfigError before any publish -----------------


def test_unknown_enabled_name_raises_config_error_before_any_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    with seated_plugin(plugin):
        registry = Registry.empty()
        # Two unknowns plus the real one: the error lists ALL unknowns and is
        # raised (a single typed ConfigError, never a KeyError) before any add.
        with pytest.raises(ConfigError) as exc:
            publish_plugins(registry, _config("nope", PLUGIN, "also-nope"))
        message = str(exc.value)
        assert "nope" in message
        assert "also-nope" in message
        # Nothing was published: not even the shipped plugin's rows, since the
        # resolution precedes every registry.add.
        assert list(registry.iter_kind_items("vm-platform")) == []
        assert list(registry.iter_kind_items("harness-integration")) == []


# -- build_registry purity: publish mutates no module-level state ---------------


def test_publish_plugins_mutates_no_capability_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The impls are seated at IMPORT (by the installed index / here by
    ``seated_plugin``), never by ``publish_plugins``. Publishing reads them
    through ``build_row`` and mutates no module-level registry, which is what
    keeps ``build_registry`` a pure function."""
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    with seated_plugin(plugin):
        before = [dict(registry) for registry in _capability_registries()]
        registry = Registry.empty()
        publish_plugins(registry, _config(PLUGIN))
        after = [dict(registry) for registry in _capability_registries()]
        assert before == after


# -- The shared manifest loader body's typed-error path -------------------------


def test_dirty_bundle_raises_config_error_not_assertion() -> None:
    """A bundled manifest with warn-level issues raises a typed ``ConfigError``,
    never an ``AssertionError`` (the pre-Phase-5 ``assert`` that ``python -O``
    would strip). ``pytest.raises(ConfigError)`` alone proves it is not the
    stripped assert, since an ``AssertionError`` would not match."""
    registry = Registry.empty()
    with pytest.raises(ConfigError):
        publish_manifest_package(
            registry,
            anchor=_DIRTY_ANCHOR,
            subdir="manifests",
            origin_for=lambda file_name: Origin.built_in(source=f"dirty/{file_name}"),
        )


def test_bad_plugin_manifest_anchor_raises_typed_plugin_attributed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin whose ``manifests`` anchor does not resolve to an importable
    package yields a typed, plugin-attributed ``ConfigError`` (not a raw
    ``ModuleNotFoundError``/``ImportError``). ``register_plugin`` never
    validates ``manifests``, so the publish step is the only place a bad anchor
    is resolved, and it must fail like every other plugin-curation bug."""
    plugin = Plugin(
        name=PLUGIN,
        description="a plugin with a bogus manifest anchor",
        capabilities={"harness-integration": (_FixtureHarnessIntegration,)},
        manifests=f"{__package__}._does_not_exist",
    )
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config(PLUGIN)  # opted in, so its manifests are loaded
    with seated_plugin(plugin):
        registry = Registry.empty()
        with pytest.raises(ConfigError) as exc:
            publish_plugins(registry, config)
        message = str(exc.value)
        assert f"plugin '{PLUGIN}'" in message  # plugin-attributed
        assert "could not be resolved" in message
        # A bare import failure is NOT what escapes.
        assert not isinstance(exc.value, ImportError)


def test_plugin_manifest_anchor_without_subdir_raises_typed_plugin_attributed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin declaring ``manifests=<anchor>`` where the anchor imports fine but
    ships NO ``manifests/`` subdir must fail LOUDLY, not silently publish nothing.
    Before the guard, ``load_manifests`` ran on the non-existent directory and the
    plugin's curated bundle vanished with no error. Now it is a typed,
    plugin-attributed ``ConfigError`` naming the missing subdir, exactly like the
    bad-anchor case above.
    """
    plugin = Plugin(
        name=PLUGIN,
        description="a plugin whose manifest anchor ships no manifests/ subdir",
        capabilities={"harness-integration": (_FixtureHarnessIntegration,)},
        manifests=_NO_SUBDIR_ANCHOR,
    )
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config(PLUGIN)  # opted in, so its manifests are loaded
    with seated_plugin(plugin):
        registry = Registry.empty()
        with pytest.raises(ConfigError) as exc:
            publish_plugins(registry, config)
        message = str(exc.value)
        assert f"plugin '{PLUGIN}'" in message  # plugin-attributed
        assert "ships no 'manifests'" in message  # names the missing subdir
        assert "no-op" not in message  # it is a loud failure, not a silent skip


def test_plugin_path_gates_missing_subdir_but_builtin_path_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing-subdir guard is the PLUGIN path only (``allowed_kinds is not
    None``): a direct plugin-style call on the no-subdir anchor raises, while the
    built-in-style call (``allowed_kinds=None``, whose ``builtin/`` subdir always
    ships) keeps its unchanged behavior. Pins that the gate keys off the caller,
    not the anchor.
    """
    from agentworks.plugins.publish import PLUGIN_MANIFEST_KINDS

    registry = Registry.empty()
    with pytest.raises(ConfigError):
        publish_manifest_package(
            registry,
            anchor=_NO_SUBDIR_ANCHOR,
            subdir="manifests",
            origin_for=lambda file_name: Origin.built_in(source=f"nosub/{file_name}"),
            allowed_kinds=PLUGIN_MANIFEST_KINDS,
        )

    # The built-in path (``allowed_kinds=None``) is ungated: the same missing
    # subdir does NOT raise, it loads nothing (a built-in's ``builtin/`` subdir
    # always ships, so this path never actually hits a missing dir in practice).
    publish_manifest_package(
        Registry.empty(),
        anchor=_NO_SUBDIR_ANCHOR,
        subdir="manifests",
        origin_for=lambda file_name: Origin.built_in(source=f"nosub/{file_name}"),
        allowed_kinds=None,
    )


# -- Phase 4 forward item: a disabled plugin's integration reaches the use-gate --


def test_disabled_plugin_harness_integration_reaches_use_gate_not_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seating is unconditional at import, so a DISABLED plugin's integration impl is
    still seated: ``harness_integration_for`` finds it and ``_resolve_template`` resolves the
    template to it (no unknown-integration error), leaving the use-gate
    (``ensure_harness_integration_enabled``) as the thing that refuses it with the
    enable-plugin error. This pins the LLD section 3 seating requirement."""
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config()  # not opted in, so the harness integration row is disabled
    with seated_plugin(plugin):
        registry = Registry.empty()
        publish_plugins(registry, config)
        registry.add(
            "session-template",
            "tmpl",
            SessionTemplate(name="tmpl", harness_integration=CapabilityBlock(name="fixture-harness")),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])

        # Seated impl found (no unknown-integration ConfigError from harness_integration_for).
        assert harness_integration_for("fixture-harness") is _FixtureHarnessIntegration
        # Template resolution reaches the seated (disabled) harness_integration.
        resolved = _resolve_template(registry, "tmpl")
        assert resolved.harness_integration == "fixture-harness"
        # The use-gate is what refuses it, naming the plugin to enable.
        with pytest.raises(StateError):
            ensure_harness_integration_enabled(registry, resolved.harness_integration)
