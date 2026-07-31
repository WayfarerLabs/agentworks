"""Manifest present-but-disabled parity + the declarable-reference use-gate
(Phase 7, LLD b's reference side + LLD c 3b).

A not-enabled plugin's bundled DECLARABLE rows are published (weak), disabled by
the same overlay that disables its capability rows, hidden from the default
``list`` but shown by ``describe``, and REFUSED AT USE with the enable hint
(never an unknown-name error, never a silent use). Enabling the plugin makes
them consumable. Driven by a manifest-only fixture plugin (empty capabilities,
so no seating needed) injected via ``SYSTEM_PLUGINS``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.capabilities.harness import ensure_harness_enabled
from agentworks.capabilities.harness.base import Harness
from agentworks.errors import ConfigError, StateError
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.plugins import Plugin, plugin_enablement_source, publish_plugins, seated_plugin
from agentworks.resources.access import ensure_recipe_enabled, ensure_reference_enabled
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import describe_resource, list_resources
from agentworks.resources.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.sessions.template import SessionTemplate

if TYPE_CHECKING:
    from agentworks.config import Config

PLUGIN = "decl-plugin"
_DECLARABLE_ANCHOR = f"{__package__}._manifest_declarable_fixture"
_RESERVED_ANCHOR = f"{__package__}._manifest_reserved_fixture"
_EXCLUDED_ANCHOR = f"{__package__}._manifest_excluded_fixture"


def _plugin(name: str = PLUGIN, *, anchor: str = _DECLARABLE_ANCHOR) -> Plugin:
    return Plugin(name=name, description="a manifest-parity fixture plugin", manifests=anchor)


def _config(*enabled: str) -> Config:
    return cast("Config", SimpleNamespace(enabled_system_plugins=tuple(enabled)))


def _operator() -> Origin:
    return Origin.operator_declared(file=Path("op.yaml"), line=1)


def _present(registry: Registry, kind: str, name: str) -> bool:
    return any(n == name for n, _ in registry.iter_kind_items(kind))


def _build(monkeypatch: pytest.MonkeyPatch, *enabled: str, operator_rows: bool = False) -> Registry:
    """Publish the fixture plugin's manifests (weak when not enabled) plus,
    optionally, an operator agent-template referencing the fixture's
    user-install-command, then finalize with the plugin source."""
    plugin = _plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config(*enabled)
    registry = Registry.empty()
    publish_plugins(registry, config)
    if operator_rows:
        registry.add(
            "agent-template",
            "op-tmpl",
            AgentTemplate(name="op-tmpl", user_install_commands=["fixture-user-cmd"]),
            _operator(),
        )
    registry.finalize(enablement_sources=[plugin_enablement_source(config)])
    return registry


# -- Present-but-disabled when not enabled; enabled + consumable when enabled ----


def test_manifest_rows_present_but_disabled_when_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch)  # not enabled
    for kind, name in [
        ("user-install-command", "fixture-user-cmd"),
        ("system-install-command", "fixture-sys-cmd"),
        ("agent-template", "fixture-agent-tmpl"),
    ]:
        assert _present(registry, kind, name), f"{kind}/{name} should be present"
        assert registry.graph.enablement_of(kind, name) is Enablement.disabled
        origin = registry.lookup(kind, name).origin
        assert origin.variant == "system-plugin"
        assert origin.plugin == PLUGIN


def test_manifest_rows_enabled_when_plugin_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch, PLUGIN)  # opted in
    assert registry.graph.enablement_of("user-install-command", "fixture-user-cmd") is Enablement.enabled
    assert registry.graph.enablement_of("agent-template", "fixture-agent-tmpl") is Enablement.enabled


# -- Disabled hides from list, shows by describe with the Disabled line ----------


def test_disabled_manifest_hidden_from_list_shown_with_include_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch)  # not enabled

    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("user-install-command", "fixture-user-cmd") not in default_rows

    shown = list_resources(registry, include_disabled=True)
    by_key = {(r.kind, r.name): r for r in shown.rows}
    assert ("user-install-command", "fixture-user-cmd") in by_key


def test_describe_renders_disabled_row_with_plugin_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch)  # not enabled
    desc = describe_resource(registry, "user-install-command", "fixture-user-cmd")
    assert desc.disabled_reason is not None
    assert PLUGIN in desc.disabled_reason


# -- The reference-side use-gate helpers -----------------------------------------


def test_ensure_reference_enabled_refuses_disabled_row_with_enable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch)  # not enabled
    with pytest.raises(StateError) as exc:
        ensure_reference_enabled(registry, "user-install-command", "fixture-user-cmd")
    message = str(exc.value)
    assert "user-install-command" in message
    assert "fixture-user-cmd" in message
    assert f"enable plugin `{PLUGIN}`" in message


def test_ensure_recipe_enabled_refuses_disabled_contribution_in_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An ENABLED operator agent-template that references the DISABLED plugin's
    # user-install-command is refused by the recipe gate, naming the disabled
    # contribution in the closure (not the operator's own enabled template).
    registry = _build(monkeypatch, operator_rows=True)  # plugin not enabled
    with pytest.raises(StateError) as exc:
        ensure_recipe_enabled(registry, "agent-template", "op-tmpl")
    message = str(exc.value)
    assert "fixture-user-cmd" in message
    assert f"enable plugin `{PLUGIN}`" in message


def test_ensure_recipe_enabled_is_noop_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _build(monkeypatch, PLUGIN, operator_rows=True)  # plugin enabled
    ensure_recipe_enabled(registry, "agent-template", "op-tmpl")  # no raise
    ensure_recipe_enabled(registry, "agent-template", "fixture-agent-tmpl")  # no raise


def test_ensure_recipe_enabled_is_noop_for_implicit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing start node (an implicit default template) is a safe no-op:
    # enablement_of tolerates it and reachable_from returns empty.
    registry = _build(monkeypatch)
    ensure_recipe_enabled(registry, "agent-template", "no-such-template")  # no raise


class _FixtureHarness(Harness):
    name = "fixture-harness"
    description = "Fixture harness (manifest-parity capability-exclusion test)"


def test_ensure_recipe_enabled_excludes_capability_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A CAPABILITY node in the closure keeps its own R14 model and is NOT
    # refused by the recipe gate: an ENABLED operator session-template whose
    # harness edge lands on a DISABLED plugin harness passes ensure_recipe_enabled
    # (the harness is a capability, excluded from the closure check), while
    # ensure_harness_enabled WOULD refuse it. This proves the exclusion, not just
    # a named-declarable refusal.
    plugin = Plugin(
        name=PLUGIN,
        description="a capability fixture plugin",
        capabilities={"harness": (_FixtureHarness,)},
    )
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config()  # harness NOT enabled -> disabled capability row
    with seated_plugin(plugin):
        registry = Registry.empty()
        publish_plugins(registry, config)
        registry.add(
            "session-template",
            "op-session",
            SessionTemplate(name="op-session", harness="fixture-harness"),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])

        # The disabled harness IS in the enabled template's closure...
        assert ("harness", "fixture-harness") in registry.graph.reachable_from("session-template", "op-session")
        # ...but the recipe gate does NOT refuse on it (capability exclusion).
        ensure_recipe_enabled(registry, "session-template", "op-session")
        # The harness keeps its own R14 use-gate, which WOULD refuse it.
        with pytest.raises(StateError, match=f"enable plugin `{PLUGIN}`"):
            ensure_harness_enabled(registry, "fixture-harness")


# -- Operator resource wins over a disabled plugin manifest, both orders ---------


def test_operator_row_wins_over_disabled_plugin_manifest_both_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    config = _config()  # plugin NOT enabled -> its manifest rows are weak/disabled

    def _op_cmd() -> UserInstallCommandEntry:
        return UserInstallCommandEntry(name="fixture-user-cmd", description="operator", command="echo op")

    # Order A (operator first, then the weak plugin row via publish_plugins):
    reg_a = Registry.empty()
    reg_a.add("user-install-command", "fixture-user-cmd", _op_cmd(), _operator())
    publish_plugins(reg_a, config)  # the plugin's weak row must NOT displace / error
    assert reg_a.lookup("user-install-command", "fixture-user-cmd").origin.variant == "operator-declared"

    # Order B (plugin first via publish_plugins, then the operator row):
    reg_b = Registry.empty()
    publish_plugins(reg_b, config)
    reg_b.add("user-install-command", "fixture-user-cmd", _op_cmd(), _operator())
    assert reg_b.lookup("user-install-command", "fixture-user-cmd").origin.variant == "operator-declared"


# -- Reserved-name and unbundleable-kind bundles are rejected at publish ---------


def test_reserved_name_bundle_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin("reserved-plugin", anchor=_RESERVED_ANCHOR)
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    registry = Registry.empty()
    with pytest.raises(ConfigError) as exc:
        publish_plugins(registry, _config("reserved-plugin"))  # even enabled, rejected
    message = str(exc.value)
    assert "reserved-plugin" in message  # plugin-attributed
    assert "agent-template" in message
    assert "default" in message


def test_unbundleable_kind_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin("excluded-plugin", anchor=_EXCLUDED_ANCHOR)
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
    registry = Registry.empty()
    with pytest.raises(ConfigError) as exc:
        publish_plugins(registry, _config())  # not enabled: rejection is publish-time, not gated
    message = str(exc.value)
    assert "excluded-plugin" in message  # plugin-attributed
    assert "secret" in message
    assert "secret.yaml" in message


# -- The enable-every-shipped-plugin curation fixture ----------------------------


def test_enable_every_shipped_plugin_finalizes_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two distinct fixtures, both enabled, must publish + finalize cleanly (no
    # curation collision), pinning the shipped-set curation the CI fixture
    # guards. Distinct plugins on distinct names do not collide.
    a = _plugin("plugin-a")
    b = _plugin("plugin-b", anchor=f"{__package__}._manifest_fixture")  # an apt-source fixture
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {a.name: a, b.name: b})
    config = _config("plugin-a", "plugin-b")
    registry = Registry.empty()
    publish_plugins(registry, config)
    registry.finalize(enablement_sources=[plugin_enablement_source(config)])
    assert registry.graph.enablement_of("agent-template", "fixture-agent-tmpl") is Enablement.enabled
    assert registry.graph.enablement_of("apt-source", "fixture-apt-source") is Enablement.enabled
