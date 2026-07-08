"""``ResourceKind`` Protocol and ``KIND_REGISTRY``.

A ``ResourceKind`` is the per-kind strategy the framework consults during
``Registry.finalize()``: it tells the Registry which miss policy to use when
a reference's ``(kind, name)`` doesn't resolve to a published Resource,
which names auto-declare is allowed to synthesize, and how to build the
synthesized Resource.

The framework/domain split: ``resources/`` owns the framework
(``KIND_REGISTRY``, the ``ResourceKind`` protocol, ``Registry.finalize``).
Each domain package defines AND registers its own kinds -- both the
declarable row dataclasses and the capability kinds live next to the
code that implements them (``agentworks.vms.kinds``,
``agentworks.secrets.kinds``, ``agentworks.catalog``, etc.). Every kind
self-registers a single instance into ``KIND_REGISTRY`` at import.
``resources/kinds/__init__`` is only the registration index: it imports
each domain's kind module so a single ``from agentworks.resources import
KIND_REGISTRY`` populates the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class InstanceRef:
    """One live DB instance that depends on a Resource per current config.

    Returned by ``ResourceKind.instances(...)``; rendered as the per-row
    contribution to ``agw resource list``'s ``USED BY`` column count and to
    ``agw resource describe``'s ``Used by:`` section.

    Fields:

    - ``instance_kind``: the DB row's kind identifier (``"vm"``, ``"agent"``,
      ``"workspace"``, ``"session"``, ``"console"``). Used by the describe
      view to group entries by instance type.
    - ``instance_name``: the DB row's name (``vm.name``, ``session.name``,
      etc.).

    The shape is intentionally minimal so a future provisioned-state
    SDD can return the same dataclass from a sibling
    ``provisioned_instances(...)`` hook (today's projection is "per
    current config"; a manifest-driven sibling would be "per provisioned
    state"). See the Phase 3c "Forward-compat note" in the plan.
    """

    instance_kind: str
    instance_name: str


class ResourceKind(Protocol):
    """Per-kind strategy consulted by ``Registry.finalize()``.

    Attribute contracts:

    - ``kind``: the kind identifier matching ``ResourceReference.kind``,
      ``Origin.source[0]`` (for auto-declared), and the Registry's per-kind
      dict key.
    - ``miss_policy``: which branch ``Registry.finalize()`` takes when a
      reference points at a name not in the Registry. ``"auto-declare"``
      synthesizes via this kind; ``"error"`` raises ``ConfigError``.
    - ``auto_declare_names``: when ``miss_policy == "auto-declare"``, the
      set of names the kind accepts auto-declaring. ``None`` means "any
      name" (secrets). A non-empty set means "only these reserved names"
      (templates accept ``{"default"}``); requests for other missing names
      error.
    - ``category``: what a kind's resources ARE (ADR 0016; per-kind by
      construction -- two resources of one kind can never differ here).
      ``"declarable"`` kinds hold data (operator TOML/YAML,
      auto-declared, or built-in rows); ``"capability"`` kinds hold
      read-only capability resources registered by the app (or, later,
      plugins), implementation in a per-domain code registry. Only
      declarable kinds may appear in manifests; the envelope enforces
      it. Kinds themselves are baked into the app -- plugins publish
      resources of existing kinds (declarable rows via bundled
      manifests, capability rows via code registration), never new
      kinds.
    - ``description``: one operator-facing line for ``agw resource
      kinds``.
    - ``builtin_override``: what happens when an operator manifest
      collides with an app-published built-in row. ``"allow"`` keeps
      today's catalog behavior (operator row replaces the built-in);
      ``"reserved"`` makes the collision a ``ConfigError``.
    - ``synthesize(references)``: called when an auto-declare-allowed
      missing name is being synthesized. Receives all matching references
      known so far (in config-load walk order). Returns the synthesized
      Resource with ``origin = Origin.auto_declared(...)`` attached.
      ``usage`` is NOT attached here -- ``Registry.finalize`` centralizes
      usage attachment in a post-stabilization pass so synthesized
      Resources can accrue later-discovered incoming edges from
      second-level dispatches uniformly with operator-declared ones.

      **Empty-references contract** (Phase 2a): every kind's
      ``synthesize`` must have defined behavior when called with
      ``references=()``. Kinds whose ``auto_declare_names`` is a
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
    ``agentworks.secrets.base``, ``AdminConfig`` from
    ``agentworks.agents.template``, etc.). The Registry stores whatever
    ``synthesize`` returns; the kind
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

    @property
    def category(self) -> Literal["declarable", "capability"]: ...

    @property
    def description(self) -> str: ...

    @property
    def builtin_override(self) -> Literal["allow", "reserved"]: ...

    def synthesize(self, references: Sequence[ResourceReference]) -> Any: ...

    # The optional ``instances(db, registry, resource) -> Iterable[InstanceRef]``
    # method is intentionally NOT declared on this Protocol. Kinds with a
    # per-instance lifecycle concept (the four named template kinds plus
    # ``admin-template`` plus ``secret``) implement it; kinds without
    # (catalog, ``git-credential-provider``, ``secret-backend``) omit it
    # entirely. The framework's consumer (``agentworks.resources.inspect``)
    # uses ``getattr(handler, "instances", None)`` to gate the call, so
    # absent-on-class IS the "no instance concept" signal. Declaring the
    # method on the Protocol would force every kind to either implement
    # it (Liskov violation for kinds where it's meaningless) or use
    # ``# type: ignore`` to opt out. Structural-duck-typing keeps the
    # contract honest. The shape of the optional method is:
    #
    #     def instances(self, db: Database, registry: Registry,
    #                   resource: Any) -> Iterable[InstanceRef]:
    #         ...
    #
    # Per current config: a future SDD adding provisioned-state tracking
    # would add a sibling ``provisioned_instances(...)`` hook returning
    # the same ``InstanceRef`` shape from manifests; today's
    # ``instances`` is the config-projected dimension.
    #
    # Deliberately ABSENT: a kind-level semantic-validation hook.
    # Checks that need config alongside the finalized graph (the secret
    # chain's names and reachability) are the owning subsystem's job,
    # run from ``bootstrap.build_registry`` after finalize -- config is
    # a setting, not a resource, and the Registry never sees it.


