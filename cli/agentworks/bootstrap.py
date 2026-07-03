"""Application-level glue: assemble a finalized ``Registry`` from the
standard set of publishers.

The "standard set of publishers" -- the bundled built-in manifests, the
catalog, the git-credential and secret-backend descriptors, the TOML
``Config``, and the operator's YAML ``ManifestSet`` -- is application
knowledge, not Registry knowledge and not Config knowledge. This module
is its legitimate home: it imports the publishers and orchestrates
them. Registry stays publisher-agnostic; Config stays unaware of the
others.

Call sites that need a finalized Registry for the common Config case use
``build_registry(config)``. Tests and multi-source orchestration can
assemble Registry by hand with ``Registry.empty()`` + explicit
``publish_to`` calls + ``finalize``.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

from agentworks.resources import Registry

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.manifests import ManifestSet

# One STANDARD registry per Config object. Commands may reach
# build_registry from several code paths; they all get the same
# finalized (frozen, read-only) instance, so once-per-command work
# hangs off registry identity (e.g. the secret resolver) and the
# manifest issues warn exactly once. Keyed by id() with a weakref
# guard against id reuse after garbage collection; explicit-manifests
# calls (tests, custom orchestration) always build fresh.
_STANDARD_REGISTRIES: dict[int, tuple[weakref.ref[Any], Registry]] = {}


def build_registry(config: Config, manifests: ManifestSet | None = None) -> Registry:
    """Build a finalized ``Registry`` from the standard set of publishers.

    Publisher order: built-in publishers first (``catalog``,
    ``git_credentials``, ``secrets``, the bundled manifests), then the
    operator sources (``Config.publish_to`` for TOML, then the YAML
    ``ManifestSet``). Operator rows may replace built-in rows only where
    the kind's ``builtin_override`` allows; operator-vs-operator
    collisions (including TOML-vs-manifest during the in-branch
    dual-source window) error at ``Registry.add``.

    When ``manifests`` is None (the standard path), the resources
    directory next to the loaded config file (``<config-dir>/resources/``)
    is auto-loaded, its spec-level warnings are surfaced (mirroring
    ``load_config``'s ``config_issues`` behavior), and the finalized
    Registry is memoized per Config object: every standard-path call
    with the same config returns the same frozen instance. That means
    on-disk changes (manifests, TOML) are observed by RELOADING the
    config -- a new Config object gets a new registry; anything
    rewriting config or manifests mid-process (e.g. the migration tool)
    must reload before rebuilding. Pass an explicit ``ManifestSet``
    (e.g. ``ManifestSet.empty()``) to skip the auto-load and always
    build fresh.
    """
    from agentworks import catalog, git_credentials, output, secrets
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
    from agentworks.manifests import builtin as builtin_manifests

    standard = manifests is None
    if standard:
        cached = _STANDARD_REGISTRIES.get(id(config))
        if cached is not None and cached[0]() is config:
            return cached[1]
    # Single-threaded CLI assumption: both this memo and the resolver
    # memo are unsynchronized check-then-set; a future SDD introducing a
    # long-lived multi-client (web, daemon) must revisit.
        resources_dir = config.source_path.parent / RESOURCES_DIRNAME
        manifests = load_manifests(resources_dir)
        for issue in manifests.issues:
            output.warn(f"Manifest: {issue}")

    assert manifests is not None
    registry = Registry.empty()
    # Built-in publishers first. The bundled manifests precede the
    # catalog publisher because catalog.publish_to also publishes the
    # operator's TOML catalog extensions (operator-declared rows), and
    # built-in rows must never land on top of operator rows.
    builtin_manifests.publish_to(registry)
    catalog.publish_to(registry, config)
    git_credentials.publish_to(registry)
    secrets.publish_to(registry)
    config.publish_to(registry)
    manifests.publish_to(registry)
    registry.finalize()
    if standard:
        # The eviction callback keeps dead configs from pinning their
        # registries (and, transitively, resolvers with cached secret
        # values) for process lifetime; the identity check guards
        # against evicting a NEW entry that reused the id.
        def _evict(dead_ref: weakref.ref[Any], config_id: int = id(config)) -> None:
            entry = _STANDARD_REGISTRIES.get(config_id)
            if entry is not None and entry[0] is dead_ref:
                del _STANDARD_REGISTRIES[config_id]

        _STANDARD_REGISTRIES[id(config)] = (weakref.ref(config, _evict), registry)
    return registry
