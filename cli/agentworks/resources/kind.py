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
      known so far (in config-load walk order). Returns the synthesized
      Resource with ``origin = Origin.auto_declared(...)`` attached.
      ``usage`` is NOT attached here -- ``Registry.finalize`` centralizes
      usage attachment in a post-stabilization pass so synthesized
      Resources can accrue later-discovered incoming edges from
      second-level dispatches uniformly with operator-declared ones.

      **Empty-requirements contract** (Phase 2a): every kind's
      ``synthesize`` must have defined behavior when called with
      ``requirements=()``. Kinds whose ``auto_declare_names`` is a
      non-None set MUST build a code-defined default in this case (the
      framework's always-materialize pre-step calls them this way to
      guarantee reserved-default names exist in the registry); they use
      the reserved sentinel ``Origin.auto_declared(source=("framework",
      "always-materialize"))`` so the breadcrumb shows where the row came
      from. Kinds with ``auto_declare_names = None`` raise
      ``NoUnreferencedDefaultError`` -- the framework never calls them
      that way, but the kind's contract must still be defined
      (defensive against future ``auto_declare_names`` changes).

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


class NoUnreferencedDefaultError(Exception):
    """Raised by a ``ResourceKind.synthesize`` when called with
    ``requirements=()`` and the kind has no notion of an unreferenced
    default (i.e., ``auto_declare_names is None``).

    The framework's always-materialize pre-step in ``Registry.finalize``
    only calls ``synthesize(requirements=())`` for kinds whose
    ``auto_declare_names`` is a non-None set, so this error is never
    raised in normal operation. The error exists so a kind's contract
    stays well-defined under future changes: if a kind that today has
    ``auto_declare_names = None`` gains a reserved name, the kind's
    ``synthesize`` already has an obvious "fix me" landing pad.
    """


# Reserved Origin source kind for always-materialized rows. The string
# "framework" must not be used as a real kind name in ``KIND_REGISTRY``.
ALWAYS_MATERIALIZE_SOURCE: tuple[str, str] = ("framework", "always-materialize")


KIND_REGISTRY: dict[str, ResourceKind] = {}
"""Module-level registry mapping kind identifier -> ``ResourceKind`` instance.

Populated by side-effect: each ``kinds/*.py`` module instantiates its kind
and writes ``KIND_REGISTRY[<kind>] = <instance>`` at module-load.
``kinds/__init__.py`` imports every kind module so the registry is
populated after ``import agentworks.resources``.
"""
