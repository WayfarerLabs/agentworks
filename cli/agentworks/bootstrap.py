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
    (``builtin_manifests``, which supply the built-in apt/install-command
    entries too), then the built-in capability rows (one generic publisher
    per capability-kind descriptor), then the system
    plugins (``plugins.publish_plugins``: every shipped plugin's capability
    rows plus the enabled plugins' bundled manifests), then the operator's
    YAML ``ManifestSet`` (``Config.publish_to`` is a no-op now: config.toml
    is settings only, ADR 0022). Plugin capability rows publish
    unconditionally and are marked disabled at finalize when not opted in
    (the injected ``plugin_enablement_source``). Operator rows may replace
    built-in rows only where the kind's ``builtin_override`` allows.

    When ``manifests`` is None (the standard path), the resources
    directory next to the loaded config file (``<config-dir>/resources/``)
    is auto-loaded without rendering warnings. Request entrypoints use
    ``load_request_registry`` when they need to render manifest warnings.
    Pass an explicit ``ManifestSet`` (e.g. ``ManifestSet.empty()``) to skip
    the auto-load.
    """
    from agentworks import plugins, secrets
    from agentworks.capabilities.descriptor import capability_descriptors
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.config.references import validate_setting_references
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
    from agentworks.manifests import builtin as builtin_manifests

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
    # Built-in publishers first. The bundled manifests supply the built-in
    # apt/install-command entries (apt sources/packages and install commands
    # ship as manifests/builtin/*.yaml); operator apt/install rows are YAML
    # manifests now (config.toml is settings only, ADR 0022), so there is no
    # separate apt / install_commands operator publisher anymore.
    builtin_manifests.publish_to(registry)
    # One generic publisher per capability kind, over the descriptor table:
    # publication is membership, so a new capability kind publishes its
    # built-in rows by existing rather than by being added here.
    for descriptor in capability_descriptors():
        publish_capability_rows(registry, descriptor)
    # System plugins publish here, after the built-in capability rows and
    # before the operator sources: every shipped plugin's capability rows
    # unconditionally (present-but-disabled when not opted in, via the
    # enablement source below), and the enabled plugins' bundled manifests.
    # Publication-only (impls were seated at import), so purity holds.
    plugins.publish_plugins(registry, config)
    config.publish_to(registry)
    manifests.publish_to(registry)
    registry.finalize(enablement_sources=[plugins.plugin_enablement_source(config)])
    # Config consistency against the finalized graph, at the boundary that
    # holds both worlds. The registry cannot do this itself: it is
    # config-agnostic by construction and settings are never published as
    # pseudo-resources (ADR 0016), so "does this settings value name a real
    # row" can only be answered once BOTH exist, which is here.
    #
    # Two steps, in this order, because they answer different questions and
    # the second reads the first's answer as given:
    #
    # 1. EXISTENCE, generically. Every settings value that names a resource
    #    must resolve to one, or it is a hard error (operator ruling,
    #    2026-08-07), in the same shape a dangling manifest reference gets at
    #    finalize. One table covers every such setting, so a new one is a row
    #    there rather than a new per-subsystem validator.
    # 2. SEMANTICS, per subsystem. What a subsystem additionally requires of
    #    names that DO resolve: here, that every declared secret is reachable
    #    via some backend in the chain. Running this first would report a
    #    misspelled backend as an "unreachable secret", because a name
    #    matching no edge just drops out of the intersection.
    validate_setting_references(config, registry)
    secrets.validate_chain(config, registry)
    return registry


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
    return registry
