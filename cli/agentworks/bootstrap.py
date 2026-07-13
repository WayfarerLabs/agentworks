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
composition root calls it once and threads the registry down. Nested
service entries are their own composition units -- ``session create
--new-workspace/--new-agent`` invokes ``create_workspace`` /
``create_agent``, each of which builds its own registry (and the
manifest warnings repeat accordingly; config-load-scale work, no
backend calls). Tests and multi-source orchestration can assemble
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
    from agentworks import catalog, git_credentials, output, secrets
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

    # Host-support gating (the platform's own call, not a config knob):
    # an unsupported platform publishes no capability row, and a
    # declared site referencing one must fail HERE with the platform's
    # stated requirement -- pre-finalize, because finalize would
    # otherwise beat us to it with the framework's generic
    # reference-miss error, which can't say "requires Windows".
    unsupported = vm_platforms.unsupported_platforms()
    if unsupported:
        declared_sites = [
            (entry.name, getattr(entry.resource, "platform", None))
            for entry in manifests.entries
            if entry.kind == "vm-site"
        ] + [(name, decl.platform) for name, decl in config.vm_sites.items()]
        for site_name, platform_name in declared_sites:
            if platform_name in unsupported:
                from agentworks.errors import ConfigError

                raise ConfigError(
                    f"vm-site '{site_name}' references platform "
                    f"'{platform_name}', which is disabled on this host: "
                    f"{unsupported[platform_name]}",
                    hint=(
                        "The platform is installed but its host requirements "
                        "are not met; remove the site declaration on this "
                        "machine or run it on a host that meets them."
                    ),
                )

    def _skip_unsupported_bundled_site(entry: object) -> bool:
        # A bundled vm-site publishes only when its platform says the
        # zero-config site is viable here (lima-local needs a local
        # limactl; wsl2 needs Windows + wsl.exe). Operator-declared
        # sites never pass through this predicate.
        if getattr(entry, "kind", None) != "vm-site":
            return False
        platform_name = getattr(entry.resource, "platform", None)  # type: ignore[attr-defined]
        if not isinstance(platform_name, str):
            return True
        platform_cls = vm_platforms.VM_PLATFORM_REGISTRY.get(platform_name)
        return (
            platform_cls is None
            or platform_cls.bundled_site_unsupported_reason() is not None
        )

    registry = Registry.empty()
    # Built-in publishers first. The bundled manifests precede the
    # catalog publisher because catalog.publish_to also publishes the
    # operator's TOML catalog extensions (operator-declared rows), and
    # built-in rows must never land on top of operator rows.
    builtin_manifests.publish_to(registry, skip=_skip_unsupported_bundled_site)
    catalog.publish_to(registry, config)
    git_credentials.publish_to(registry)
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
