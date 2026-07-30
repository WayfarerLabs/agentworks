"""Plugin surfaces (Phase 6, LLD c sections 6 and 7): the disabled-hides /
not-ready-shows default rule, the ``system-plugin`` provenance annotation, the
``describe`` disabled-row rendering, and the doctor plugin roster.

These drive the service layer directly against a FIXTURE plugin injected the
same two ways Phase 5 pins (``seated_plugin`` seats the impls; a monkeypatched
``SYSTEM_PLUGINS`` makes ``publish_plugins`` and the roster iterate the
fixture only, replacing the shipped index so the migrated plugins like
``onepassword`` do not interfere). The roster tests need no
seating: ``_check_plugins`` reads only ``SYSTEM_PLUGINS`` and config, never the
capability registries (a plugin is an origin, not a resource kind, R12).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import ScopeLevel
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.doctor import Status, _check_plugins
from agentworks.plugins import Plugin, PluginCommand, plugin_enablement_source, publish_plugins, seated_plugin
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import (
    describe_resource,
    list_resources,
    render_resource_description,
    render_resource_table,
)
from agentworks.resources.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.vms.sites import VMSiteDecl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.config import Config

# The manifest fixture package reused from the Phase 5 publish tests: a
# self-contained ``apt-source`` under a ``manifests/`` subdir beside this file.
_MANIFEST_ANCHOR = f"{__package__}._manifest_fixture"


# -- Real fixture impls (subclasses, so they fold through their consumers) ------


class _ReadyPlatform(VMPlatform):
    name = "alpha-platform"
    description = "A ready plugin platform"

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        return None


class _NotReadyPlatform(VMPlatform):
    name = "alpha-notready-platform"
    description = "An enabled-but-not-ready plugin platform"

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        return None

    @classmethod
    def unsupported_reason(cls) -> str | None:
        return "unsupported on this host (fixture)"


class _BetaPlatform(VMPlatform):
    name = "beta-platform"
    description = "A disabled plugin platform"

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        return None


def _alpha() -> Plugin:
    """The opted-in plugin: a ready platform, a not-ready-but-enabled platform,
    and a bundled manifest."""
    return Plugin(
        name="alpha",
        description="the opted-in fixture plugin",
        capabilities={"vm-platform": (_ReadyPlatform, _NotReadyPlatform)},
        manifests=_MANIFEST_ANCHOR,
    )


def _beta() -> Plugin:
    """The not-opted-in plugin: its rows publish but land disabled."""
    return Plugin(
        name="beta",
        description="the not-opted-in fixture plugin",
        capabilities={"vm-platform": (_BetaPlatform,)},
    )


def _config(*enabled: str) -> Config:
    return cast("Config", SimpleNamespace(plugins_enabled=tuple(enabled)))


def _operator() -> Origin:
    from pathlib import Path

    return Origin.operator_declared(file=Path("op.yaml"), line=1)


def _seat_and_publish(monkeypatch: pytest.MonkeyPatch, config: Config) -> Registry:
    """Seat alpha + beta, publish them, and finalize with the plugin enablement
    source, exactly as ``build_registry`` wires it. Returns the finalized
    registry, with an operator ``vm-site`` consuming alpha's ready platform."""
    alpha, beta = _alpha(), _beta()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {alpha.name: alpha, beta.name: beta})
    # The rows and the finalized graph are self-contained once built (build_row
    # bakes each impl's description in; the readiness fold runs during finalize
    # while impls are seated), so reads after the context tears seating down
    # touch only frozen state.
    with seated_plugin(alpha), seated_plugin(beta):
        registry = Registry.empty()
        publish_plugins(registry, config)
        registry.add(
            "vm-site",
            "alpha-site",
            VMSiteDecl(name="alpha-site", platform="alpha-platform", platform_config={}),
            _operator(),
        )
        registry.finalize(enablement_sources=[plugin_enablement_source(config)])
    return registry


