"""The ``Plugin`` descriptor, the reserved ``PluginCommand`` frame, and
``PluginError``.

This module is the plugin framework's data layer: a frozen, immutable
descriptor a shipped plugin authors, plus the typed error every
malformed-descriptor / duplicate-plugin / cross-plugin-collision case
raises. It deliberately depends on nothing in the capability or resource
machinery (it imports only the exception root and, for the reserved
``required_scopes`` annotation, ``ScopeLevel``): a ``Plugin`` is
constructible in a test without a registry, and it becomes valid or
rejected only when :func:`agentworks.plugins.registration.register_plugin`
seats it. That split is deliberate and pinned:

- **Immutability is a data invariant**, enforced at construction here
  (``__post_init__`` rewrites ``capabilities`` to an immutable
  ``MappingProxyType`` of tuples).
- **Descriptor validity** (name shape, adapter existence, impl typing,
  intra-descriptor collisions) is a registration-time contract, enforced
  in ``register_plugin``, which needs the adapter table this module must
  not import.

``__post_init__`` performs NO semantic validation, only the immutability
normalization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import ScopeLevel
    from agentworks.guide.contract import TopicContribution


class PluginError(StateError):
    """A shipped plugin's descriptor is malformed, its name duplicates
    another plugin's, or its capability impls collide with an existing
    occupant.

    A ``StateError`` (not ``ConfigError``): a shipped plugin is curated
    in-repo code, so a bad descriptor is a framework/curation bug, not
    operator data. (Operator-facing plugin errors, an unknown enabled
    name or an unknown ``[plugins]`` key, are ``ConfigError`` and live in
    the surfaces layer.) Every ``PluginError`` message names the
    offending plugin.
    """


@dataclass(frozen=True)
class PluginCommand:
    """Reserved frame for a plugin-owned CLI command (R10). Inert in v1:
    nothing constructs or dispatches one. Typed so the field is a real
    shape a later effort populates, not an untyped hole."""

    name: str


@dataclass(frozen=True)
class Plugin:
    """A system plugin's descriptor (R2, R10). Frozen and immutable; all
    fields optional except ``name``.

    - ``capabilities`` is keyed by capability kind (``"vm-platform"``,
      ``"harness-integration"``, ``"git-credential-provider"``, ``"secret-backend"``);
      each value is a tuple of impl CLASSES, uniformly. Every capability
      registry stores the exact class; adapters validate and seat it without
      construction. Every impl exposes ``name`` / ``description`` as class
      attributes, so identity is read and preserved uniformly.
    - ``manifests`` is the importlib-resources package anchor whose
      ``manifests/`` subdirectory holds the plugin's YAML (or ``None``).
      The surfaces layer resolves and loads it; this layer only stores it.
    - ``required_scopes`` is the reserved least-privilege declaration
      (R10), recorded and displayable but unenforced.
    - ``commands`` is a reserved, typed placeholder frame (R10), inert in
      v1.
    - ``guide_topics`` is inert contribution data consumed only by a
      guide-scoped catalog request.
    """

    name: str
    description: str = ""
    capabilities: Mapping[str, tuple[type, ...]] = field(default_factory=dict)
    manifests: str | None = None
    required_scopes: tuple[ScopeLevel, ...] = ()  # reserved, inert (R10)
    commands: tuple[PluginCommand, ...] = ()  # reserved, inert (R10)
    guide_topics: tuple[TopicContribution, ...] = ()

    def __post_init__(self) -> None:
        """Normalize descriptor contribution containers without validating them.

        ``capabilities`` becomes an immutable ``MappingProxyType`` whose values
        are tuples, and ``guide_topics`` becomes a tuple. The annotations carry
        the element shapes: nothing downstream re-derives them. What stays
        deferred is semantic validation of the content each element holds, to
        ``register_plugin`` or guide-scoped catalog build.
        """
        normalized = {kind: tuple(impls) for kind, impls in self.capabilities.items()}
        object.__setattr__(self, "capabilities", MappingProxyType(normalized))
        object.__setattr__(self, "guide_topics", tuple(self.guide_topics))
