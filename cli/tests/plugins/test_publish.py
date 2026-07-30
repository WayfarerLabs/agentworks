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
harness use-gate); the fixture's bundled manifest is a self-contained
``apt-source`` in the ``_manifest_fixture`` package beside this file.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.harness import ensure_harness_enabled, harness_for
from agentworks.capabilities.harness.base import Harness
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.errors import ConfigError, StateError
from agentworks.manifests.package import publish_manifest_package
from agentworks.plugins import Plugin, plugin_enablement_source, publish_plugins, seated_plugin
from agentworks.plugins.registration import _capability_registries
from agentworks.resources.graph import Enablement
from agentworks.resources.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.sessions.manager._env import _resolve_template
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.sites import VMSiteDecl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config

PLUGIN = "pub-plugin"

# The manifest fixture package (a `manifests/` subdir of yaml beside this
# test); its bare package name is the importlib-resources anchor a plugin
# stores in ``Plugin.manifests``.
_MANIFEST_ANCHOR = f"{__package__}._manifest_fixture"
_DIRTY_ANCHOR = f"{__package__}._manifest_dirty_fixture"


# -- Real fixture impls (subclasses, so they fold through their consumers) ------


class _FixtureVMPlatform(VMPlatform):
    name = "fixture-platform"
    description = "Fixture VM platform (plugin publish test)"

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        # Accept the fixture's (empty) config blob; the inherited default
        # rejects any non-empty config.
        return None


class _FixtureHarness(Harness):
    name = "fixture-harness"
    description = "Fixture harness (plugin publish test)"


def _fixture_plugin(name: str = PLUGIN, *, with_manifests: bool = True) -> Plugin:
    return Plugin(
        name=name,
        description="a publish-test fixture plugin",
        capabilities={
            "vm-platform": (_FixtureVMPlatform,),
            "harness": (_FixtureHarness,),
        },
        manifests=_MANIFEST_ANCHOR if with_manifests else None,
    )


def _config(*enabled: str) -> Config:
    return cast("Config", SimpleNamespace(plugins_enabled=tuple(enabled)))


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
            VMSiteDecl(name="s", platform="fixture-platform", platform_config={}),
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
        assert source_origin.source == f"agentworks.plugins.{PLUGIN}/manifests/fixture-source.yaml"


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
            VMSiteDecl(name="s", platform="fixture-platform", platform_config={}),
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
        assert list(registry.iter_kind_items("harness")) == []


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
    with pytest.raises(ConfigError, match="issue-free"):
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
        capabilities={"harness": (_FixtureHarness,)},
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


def test_builtin_publish_routes_through_shared_body_preserving_origin() -> None:
    """``builtin.py`` migrated onto the shared body; its rows still land with a
    ``built-in`` origin whose source is ``agentworks.manifests.builtin/<file>``
    (the exact shape it produced before the migration)."""
    from agentworks.manifests import builtin

    registry = Registry.empty()
    builtin.publish_to(registry)
    origin = registry.lookup("apt-source", "github-cli").origin
    assert origin.variant == "built-in"
    assert origin.source == "agentworks.manifests.builtin/apt-sources.yaml"


# -- Phase 4 forward item: a disabled plugin's harness reaches the use-gate ------


def test_disabled_plugin_harness_reaches_use_gate_not_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seating is unconditional at import, so a DISABLED plugin's harness impl is
    still seated: ``harness_for`` finds it and ``_resolve_template`` resolves the
    template to it (no unknown-harness error), leaving the use-gate
    (``ensure_harness_enabled``) as the thing that refuses it with the
    enable-plugin error. This pins the LLD section 3 seating requirement."""
    plugin = _fixture_plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config()  # NOT opted in -> the harness row is disabled
    with seated_plugin(plugin):
        registry = Registry.empty()
        publish_plugins(registry, config)
        registry.add(
            "session-template",
            "tmpl",
            SessionTemplate(name="tmpl", harness="fixture-harness"),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])

        # Seated impl found (no unknown-harness ConfigError from harness_for).
        assert harness_for("fixture-harness") is _FixtureHarness
        # Template resolution reaches the seated (disabled) harness.
        resolved = _resolve_template(registry, "tmpl")
        assert resolved.harness == "fixture-harness"
        # The use-gate is what refuses it, naming the plugin to enable.
        with pytest.raises(StateError, match="enable plugin `pub-plugin`"):
            ensure_harness_enabled(registry, resolved.harness)
