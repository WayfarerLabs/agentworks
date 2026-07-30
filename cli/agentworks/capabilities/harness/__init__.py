"""The ``harness`` capability: code-side handles for each
``session-template`` ``spec.harness`` value.

Each harness implementation (``ShellHarness``, ``ClaudeCodeHarness``) is
a ``Capability`` (see ``capabilities/README.md``): it validates its
``harness_config``, owns the session's launch-target readiness, and
produces the tmux pane command as its op (``start`` / ``restart``). The
consuming resource is the ``session`` node, which HOLDS a harness
instance and composes its readiness; that node lives in the ``sessions``
domain, not here. Capabilities depend only on the framework, never on
their consuming domain (FRD R1): this package imports neither
``sessions`` nor ``orchestration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.harness.base import Harness, require_commands
from agentworks.capabilities.harness.claude_code import ClaudeCodeHarness
from agentworks.capabilities.harness.shell import ShellHarness

if TYPE_CHECKING:
    from agentworks.resources import Registry

__all__ = [
    "HARNESS_REGISTRY",
    "ClaudeCodeHarness",
    "Harness",
    "ShellHarness",
    "ensure_harness_enabled",
    "harness_for",
    "publish_to",
    "require_commands",
]


# The capability registry (the canonical harness list): harness name ->
# implementation class. ``validate`` (blob validation) and
# ``dependencies`` (implied references) are invoked through this dict at
# each source's blob boundary, and ``merge_config`` through it at
# resolve; descriptor rows publish from it.
HARNESS_REGISTRY: dict[str, type[Harness]] = {
    ShellHarness.name: ShellHarness,
    ClaudeCodeHarness.name: ClaudeCodeHarness,
}


def harness_for(name: str) -> type[Harness]:
    """The harness class registered under ``name``, with typed framing on
    a miss.

    Unknown names are normally caught earlier by the kind's ``error``
    miss policy at finalize (a ``session-template`` ``spec.harness`` typo
    surfaces there); this lookup, used by the resolver on names that
    already validated as references, raises a ``ConfigError`` as defense
    in depth.
    """
    from agentworks.errors import ConfigError

    try:
        return HARNESS_REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(HARNESS_REGISTRY)) or "(none)"
        raise ConfigError(f"unknown harness {name!r}; known harnesses: {known}") from None


def ensure_harness_enabled(registry: Registry, name: str) -> None:
    """The typed using-a-disabled-harness error (R14, the secret model).

    A ``session-template`` that names a disabled plugin harness STAYS ready
    (it does not propagate, mirroring how a ``secret`` stays ready while its
    backends are gated at resolution); the harness is instead gated at USE.
    This reads the harness's opt-in axis off the graph
    (``enablement_of("harness", name)``) and, when disabled, raises naming the
    plugin to enable. The plugin name is derived from the harness row's
    ``system-plugin`` origin (``registry.lookup(...).origin.plugin``), since the
    mark's reason is not persisted on the frozen node; a disabled harness with a
    non-plugin origin (a direct test) falls back to a generic tail.

    Called at the two session-build call sites that hold the registry and the
    resolved template (``_create_build.py`` create, ``_lifecycle.py`` restart /
    reattach), NOT inside the node factories (which thread no registry) and NOT
    on the read-only ``_display_harness`` listing path (an enabled template that
    references a disabled harness still shows the harness name; only its use
    fails).
    """
    from agentworks.errors import StateError
    from agentworks.resources.graph import Enablement

    if registry.graph.enablement_of("harness", name) is not Enablement.disabled:
        return
    origin = getattr(registry.lookup("harness", name), "origin", None)
    plugin = getattr(origin, "plugin", None)
    tail = f"enable plugin `{plugin}`" if plugin else "enable its unit"
    raise StateError(
        f"harness '{name}' is disabled; {tail}",
        entity_kind="harness",
        entity_name=name,
        hint="`agw doctor` lists each plugin's state; enable the plugin providing this harness",
    )


def publish_to(registry: Registry) -> None:
    """Publish the known harness types into the registry.

    Each entry lands as a ``HarnessEntry`` row, built-in with source
    ``"agentworks.capabilities.harness"``. Read-only rows: a
    ``session-template`` ``spec.harness`` reference validates against
    them uniformly, and the harnesses list/describe like every other
    resource.
    """
    from agentworks.capabilities.harness.kinds import HarnessEntry
    from agentworks.resources import Origin

    code_origin = Origin.built_in(source="agentworks.capabilities.harness")
    for type_name in sorted(HARNESS_REGISTRY):
        registry.add(
            "harness",
            type_name,
            HarnessEntry(name=type_name),
            code_origin,
        )
