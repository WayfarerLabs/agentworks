"""``publish_plugins``: the ``build_registry`` publish step for system
plugins (R5 publication, R9 manifests, Phase 5 + Phase 7 parity).

Publication only. Every shipped plugin's capability impls were already
SEATED at import (the installed index called ``register_plugin`` for each,
LLD a), so this step mutates no module-level state and ``build_registry``
stays pure: it reads the seated impls through the capability adapters'
``build_row`` (the whitelisted builder path), stamps a ``system-plugin``
origin, and loads every shipped plugin's bundled manifests through the shared
loader body.

Both capabilities and manifests publish for **every shipped plugin,
unconditionally**, not gated by ``[plugins]`` (Phase 7 retired the Phase 5
enabled-only manifest gate). Enablement is LLD (b)'s job: its source marks a
not-opted-in plugin's rows ``disabled`` at finalize, so the row is
present-but-disabled (a reference to it is not-ready / refused at use with the
enable hint), never absent (which would surface as an unknown-name hard error).
The one asymmetry that remains is row STRENGTH, not presence: a not-enabled
plugin's manifest rows publish **weak** (add-if-absent, LLD c 3b.3) so a
disabled plugin's declarable row never blocks an operator/built-in/enabled row
in any publish order; an enabled plugin's manifest rows and ALL capability rows
publish strong. (Capability name clashes are caught at seating in
``register_plugin``, which is enablement-independent, so capability rows need no
weak treatment.) The earlier "a not-enabled plugin offers no resources an
operator references by name" rationale for enabled-only manifests is
SUPERSEDED: bundled declarables ARE referenceable by name (an agent-template's
``user_install_commands``, a template's ``inherits``), and an absent row makes
such a reference an unknown-name hard error rather than the enable hint.

Bundled kinds are restricted to ``PLUGIN_MANIFEST_KINDS`` (the declarable kinds
whose consumption gates Phase 7 wires); a bundle of any other kind, or of a
kind's reserved auto-declared name, is a typed ``ConfigError`` at publish.

An unknown enabled name is resolved up front, before any ``registry.add``, as
a single typed ``ConfigError`` (never a ``KeyError``, never the post-finalize
validate block).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from agentworks.errors import ConfigError
from agentworks.manifests.package import load_manifest_package, publish_manifest_package
from agentworks.plugins.adapters import capability_adapters
from agentworks.resources import Origin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.config import Config
    from agentworks.plugins.base import Plugin
    from agentworks.resources.registry import Registry


# The declarable kinds a system plugin may bundle in a manifest (Phase 7, LLD c
# 3b.2). Exactly the kinds whose consumption sites Phase 7 gates (LLD b's site
# table), so R9's "nothing a not-enabled plugin offers is available at a
# consumption site" is enforced BY CONSTRUCTION, not documented: a plugin cannot
# bundle a kind whose reference path is ungated. Expanding this set is a
# deliberate act (wire the kind's consumption gate, add it here, pin both with a
# test). The excluded kinds (secret, git-credential, vm-site, workspace-template,
# named-console-template) are the ones whose consumption paths Phase 7 does not
# gate.
PLUGIN_MANIFEST_KINDS = frozenset(
    {
        "system-install-command",
        "user-install-command",
        "apt-package",
        "apt-source",
        "vm-template",
        "agent-template",
        "admin-template",
        "session-template",
    }
)


class _NamedImpl(Protocol):
    """The one attribute publication reads off a seated impl class: its
    registry key ``name`` (a non-empty, ``/``-free ``str``, guaranteed by
    ``register_plugin``'s descriptor validation). The descriptor types impls
    as bare ``type``; this narrows the read without re-validating."""

    name: str


def plugin_manifest_resource_owners(plugins: Iterable[Plugin]) -> tuple[tuple[str, str, str], ...]:
    """Return declarable resource ownership from each plugin's manifest bundle."""
    owners: list[tuple[str, str, str]] = []
    for plugin in plugins:
        if plugin.manifests is None:
            continue
        manifests = load_manifest_package(
            anchor=plugin.manifests,
            subdir="manifests",
            allowed_kinds=PLUGIN_MANIFEST_KINDS,
        )
        owners.extend((plugin.name, entry.kind, entry.name) for entry in manifests.entries)
    return tuple(owners)


def publish_plugins(registry: Registry, config: Config) -> None:
    """Publish every shipped plugin's capability rows (unconditionally) and
    the enabled plugins' bundled manifests into ``registry``.

    Resolves the enabled names first: an enabled name that is not an
    installed system plugin (a typo or an uninstalled plugin, R4) is a single
    typed ``ConfigError`` raised BEFORE any row is added, so the post-finalize
    validate block never sees an unknown enabled name. Set-membership against
    ``SYSTEM_PLUGINS`` also carries the degenerate ``[plugins].system``
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

    unknown = {name for name in config.enabled_system_plugins if name not in SYSTEM_PLUGINS}
    if unknown:
        known = ", ".join(sorted(SYSTEM_PLUGINS)) or "(none installed)"
        raise ConfigError(
            f"[plugins].system names that are not installed system plugins: "
            f"{', '.join(sorted(unknown))} (installed system plugins: {known})"
        )

    # Capability rows for every shipped plugin, unconditionally (R5). The
    # enablement source (LLD b) marks a not-opted-in plugin's rows disabled at
    # finalize; a row exists only for an actually-seated impl, since build_row
    # reads the seated occupant.
    adapters = capability_adapters()
    for plugin in SYSTEM_PLUGINS.values():
        origin = Origin.system_plugin(plugin=plugin.name, source=f"agentworks.plugins.{plugin.name}")
        for kind, impls in plugin.capabilities.items():
            adapter = adapters[kind]
            for impl in impls:
                name = cast("_NamedImpl", impl).name
                registry.add(kind, name, adapter.build_row(name, origin), origin)

    # Bundled manifests for EVERY shipped plugin, unconditionally (R9, Phase 7).
    # The enabled set no longer gates publication; it decides only row STRENGTH:
    # a not-enabled plugin's manifest rows publish weak (add-if-absent), an
    # enabled plugin's publish strong. Iterate SYSTEM_PLUGINS (not the enabled
    # list) so a duplicate enabled name cannot double-publish.
    enabled = frozenset(config.enabled_system_plugins)
    for plugin in SYSTEM_PLUGINS.values():
        anchor = plugin.manifests
        if anchor is None:
            continue
        _publish_plugin_manifests(registry, plugin.name, anchor, weak=plugin.name not in enabled)


def _publish_plugin_manifests(registry: Registry, plugin_name: str, anchor: str, *, weak: bool) -> None:
    """Publish ``plugin_name``'s bundled manifests through the shared loader
    body, stamping each entry with the plugin's per-file ``system-plugin``
    origin and restricting the kinds to ``PLUGIN_MANIFEST_KINDS``. Split out so
    the per-file origin closure is a cleanly typed inner function rather than a
    loop-bound lambda.

    ``weak`` publishes a not-enabled plugin's rows add-if-absent (LLD c 3b.3),
    so a disabled plugin's declarable row never blocks a stronger row.

    The shared body raises a typed ``ConfigError`` on a bad anchor, a dirty
    bundle, an unbundleable kind, or a reserved-name bundle, but cannot name the
    plugin (it is anchor-only); this re-raises with plugin attribution so a
    curation bug reads as ``plugin '<name>': ...``, mirroring
    ``register_plugin``'s attributed re-raises rather than surfacing an anchor
    the operator cannot map back to a plugin."""

    def origin_for(file_name: str) -> Origin:
        return Origin.system_plugin(
            plugin=plugin_name,
            source=f"agentworks.plugins.{plugin_name}/manifests/{file_name}",
        )

    try:
        publish_manifest_package(
            registry,
            anchor=anchor,
            subdir="manifests",
            origin_for=origin_for,
            allowed_kinds=PLUGIN_MANIFEST_KINDS,
            weak=weak,
        )
    except ConfigError as exc:
        raise ConfigError(f"plugin {plugin_name!r}: {exc}") from exc