class NoUnreferencedDefaultError(Exception):
    """Raised by a ``ResourceKind.synthesize`` when called with
    ``references=()`` and the kind has no notion of an unreferenced
    default (i.e., ``auto_declare_names is None``).

    The framework's always-materialize pre-step in ``Registry.finalize``
    only calls ``synthesize(references=())`` for kinds whose
    ``auto_declare_names`` is a non-None set, so this error is never
    raised in normal operation. The error exists so a kind's contract
    stays well-defined under future changes: if a kind that today has
    ``auto_declare_names = None`` gains a reserved name, the kind's
    ``synthesize`` already has an obvious "fix me" landing pad.
    """


# Operator-surface singleton kinds whose rows the TOML publisher
# synthesizes (SourceLocation line == 0) when the operator omits the
# sections; see Registry._check_collision.
SYNTHESIZED_SINGLETON_KINDS = frozenset({"admin-template", "named-console-template"})

# Reserved Origin source kind for always-materialized rows. The string
# "framework" must not be used as a real kind name in ``KIND_REGISTRY``.
ALWAYS_MATERIALIZE_SOURCE: tuple[str, str] = ("framework", "always-materialize")


KIND_REGISTRY: dict[str, ResourceKind] = {}
"""Module-level registry mapping kind identifier -> ``ResourceKind`` instance.

Populated by side-effect: each domain's kind module (``agentworks.vms.kinds``,
``agentworks.secrets.kinds``, ``agentworks.catalog``, etc.) instantiates its
kind and writes ``KIND_REGISTRY[<kind>] = <instance>`` at module-load.
``resources/kinds/__init__.py`` is the registration index that imports every
domain kind module so the registry is populated after
``import agentworks.resources``.
"""
