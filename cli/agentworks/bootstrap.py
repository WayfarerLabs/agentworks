"""Application-level glue: assemble a finalized ``Registry`` from the
standard set of publishers.

The "standard set of publishers" -- the bundled built-in manifests, the
catalog, the git-credential-provider and secret-backend capability resources, the TOML
``Config``, and the operator's YAML ``ManifestSet`` -- is application
knowledge, not Registry knowledge and not Config knowledge. This module
is its legitimate home: it imports the publishers and orchestrates
them. Registry stays publisher-agnostic; Config stays unaware of the
others.

``build_registry`` is a pure function: no memo, no cache. Each
composition root calls it once and threads the registry down; the
orchestrated ``session create --new-workspace/--new-agent`` realizes
its ephemeral workspace and agent through the shared realize bodies
against the one registry it built, so no nested root builds a second
one. Tests and multi-source orchestration can assemble
Registry by hand with ``Registry.empty()`` + explicit ``publish_to``
calls + ``finalize``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.resources import Registry

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.manifests import ManifestSet


def build_registry(config: Config, manifests: ManifestSet | None = None) -> Registry:
    """Build a finalized ``Registry`` from the standard set of publishers.

    Publisher order: built-in publishers first (``catalog``,
    ``git_credentials``, ``secrets``, the bundled manifests), then the
    operator sources (``Config.publish_to`` for TOML, then the YAML
    ``ManifestSet``). Operator rows may replace built-in rows only where
    the kind's ``builtin_override`` allows; operator-vs-operator
    collisions (a resource declared in both TOML and a manifest) error
    at ``Registry.add``.

    When ``manifests`` is None (the standard path), the resources
    directory next to the loaded config file (``<config-dir>/resources/``)
    is auto-loaded and its spec-level warnings are surfaced (mirroring
    ``load_config``'s ``config_issues`` behavior). Pass an explicit
    ``ManifestSet`` (e.g. ``ManifestSet.empty()``) to skip the auto-load.
    """
    from agentworks import catalog, output, secrets
    from agentworks.capabilities import git_credential, harness
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.errors import StateError
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.vms import sites as vm_sites

    if not config.resources_loaded:
        raise StateError(
            "build_registry requires a Config loaded with resources=True; "
            "this one was loaded settings-only (load_config(resources=False)), "
            "so publishing it would silently drop every TOML-declared resource"
        )

    if manifests is None:
        resources_dir = config.source_path.parent / RESOURCES_DIRNAME
        manifests = load_manifests(resources_dir)
        for issue in manifests.issues:
            output.warn(f"Manifest: {issue}")

    # Host support is NOT a bootstrap concern: platforms gate their own
    # capability rows (vm_platforms.publish_to), every vm-site (bundled
    # and declared alike) registers unconditionally and self-disables
    # when it lacks what it needs (the vm-site kind's generic
    # disabled_reason hook), and the registry's reserved-name override
    # fires on every host because the bundled rows always publish.
    # Using a disabled site is a typed error at resolve time;
    # doctor warns on references to one.
    registry = Registry.empty()
    # Built-in publishers first. The bundled manifests precede the
    # catalog publisher because catalog.publish_to also publishes the
    # operator's TOML catalog extensions (operator-declared rows), and
    # built-in rows must never land on top of operator rows.
    builtin_manifests.publish_to(registry)
    catalog.publish_to(registry, config)
    git_credential.publish_to(registry)
    harness.publish_to(registry)
    secrets.publish_to(registry)
    vm_platforms.publish_to(registry)
    config.publish_to(registry)
    manifests.publish_to(registry)
    registry.finalize()
    # Config consistency against the finalized graph: subsystems whose
    # SETTINGS name resources validate them here, at the boundary that
    # holds both worlds. The chain ([secret_config].backends) and
    # defaults.site are config, not resources; this is each subsystem
    # consuming its config in normal operation, so every
    # resource-touching command fails fast with config vocabulary.
    secrets.validate_chain(config, registry)
    vm_sites.validate_sites(config, registry)
    return registry
