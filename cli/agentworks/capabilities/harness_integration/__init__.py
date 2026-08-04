"""The ``harness integration`` capability: code-side handles for each
``session-template`` ``spec.harness_integration`` value.

Each harness integration implementation is a ``Capability`` (see
``capabilities/README.md``): it validates its ``harness_integration_config``, owns the
session's launch-target readiness, and produces the tmux pane command as its
op (``start`` / ``restart``). The consuming resource is the ``session`` node,
which HOLDS a harness integration instance and composes its readiness; that node lives in
the ``sessions`` domain, not here. Capabilities depend only on the framework,
never on their consuming domain (FRD R1): this package imports neither
``sessions`` nor ``orchestration``.

``ShellIntegration`` is the sole core built-in (and the default). The
``ClaudeCodeIntegration`` (name ``claude-code``) now ships in the opt-in
``claude`` system plugin (``agentworks.plugins.claude``); its adapter re-seats
it into ``HARNESS_INTEGRATION_REGISTRY`` at import, so ``harness_integration_for`` /
``ensure_harness_integration_enabled`` (which key by registry NAME, not the concrete
class) still find it, while its ROW publishes with a ``system-plugin`` origin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.harness_integration.base import HarnessIntegration, require_commands
from agentworks.capabilities.harness_integration.shell import ShellIntegration

if TYPE_CHECKING:
    from agentworks.resources import Registry

__all__ = [
    "HARNESS_INTEGRATION_REGISTRY",
    "HarnessIntegration",
    "ShellIntegration",
    "ensure_harness_integration_enabled",
    "harness_integration_for",
    "publish_to",
    "require_commands",
]


# The capability registry (the canonical harness integration list): harness integration name ->
# implementation class. ``validate`` (blob validation) and
# ``dependencies`` (implied references) are invoked through this dict at
# each source's blob boundary, and ``merge_config`` through it at
# resolve; descriptor rows publish from it.
#
# ``shell`` is the sole core built-in (and the default harness integration). The
# ``claude-code`` harness integration now ships as the ``claude`` system plugin
# (``agentworks.plugins.claude``), whose adapter re-seats it here at import;
# its ROW is published by ``plugins.publish_plugins`` with a ``system-plugin``
# origin, so ``publish_to`` below skips it.
HARNESS_INTEGRATION_REGISTRY: dict[str, type[HarnessIntegration]] = {
    ShellIntegration.name: ShellIntegration,
}


def harness_integration_for(name: str) -> type[HarnessIntegration]:
    """The harness integration class registered under ``name``, with typed framing on
    a miss.

    Unknown names are normally caught earlier by the kind's ``error``
    miss policy at finalize (a ``session-template`` ``spec.harness_integration`` typo
    surfaces there); this lookup, used by the resolver on names that
    already validated as references, raises a ``ConfigError`` as defense
    in depth.
    """
    from agentworks.errors import ConfigError

    try:
        return HARNESS_INTEGRATION_REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(HARNESS_INTEGRATION_REGISTRY)) or "(none)"
        raise ConfigError(f"unknown harness integration {name!r}; known harness integrations: {known}") from None


def ensure_harness_integration_enabled(registry: Registry, name: str) -> None:
    """The typed using-a-disabled-harness integration error (R14, the secret model).

    A ``session-template`` that names a disabled plugin harness integration STAYS ready
    (it does not propagate, mirroring how a ``secret`` stays ready while its
    backends are gated at resolution); the harness integration is instead gated at USE.
    This reads the harness integration's opt-in axis off the graph
    (``enablement_of("harness-integration", name)``) and, when disabled, raises naming the
    plugin to enable. The plugin name is derived from the harness integration row's
    ``system-plugin`` origin (``registry.lookup(...).origin.plugin``), since the
    mark's reason is not persisted on the frozen node; a disabled harness integration with a
    non-plugin origin (a direct test) falls back to a generic tail.

    Called at the two session-build call sites that hold the registry and the
    resolved template (``_create_build.py`` create, ``_lifecycle.py`` restart /
    reattach), NOT inside the node factories (which thread no registry) and NOT
    on the read-only ``_display_harness_integration`` listing path (an enabled template that
    references a disabled harness integration still shows the harness integration name; only its use
    fails).
    """
    from agentworks.errors import StateError
    from agentworks.resources.graph import Enablement

    if registry.graph.enablement_of("harness-integration", name) is not Enablement.disabled:
        return
    origin = getattr(registry.lookup("harness-integration", name), "origin", None)
    plugin = getattr(origin, "plugin", None)
    tail = f"enable plugin `{plugin}`" if plugin else "enable its unit"
    raise StateError(
        f"harness integration '{name}' is disabled; {tail}",
        entity_kind="harness-integration",
        entity_name=name,
        hint="`agw doctor` lists each plugin's state; enable the plugin providing this harness integration",
    )


def publish_to(registry: Registry) -> None:
    """Publish the core built-in harness integration types into the registry.

    Each entry lands as a ``HarnessIntegrationEntry`` row, built-in with source
    ``"agentworks.capabilities.harness_integration"``. Read-only rows: a
    ``session-template`` ``spec.harness_integration`` reference validates against
    them uniformly, and the harnesses list/describe like every other
    resource.

    A harness integration seated by a system plugin (``claude-code`` via the ``claude``
    plugin) keeps its impl in ``HARNESS_INTEGRATION_REGISTRY`` so the resolver can stamp it
    onto the graph node, but its row is published by ``plugins.publish_plugins``
    with a ``system-plugin`` origin. Skip those names here so the plugin is the
    sole publisher of the row; publishing it here too would collide (built-in vs
    system-plugin) at ``Registry.add``.
    """
    from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
    from agentworks.plugins.registration import plugin_seated_names
    from agentworks.resources import Origin

    seated_by_plugin = plugin_seated_names("harness-integration")
    code_origin = Origin.built_in(source="agentworks.capabilities.harness_integration")
    for type_name in sorted(HARNESS_INTEGRATION_REGISTRY):
        if type_name in seated_by_plugin:
            continue
        registry.add(
            "harness-integration",
            type_name,
            HarnessIntegrationEntry(name=type_name),
            code_origin,
        )
