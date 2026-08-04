"""Application-level glue: assemble a finalized ``Registry`` from the
standard set of publishers.

The "standard set of publishers" (the bundled built-in manifests, the
apt and install-command operator publishers, the git-credential-provider
and secret-backend capability resources, the TOML ``Config``, and the
operator's YAML ``ManifestSet``) is application
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

from contextvars import ContextVar
from typing import TYPE_CHECKING

from agentworks.resources import Registry

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.manifests import ManifestSet


_warned_request_configs: ContextVar[frozenset[str]] = ContextVar("warned_request_configs", default=frozenset())
"""Config paths whose ambient manifest warnings were rendered this request."""


def begin_request_warning_scope() -> None:
    """Reset per-request warning aggregation at the CLI boundary.

    Managers may build a registry more than once during a single command
    (for example an orchestration preflight followed by realization). The
    warning belongs to the command request, so only its first composition
    root emits it. ``ContextVar`` keeps concurrent in-process callers
    independent; library callers that do not establish a scope still get one
    warning for their current context.
    """
    _warned_request_configs.set(frozenset())


def build_registry(config: Config, manifests: ManifestSet | None = None) -> Registry:
    """Build a finalized ``Registry`` from the standard set of publishers.

    Publisher order: the bundled built-in manifests first
    (``builtin_manifests``), then the ``apt`` / ``install_commands``
    operator publishers (the deprecated TOML surface for those two kinds;
    they follow the bundled manifests, which now supply the built-in
    apt/install-command entries), then the built-in capability rows
    (``git_credential``, ``harness``, ``secrets``, ``vm_platforms``), then
    the system plugins (``plugins.publish_plugins``: every shipped plugin's
    capability rows plus the enabled plugins' bundled manifests), then the
    operator sources (``Config.publish_to`` for TOML, then the YAML
    ``ManifestSet``). Plugin capability rows publish unconditionally and are
    marked disabled at finalize when not opted in (the injected
    ``plugin_enablement_source``). Operator rows may replace built-in rows
    only where the kind's ``builtin_override`` allows;
    operator-vs-operator collisions (a resource declared in both TOML and
    a manifest) error at ``Registry.add``.

    When ``manifests`` is None (the standard path), the resources
    directory next to the loaded config file (``<config-dir>/resources/``)
    is auto-loaded without rendering warnings. Request entrypoints use
    ``load_request_registry`` when they need to render manifest warnings.
    Pass an explicit ``ManifestSet`` (e.g. ``ManifestSet.empty()``) to skip
    the auto-load.
    """
    from agentworks import apt, install_commands, plugins, secrets
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

    # Host support is NOT a bootstrap concern: every platform publishes its
    # capability row unconditionally (R13; host support is the row's folded
    # readiness, not its absence), every vm-site (bundled and declared alike)
    # registers unconditionally and is NOT-READY when it lacks what it needs
    # (its readiness verdict, folded at finalize and read off the graph), and
    # the registry's reserved-name override fires on every host because the
    # bundled rows always publish. Using a not-ready site is a typed error at
    # resolve time; doctor warns on references to one.
    registry = Registry.empty()
    # Built-in publishers first. The bundled manifests now supply the
    # built-in apt/install-command entries too (apt sources/packages and
    # install commands ship as manifests/builtin/*.yaml), so they must
    # precede the apt / install_commands operator publishers, which publish
    # only the operator's deprecated TOML extensions (operator-declared
    # rows): built-in rows must never land on top of operator rows.
    builtin_manifests.publish_to(registry)
    apt.publish_to(registry, config)
    install_commands.publish_to(registry, config)
    git_credential.publish_to(registry)
    harness.publish_to(registry)
    secrets.publish_to(registry)
    vm_platforms.publish_to(registry)
    # System plugins publish here, after the built-in capability rows and
    # before the operator sources: every shipped plugin's capability rows
    # unconditionally (present-but-disabled when not opted in, via the
    # enablement source below), and the enabled plugins' bundled manifests.
    # Publication-only (impls were seated at import), so purity holds.
    plugins.publish_plugins(registry, config)
    config.publish_to(registry)
    manifests.publish_to(registry)
    registry.finalize(enablement_sources=[plugins.plugin_enablement_source(config)])
    # Config consistency against the finalized graph: subsystems whose
    # SETTINGS name resources validate them here, at the boundary that
    # holds both worlds. The chain ([secret_config].backends) and
    # defaults.site are config, not resources; this is each subsystem
    # consuming its config in normal operation, so every
    # resource-touching command fails fast with config vocabulary.
    secrets.validate_chain(config, registry)
    vm_sites.validate_sites(config, registry)
    return registry


def harness_selector_deprecation(config: Config, manifests: ManifestSet) -> str | None:
    """Return the one combined old-selector warning, without emitting it."""
    resources = (*config.deprecated_harness_selectors, *manifests.deprecated_harness_selectors)
    if not resources:
        return None
    return (
        f"deprecated session-template selector in: {', '.join(resources)}. "
        "`harness` is deprecated; use `harness_integration` instead. "
        "It will be removed in 0.14.0. Run `agw resource migrate` to rewrite these declarations. "
        "Silence this warning with --no-deprecations."
    )


def load_request_registry(config: Config, manifests: ManifestSet | None = None, *, warn: bool = True) -> Registry:
    """Build a registry and render request-scoped warnings once."""
    from agentworks import output
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests

    resolved = manifests if manifests is not None else load_manifests(config.source_path.parent / RESOURCES_DIRNAME)
    registry = build_registry(config, resolved)
    request_key = str(config.source_path)
    already_warned = request_key in _warned_request_configs.get()
    if warn and not already_warned:
        _warned_request_configs.set(_warned_request_configs.get() | {request_key})
        for issue in resolved.issues:
            output.warn(f"Manifest: {issue}")
        if not output.deprecations_suppressed():
            for issue in resolved.deprecation_issues:
                output.warn(f"Manifest: {issue}")
            if message := harness_selector_deprecation(config, resolved):
                output.warn(message)
    return registry
