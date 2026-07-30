"""``publish_plugins``: the ``build_registry`` publish step for system
plugins (R5 publication, R9 manifests, Phase 5).

Publication only. Every shipped plugin's capability impls were already
SEATED at import (the installed index called ``register_plugin`` for each,
LLD a), so this step mutates no module-level state and ``build_registry``
stays pure: it reads the seated impls through the capability adapters'
``build_row`` (the whitelisted builder path), stamps a ``system-plugin``
origin, and loads the enabled plugins' bundled manifests through the shared
loader body.

Two deliberate asymmetries the LLD pins:

- **Capability rows publish for every shipped plugin, unconditionally**, not
  gated by ``[plugins]``. Enablement is LLD (b)'s job: its source marks a
  not-opted-in plugin's rows ``disabled`` at finalize, so the row is
  present-but-disabled (a reference to it is not-ready with the enable hint),
  never absent (which would surface as an unknown-name hard error).
- **Manifests publish for enabled plugins only** (R9). A not-opted-in plugin
  offers no resources an operator references by name, so gating publication
  is simpler than publish-then-disable and keeps a not-enabled plugin's
  resources out of collision checks against operator resources.

An unknown enabled name is resolved up front, before any ``registry.add``, as
a single typed ``ConfigError`` (never a ``KeyError``, never the post-finalize
validate block).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from agentworks.errors import ConfigError
from agentworks.manifests.package import publish_manifest_package
from agentworks.plugins.adapters import CAPABILITY_ADAPTERS
from agentworks.resources import Origin

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry


class _NamedImpl(Protocol):
    """The one attribute publication reads off a seated impl class: its
    registry key ``name`` (a non-empty, ``/``-free ``str``, guaranteed by
    ``register_plugin``'s descriptor validation). The descriptor types impls
    as bare ``type``; this narrows the read without re-validating."""

    name: str


def publish_plugins(registry: Registry, config: Config) -> None:
    """Publish every shipped plugin's capability rows (unconditionally) and
    the enabled plugins' bundled manifests into ``registry``.

    Resolves the enabled names first: an enabled name that is not an
    installed system plugin (a typo or an uninstalled plugin, R4) is a single
    typed ``ConfigError`` raised BEFORE any row is added, so the post-finalize
    validate block never sees an unknown enabled name. Set-membership against
    ``SYSTEM_PLUGINS`` also carries the degenerate ``[plugins] enabled``
    entries the Phase 3 loader does not normalize: an empty name is an unknown
    (``ConfigError``), and a duplicate resolves to the same plugin, which
    publication (iterating ``SYSTEM_PLUGINS``, not the enabled list) cannot
    double-publish.
    """
    # Resolved lazily (not a module-level import): SYSTEM_PLUGINS is defined in
    # the package __init__ AFTER this submodule is imported, so a top-level
    # import would read it mid-initialization. By call time (build_registry)
    # the package is fully initialized, and tests can monkeypatch the index.
    from agentworks.plugins import SYSTEM_PLUGINS

    unknown = {name for name in config.plugins_enabled if name not in SYSTEM_PLUGINS}
    if unknown:
        known = ", ".join(sorted(SYSTEM_PLUGINS)) or "(none installed)"
        raise ConfigError(
            f"[plugins] enabled names that are not installed system plugins: "
            f"{', '.join(sorted(unknown))} (installed system plugins: {known})"
        )

    # Capability rows for every shipped plugin, unconditionally (R5). The
    # enablement source (LLD b) marks a not-opted-in plugin's rows disabled at
    # finalize; a row exists only for an actually-seated impl, since build_row
    # reads the seated occupant.
    for plugin in SYSTEM_PLUGINS.values():
        origin = Origin.system_plugin(plugin=plugin.name, source=f"agentworks.plugins.{plugin.name}")
        for kind, impls in plugin.capabilities.items():
            adapter = CAPABILITY_ADAPTERS[kind]
            for impl in impls:
                name = cast("_NamedImpl", impl).name
                registry.add(kind, name, adapter.build_row(name, origin), origin)

    # Bundled manifests for enabled plugins only (R9). Iterate SYSTEM_PLUGINS
    # (not the enabled list) so a duplicate enabled name cannot double-publish.
    enabled = frozenset(config.plugins_enabled)
    for plugin in SYSTEM_PLUGINS.values():
        anchor = plugin.manifests
        if plugin.name not in enabled or anchor is None:
            continue
        _publish_plugin_manifests(registry, plugin.name, anchor)


def _publish_plugin_manifests(registry: Registry, plugin_name: str, anchor: str) -> None:
    """Publish ``plugin_name``'s bundled manifests through the shared loader
    body, stamping each entry with the plugin's per-file ``system-plugin``
    origin. Split out so the per-file origin closure is a cleanly typed inner
    function rather than a loop-bound lambda.

    The shared body raises a typed ``ConfigError`` on a bad anchor or a dirty
    bundle but cannot name the plugin (it is anchor-only); this re-raises with
    plugin attribution so a curation bug reads as ``plugin '<name>': ...``,
    mirroring ``register_plugin``'s attributed re-raises rather than surfacing
    an anchor the operator cannot map back to a plugin."""

    def origin_for(file_name: str) -> Origin:
        return Origin.system_plugin(
            plugin=plugin_name,
            source=f"agentworks.plugins.{plugin_name}/manifests/{file_name}",
        )

    try:
        publish_manifest_package(registry, anchor=anchor, subdir="manifests", origin_for=origin_for)
    except ConfigError as exc:
        raise ConfigError(f"plugin {plugin_name!r}: {exc}") from exc
