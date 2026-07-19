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
from agentworks.capabilities.harness.shell import ShellHarness

if TYPE_CHECKING:
    from agentworks.resources import Registry

__all__ = [
    "HARNESS_REGISTRY",
    "Harness",
    "ShellHarness",
    "harness_for",
    "publish_to",
    "require_commands",
]


# The capability registry (the canonical harness list): harness name ->
# implementation class. ``validate_config`` (blob validation) is invoked
# through this dict at each source's blob boundary, and ``merge_config``
# through it at resolve; descriptor rows publish from it.
HARNESS_REGISTRY: dict[str, type[Harness]] = {
    ShellHarness.name: ShellHarness,
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
        raise ConfigError(
            f"unknown harness {name!r}; known harnesses: {known}"
        ) from None


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