# -- Disabled hides, not-ready shows (list) -------------------------------------


def test_list_hides_disabled_but_shows_not_ready_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default surface: a disabled plugin row is hidden, while an
    enabled-but-not-ready row still lists. The filter is on the ENABLEMENT
    axis, never readiness."""
    config = _config("alpha")  # alpha opted in, beta not
    registry = _seat_and_publish(monkeypatch, config)

    listing = list_resources(registry)
    names = {(r.kind, r.name) for r in listing.rows}

    # alpha's ready + not-ready platforms are ENABLED, so both list.
    assert ("vm-platform", "alpha-platform") in names
    assert ("vm-platform", "alpha-notready-platform") in names
    # The not-ready-but-enabled row survives, marked not-ready.
    notready = next(r for r in listing.rows if r.name == "alpha-notready-platform")
    assert notready.not_ready_reason is not None
    assert "unsupported on this host (fixture)" in notready.not_ready_reason
    # beta's platform is DISABLED (beta not opted in), so it is hidden.
    assert ("vm-platform", "beta-platform") not in names
    # The post-filter summary counts only the visible plugin rows: alpha's two
    # platforms plus its bundled apt-source manifest; beta is hidden.
    assert listing.plugin_count == 3


def test_include_disabled_reveals_disabled_rows_with_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config("alpha")
    registry = _seat_and_publish(monkeypatch, config)

    listing = list_resources(registry, include_disabled=True)
    names = {(r.kind, r.name) for r in listing.rows}
    assert ("vm-platform", "beta-platform") in names
    # alpha's two platforms + its apt-source manifest + beta's now-revealed row.
    assert listing.plugin_count == 4


def test_list_renders_from_plugin_provenance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config("alpha")
    registry = _seat_and_publish(monkeypatch, config)

    listing = list_resources(registry, kinds=("vm-platform",))
    render_resource_table(listing)
    out = capsys.readouterr().out
    # The DESCRIPTION cell attributes the row to its plugin.
    assert "from plugin alpha" in out


# -- describe of a disabled row (explicit lookup, always renders) ---------------


def test_describe_disabled_row_renders_with_disabled_line_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("alpha")  # beta disabled
    registry = _seat_and_publish(monkeypatch, config)

    desc = describe_resource(registry, "vm-platform", "beta-platform")
    # describe is an explicit lookup: it renders the named row even disabled.
    assert desc.name == "beta-platform"
    # The Disabled line's text is derived from origin + config, exactly as the
    # roster phrases it, NOT from a per-node reason.
    assert desc.disabled_reason == "not enabled in [plugins] (plugin beta)"


def test_describe_disabled_row_render_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config("alpha")
    registry = _seat_and_publish(monkeypatch, config)

    render_resource_description(describe_resource(registry, "vm-platform", "beta-platform"))
    out = capsys.readouterr().out
    assert "Disabled: not enabled in [plugins] (plugin beta)" in out
    assert "from plugin beta" in out


def test_describe_enabled_row_has_no_disabled_line(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config("alpha")
    registry = _seat_and_publish(monkeypatch, config)

    desc = describe_resource(registry, "vm-platform", "alpha-platform")
    assert desc.disabled_reason is None


# -- The full fixture end-to-end (descriptor -> roster) -------------------------


def test_full_fixture_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pass over the whole path: descriptor -> index (monkeypatched) ->
    registration (seated) -> unconditional publication -> enablement overlay
    (finalize) -> consumption + hidden-when-disabled (list) +
    shown-when-named (describe) + roster."""
    config = _config("alpha")
    registry = _seat_and_publish(monkeypatch, config)

    # Publication: alpha's manifest resource is present and enabled (opted in);
    # its capability rows carry a system-plugin origin.
    assert registry.graph.enablement_of("apt-source", "fixture-apt-source") is Enablement.enabled
    assert registry.lookup("vm-platform", "alpha-platform").origin.variant == "system-plugin"
    # Enablement overlay: beta's row is present-but-disabled.
    assert registry.graph.enablement_of("vm-platform", "beta-platform") is Enablement.disabled
    # Consumption: the vm-site on alpha's ready platform is ready.
    assert registry.graph.is_ready("vm-site", "alpha-site")

    # Hidden-when-disabled (list) and shown-when-named (describe).
    default_names = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("vm-platform", "beta-platform") not in default_names
    assert describe_resource(registry, "vm-platform", "beta-platform").disabled_reason is not None

    # Roster: alpha enabled -> ok, beta disabled -> info.
    group = _check_plugins(config)
    by_name = {c.name: c for c in group.checks}
    assert by_name["plugin alpha"].status is Status.OK
    assert by_name["plugin beta"].status is Status.INFO
    assert "not enabled in [plugins]" in (by_name["plugin beta"].message or "")


