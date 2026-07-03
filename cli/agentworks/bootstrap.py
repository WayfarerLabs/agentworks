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

from typing import TYPE_CHECKING

from agentworks.resources import Registry

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.manifests import ManifestSet

# Resources directories whose manifest issues were already warned this
# process (build_registry may run several times per command).
_WARNED_MANIFEST_DIRS: set[Path] = set()


def build_registry(config: Config, manifests: ManifestSet | None = None) -> Registry:
    """Build a finalized ``Registry`` from the standard set of publishers.

    Publisher order: built-in publishers first (``catalog``,
    ``git_credentials``, ``secrets``, the bundled manifests), then the
    operator sources (``Config.publish_to`` for TOML, then the YAML
    ``ManifestSet``). Operator rows may replace built-in rows only where
    the kind's ``builtin_override`` allows; operator-vs-operator
    collisions (including TOML-vs-manifest during the in-branch
    dual-source window) error at ``Registry.add``.

    When ``manifests`` is None, the resources directory next to the
    loaded config file (``<config-dir>/resources/``) is auto-loaded and
    its spec-level warnings are surfaced, mirroring ``load_config``'s
    ``config_issues`` behavior. Pass an explicit ``ManifestSet`` (e.g.
    ``ManifestSet.empty()``) to skip the auto-load.
    """
    from agentworks import catalog, git_credentials, output, secrets
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
    from agentworks.manifests import builtin as builtin_manifests

    if manifests is None:
        resources_dir = config.source_path.parent / RESOURCES_DIRNAME
        manifests = load_manifests(resources_dir)
        # Some commands build more than one registry; warn each
        # manifest issue once per directory per process, mirroring
        # load_config's one-shot config_issues warning.
        if resources_dir not in _WARNED_MANIFEST_DIRS:
            _WARNED_MANIFEST_DIRS.add(resources_dir)
            for issue in manifests.issues:
                output.warn(f"Manifest: {issue}")

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
    return registry
