"""``ResourceKind`` Protocol and ``KIND_REGISTRY``.

A ``ResourceKind`` is the per-kind strategy the framework consults during
``Registry.finalize()``: it tells the Registry which miss policy to use when
a requirement's ``(kind, name)`` doesn't resolve to a published Resource,
which names auto-declare is allowed to synthesize, and how to build the
synthesized Resource.

Each kind lives in its own module under ``kinds/`` and self-registers a
single instance into ``KIND_REGISTRY`` at import. ``kinds/__init__.py``
imports every kind module so a single ``from agentworks.resources import     KIND_REGISTRY``
populates the registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


class ResourceKind(Protocol):
    """Per-kind strategy consulted by ``Registry.finalize()``.

    Attribute contracts:

    - ``kind``: the kind identifier matching ``ResourceRequirement.kind``,
      ``Origin.source[0]`` (for auto-declared), and the Registry's per-kind
      dict key.
    - ``miss_policy``: which branch ``Registry.finalize()`` takes when a
      requirement points at a name not in the Registry. ``"auto-declare"``
      synthesizes via this kind; ``"error"`` raises ``ConfigError``.
    - ``auto_declare_names``: when ``miss_policy == "auto-declare"``, the
      set of names the kind accepts auto-declaring. ``None`` means "any
      name" (secrets). A non-empty set means "only these reserved names"
      (templates accept ``{"default"}``); requests for other missing names
      error.
    - ``synthesize(requirements)``: called when an auto-declare-allowed
      missing name is being synthesized. Receives all matching requirements
      in config-load walk order. Returns the synthesized Resource instance
      with framework metadata (``origin = Origin.auto_declared(...)``,
      ``usage`` built from the requirements) already attached.

    The return type of ``synthesize`` is ``Any`` because Resources are
    diverse types from different modules (``SecretDecl`` from
    ``agentworks.secrets.base``, ``AdminConfig`` from ``agentworks.config``,
    etc.). The Registry stores whatever ``synthesize`` returns; the kind
    knows the right shape for its kind.

    The attributes are declared as ``@property`` so frozen-dataclass
    implementations (with their read-only fields) satisfy the Protocol;
    a settable-attribute Protocol would reject them.
    """

    @property
    def kind(self) -> str: ...

    @property
    def miss_policy(self) -> Literal["auto-declare", "error"]: ...

    @property
    def auto_declare_names(self) -> frozenset[str] | None: ...

    def synthesize(self, requirements: Sequence[ResourceRequirement]) -> Any: ...


KIND_REGISTRY: dict[str, ResourceKind] = {}
"""Module-level registry mapping kind identifier -> ``ResourceKind`` instance.

Populated by side-effect: each ``kinds/*.py`` module instantiates its kind
and writes ``KIND_REGISTRY[<kind>] = <instance>`` at module-load.
``kinds/__init__.py`` imports every kind module so the registry is
populated after ``import agentworks.resources``.
"""