# -- The doctor plugin roster (LLD c section 7) ---------------------------------


def test_roster_lists_enabled_ok_and_disabled_info(monkeypatch: pytest.MonkeyPatch) -> None:
    alpha, beta = _alpha(), _beta()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {alpha.name: alpha, beta.name: beta})

    group = _check_plugins(_config("alpha"))
    assert group.name == "System plugins"
    by_name = {c.name: c for c in group.checks}

    assert by_name["plugin alpha"].status is Status.OK
    assert by_name["plugin alpha"].message == "the opted-in fixture plugin"

    assert by_name["plugin beta"].status is Status.INFO
    message = by_name["plugin beta"].message or ""
    assert "disabled (not enabled in [plugins])" in message
    # Roster only: it never enumerates a disabled plugin's contributions.
    assert "beta-platform" not in message


def test_roster_empty_index_renders_present_but_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped empty index renders an empty-but-present group."""
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {})

    group = _check_plugins(_config())
    assert group.name == "System plugins"
    assert len(group.checks) == 1
    assert group.checks[0].status is Status.INFO
    assert group.checks[0].name == "No system plugins installed."


def test_roster_required_scopes_informational_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    scoped = Plugin(
        name="scoped",
        description="a plugin with reserved scopes",
        required_scopes=(ScopeLevel.SYSTEM, ScopeLevel.VM),
    )
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {scoped.name: scoped})

    checks = _check_plugins(_config("scoped")).checks
    scope_lines = [c for c in checks if "least privilege" in c.name]
    assert len(scope_lines) == 1
    assert scope_lines[0].status is Status.INFO
    assert scope_lines[0].message == "system, vm"


def test_roster_required_scopes_inert_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    bare = Plugin(name="bare", description="no reserved scopes")  # required_scopes == ()
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {bare.name: bare})

    checks = _check_plugins(_config("bare")).checks
    assert not any("least privilege" in c.name for c in checks)


# -- Reserved fields are inert (nothing constructs / dispatches a command) ------


def test_reserved_fields_do_not_affect_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """``required_scopes`` and ``commands`` are stored data only: a plugin
    carrying them publishes the exact same capability rows as one without, so
    nothing reads them for publication or dispatch."""

    def _published(plugin: Plugin) -> set[tuple[str, str]]:
        monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {plugin.name: plugin})
        with seated_plugin(plugin):
            registry = Registry.empty()
            publish_plugins(registry, _config(plugin.name))
            return {(kind, name) for kind in registry.iter_kinds() for name, _ in registry.iter_kind_items(kind)}

    bare = Plugin(name="p", description="d", capabilities={"vm-platform": (_ReadyPlatform,)})
    loaded = Plugin(
        name="p",
        description="d",
        capabilities={"vm-platform": (_ReadyPlatform,)},
        required_scopes=(ScopeLevel.SYSTEM,),
        commands=(PluginCommand(name="do-thing"),),
    )
    assert _published(bare) == _published(loaded)
