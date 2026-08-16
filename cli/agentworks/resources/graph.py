"""``DependencyGraph``: the framework's retained, queryable dependency graph.

``Registry.finalize`` builds one of these from the reference walk it already
runs and hands it to the ``Registry`` to retain (``Registry.graph``). The graph
is the single access path for structural questions the codebase previously
answered by re-walking ``dependencies(context)`` or reading a ``references``
field off each resource dataclass:

- ``edges_of``: a node's outbound reference edges (who it points at).
- ``incoming_edges_of``: a node's full inbound reference edges (who points at it).
- ``dependents_of``: a node's inbound references (who points at it), the
  reduced compatibility projection that replaced the removed per-resource
  ``references`` field.
- ``runtime_reachable_from`` / ``composed_from``: the two transitive closures of
  ``edges_of`` from a node, over what it NEEDS and over what it is MADE OF
  (FR17: an inheritance edge is source composition, not a runtime need). There
  is deliberately no closure over both: a caller has to say which question it
  is asking, because getting it wrong is a wrong answer rather than a crash.
- ``readiness_of`` / ``is_ready``: the node's stored readiness verdict.
- ``impl_of``: a capability node's stamped implementation (for secret
  resolution to reach a backend off the graph, not the live registry).

Every method is a pure read over the frozen node map; none recomputes: the
finalize fold stores each node's readiness verdict, and the projection surfaces,
site selection, and secret resolution all read it here rather than recomputing.

The contract in brief: the graph is frozen after ``finalize`` and every query
is a pure read of ``_nodes`` (no recomputation). Nodes are keyed by
``(kind, name)`` with exactly one node per published resource, including
capability rows, which carry their exact implementation class in ``impl`` for
every capability kind, so consumers reach a capability's code off the graph rather
than the live registry. ``readiness_of`` / ``enablement_of`` return stored
verdicts; ``edges_of`` / ``incoming_edges_of`` expose full edges in both
directions while ``dependents_of`` retains its reduced inbound projection;
``runtime_reachable_from`` and ``composed_from`` are the two transitive
closures. The retention was introduced by the 2026-07 registry-readiness
refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentworks.resources.reference import ReferenceEntry, RefRelationship, ResourceReference


class Enablement(Enum):
    """Whether a node is opted in (``enabled``) or opted out (``disabled``).

    Distinct from readiness: enablement is the operator's opt-in decision,
    readiness is whether the node can run on this host. Enablement is now
    PRODUCIBLE: ``finalize`` composes injected :data:`EnablementSource` callables
    (:func:`compose_enablement`) into a disabled-node mark map and derives this
    binary axis as its pure projection (a node is ``disabled`` iff a source marks
    it). The plugin opt-in source is the first such producer; a build with no
    sources yields all-``enabled``, exactly as before. The binary axis remains
    the sole authority for the fold's enabled/disabled branch; a mark only
    carries the disabling reason alongside it.
    """

    enabled = "enabled"
    disabled = "disabled"


@dataclass(frozen=True)
class DisabledMark:
    """One source's verdict that a node is disabled, carrying WHY and WHO.

    ``reason`` is the remediation clause a dependent's hint appends
    verbatim (e.g. ``"enable plugin `azure`"``), NOT the state phrasing;
    ``source`` is the source identity (e.g. ``"plugin-opt-in"``), retained
    for precedence and future "why is this disabled" surfaces. Transient:
    a mark rides the fold's :class:`DependencyState` and the binary
    projection ``finalize`` derives from it, and is never persisted on the
    frozen node (LLD b). A node ABSENT from a source's output is enabled as
    far as that source is concerned.
    """

    reason: str
    source: str


# An enablement source: a pure function from the present rows to the subset
# of ``(kind, name)`` nodes it disables, each with its :class:`DisabledMark`.
# This is the R13 multi-source seam: sources compose (see
# :func:`compose_enablement`), and each disabled verdict carries which source
# fired and its own reason. The ``Registry`` folds opaque source callables and
# never imports ``Config`` or ``plugins``; a config-bound source (the plugin
# opt-in source, LLD b) is constructed by ``build_registry`` and injected at
# ``finalize``.
type EnablementSource = Callable[
    [Mapping[str, Mapping[str, object]]],
    Mapping[tuple[str, str], DisabledMark],
]


def compose_enablement(
    sources: Sequence[EnablementSource],
    resources: Mapping[str, Mapping[str, object]],
) -> dict[tuple[str, str], DisabledMark]:
    """Fold a list of enablement sources into the disabled-node mark map.

    A node is disabled if ANY source disables it. When more than one source
    disables the same node, the FIRST source in the list wins its reason
    (``setdefault``); the list order is the assembly point's choice
    (``build_registry``, LLD c), so a future operator-explicit-disable source
    can be ordered ahead of the plugin source if operator intent should own the
    reason. No sources (the default) yields no marks, hence all-enabled: the
    exact behavior the landed refactor's ``_node_enablement()`` had.
    """
    marks: dict[tuple[str, str], DisabledMark] = {}
    for source in sources:
        for key, mark in source(resources).items():
            marks.setdefault(key, mark)  # first source in the list wins
    return marks


@dataclass(frozen=True)
class Readiness:
    """A node's readiness verdict: can it run on this host, and if not, why.

    ``reason is None`` means ready; a string is the operator-facing reason it is
    not. ``is_available`` distinguishes an observed not-ready verdict from a
    deliberately omitted host check. Construct through the named classmethods
    so call sites read as verdicts, not as ``str | None`` bookkeeping.
    """

    reason: str | None = None
    is_available: bool = True

    @property
    def is_ready(self) -> bool:
        return self.reason is None

    @classmethod
    def ready(cls) -> Readiness:
        return cls(None)

    @classmethod
    def blocked(cls, reason: str) -> Readiness:
        return cls(reason)

    @classmethod
    def unavailable(cls, reason: str) -> Readiness:
        """Return a non-ready verdict whose host check was deliberately omitted."""
        return cls(reason, is_available=False)


@dataclass(frozen=True)
class DependencyState:
    """What the fold hands a node about ONE of its dependencies: the dep's
    enablement, its readiness (when enabled), and its capability impl.

    ``readiness`` is ``None`` iff the dep is disabled (readiness is computed
    only for enabled nodes; enablement is the axis that answers for a disabled
    node). ``impl`` is the dependency's capability implementation class
    (for every capability kind),
    ``None`` for a non-capability dep), carried so the depending node can run a
    config-dependent capability check WITHOUT reaching into a live registry
    (the impl came from the graph, so this stays guard-clean, R11). See LLD c.
    """

    enablement: Enablement
    readiness: Readiness | None
    impl: object | None
    # The disabling source's remediation reason when this dep is disabled;
    # ``None`` otherwise. Defaulted, so every existing construction (tests
    # that build a ``DependencyState`` directly) still type-checks: the field
    # is purely additive, read only by a propagating ``not_ready`` hook.
    disabled_reason: str | None = None


@dataclass(frozen=True)
class _Node:
    """One node in the graph: a published resource keyed by ``(kind, name)``.

    ``outbound`` is this node's declared edges (full ``ResourceReference``s,
    which carry target kind/name, usage, and source). ``incoming`` retains the
    same full references on their target node. ``inbound`` is the reduced
    ``ReferenceEntry`` compatibility projection derived from those references
    and moved off the resource dataclass (caller inventory E).
    ``impl`` is the exact capability implementation class for capability nodes,
    ``None`` otherwise.
    """

    key: tuple[str, str]
    outbound: tuple[ResourceReference, ...]
    incoming: tuple[ResourceReference, ...]
    inbound: tuple[ReferenceEntry, ...]
    enablement: Enablement
    readiness: Readiness
    impl: object | None


@dataclass(frozen=True)
class DependencyGraph:
    """The retained, frozen dependency graph. Built by ``build_graph`` inside
    ``Registry.finalize`` and queried through the six read methods below.

    ``_nodes`` is a read-only mapping (``MappingProxyType``); the graph and its
    tuples are immutable after finalize.
    """

    _nodes: Mapping[tuple[str, str], _Node]

    def edges_of(self, kind: str, name: str) -> tuple[ResourceReference, ...]:
        """This node's outbound reference edges. Raises ``KeyError`` on an
        unknown key (callers hold canonical keys from ``iter_kind_items``)."""
        return self._nodes[(kind, name)].outbound

    def incoming_edges_of(self, kind: str, name: str) -> tuple[ResourceReference, ...]:
        """This node's full incoming reference edges. Raises ``KeyError`` on
        an unknown key, matching :meth:`edges_of` and :meth:`dependents_of`."""
        return self._nodes[(kind, name)].incoming

    def dependents_of(self, kind: str, name: str) -> tuple[ReferenceEntry, ...]:
        """Who references this node (its inbound ``ReferenceEntry`` list). The
        replacement for every ``getattr(resource, "references", ())`` reader.
        Raises ``KeyError`` on an unknown key."""
        return self._nodes[(kind, name)].inbound

    def runtime_reachable_from(self, kind: str, name: str) -> list[tuple[str, str]]:
        """The transitive closure over RUNTIME-NEED edges: what this node
        needs to have resolved for it to work.

        Crosses ``USES`` and stops at everything else, which today means
        it stops at inheritance (FR17). Inheritance is source composition:
        the parent's declaration is merged into this node's, and this node
        already publishes the merged result's needs as edges of its own,
        so crossing the edge as well would attribute the parent's
        STANDALONE needs (the secret name the child overrode, say) to the
        child and prompt for a secret it does not use.

        Excludes the start node, deduped, in first-encountered order. A
        graph-owned DFS over the frozen ``outbound`` edges, so consumers stop
        hand-rolling one. Cycle-safe via a visited set (the graph is acyclic
        after finalize's cycle pass, but this query may be called on a graph
        built before that pass in tests). Returns keys; callers resolve rows via
        ``registry.lookup``.
        """
        from agentworks.resources.reference import RefRelationship

        return self._closure(kind, name, crossing=frozenset({RefRelationship.USES}))

    def composed_from(self, kind: str, name: str) -> list[tuple[str, str]]:
        """The transitive closure over SOURCE-COMPOSITION edges: the
        declarations this node is assembled out of, nearest first.

        The other half of the FR17 split, and the answer to a different
        question: not "what does this need" but "whose declarations am I".
        Its caller is the recipe use-gate, which must refuse a lineage
        whose parent is turned off even though the child's own needs are
        all satisfiable.

        Same walk contract as :meth:`runtime_reachable_from`.
        """
        from agentworks.resources.reference import RefRelationship

        return self._closure(kind, name, crossing=frozenset({RefRelationship.INHERITS}))

    def _closure(
        self,
        kind: str,
        name: str,
        *,
        crossing: frozenset[RefRelationship],
    ) -> list[tuple[str, str]]:
        """The closure over the edges whose relationship is in ``crossing``.

        The set is EXPLICIT rather than a "everything but X" filter so a
        new ``RefRelationship`` cannot join a closure by default: it stays
        out of both until someone decides, and
        ``test_every_relationship_has_a_closure`` fails until they do.
        """
        start = (kind, name)
        visited: set[tuple[str, str]] = {start}
        ordered: list[tuple[str, str]] = []
        stack: list[tuple[str, str]] = [start]
        while stack:
            node = self._nodes.get(stack.pop())
            if node is None:
                continue
            for ref in node.outbound:
                if ref.relationship not in crossing:
                    continue
                target = (ref.kind, ref.name)
                if target not in visited:
                    visited.add(target)
                    ordered.append(target)
                    stack.append(target)
        return ordered

    def impl_of(self, kind: str, name: str) -> object | None:
        """The exact capability implementation class stamped on this node, or
        ``None`` for a non-capability node. The sanctioned
        way for a consumer (secret resolution, LLD d) to reach a capability's
        code off the graph rather than probing the live registry (R11). Raises
        ``KeyError`` on an unknown key."""
        return self._nodes[(kind, name)].impl

    def readiness_of(self, kind: str, name: str) -> Readiness:
        """The node's stored readiness verdict. Tolerates a missing node
        (returns a default-ready verdict), matching the projection surfaces'
        "absent means never disabled" tolerance."""
        node = self._nodes.get((kind, name))
        return node.readiness if node is not None else Readiness.ready()

    def enablement_of(self, kind: str, name: str) -> Enablement:
        """The node's opt-in axis (``enabled`` | ``disabled``). Distinct from
        readiness: a disabled node folds to a READY placeholder (enablement is
        the axis that answers for it, LLD c), so a consumer that must exclude a
        disabled unit (secret resolution's active chain, LLD d) reads this, not
        ``readiness_of``. Tolerates a missing node (returns ``enabled``, the
        every-node-is-enabled default this effort produces, R7)."""
        node = self._nodes.get((kind, name))
        return node.enablement if node is not None else Enablement.enabled

    def is_ready(self, kind: str, name: str) -> bool:
        """``readiness_of(...).is_ready``, for the many call sites that only
        branch on the boolean."""
        return self.readiness_of(kind, name).is_ready


@dataclass(frozen=True)
class FinalizeContext:
    """The controlled context finalize threads to each resource, at both of
    the passes that hand a resource control:
    :meth:`~agentworks.declared_resource.DeclaredResource.dependencies` in
    the build walk and
    :meth:`~agentworks.declared_resource.DeclaredResource.validate` in the
    validate pass.

    Named for the phase rather than for the build because it serves both:
    a resource that has to see its inheritance chain has to see it in both
    places (its edges and its shape check are two readings of one merged
    declaration), and there is nothing in it a build reads that a validate
    should not.

    Two load-bearing fields, both FRAMEWORK INPUTS supplied during
    finalize rather than a consumer reaching into a live registry, which is
    why the R11 guard whitelists the builder's own call (LLD b):

    - ``capability_classes``: immutable class projections for every
      class-by-name capability kind. Consuming resources select through this
      generic seam instead of receiving a kind-specific tuple.
    - ``rows``: every published resource, by kind and name, which an
      INHERITING resource reads to resolve its own ``inherits`` chain
      before emitting edges (FR17: a resource's runtime dependencies come
      from its EFFECTIVE declaration, so a child that overrides a parent's
      secret name depends on the override alone).

    A default-empty context (tests pass one) yields no present backends,
    so a secret under it emits only its explicit mapping-key edges, and no
    rows, so an inheriting resource under it resolves to its own
    declaration alone. See :meth:`rows_of` for why that degradation is the
    honest one.
    """

    capability_classes: Mapping[str, Mapping[str, type]] = MappingProxyType({})
    # The builder's live row map, read-only by contract (hence the
    # ``Mapping`` annotation): the rows do not change between the build
    # walk and the late-materialization walk that shares this context, and
    # the resources it holds are heterogeneous by kind, which is what the
    # ``Any`` value type says honestly.
    rows: Mapping[str, Mapping[str, Any]] = MappingProxyType({})

    def capability_class(self, kind: str, name: str) -> type | None:
        """The projected implementation class for ``kind``/``name``, or
        ``None`` for a name miss.

        An unregistered ``kind`` is refused rather than answered with
        ``None``, for the reason :meth:`rows_of` gives.
        """
        from agentworks.errors import StateError

        kinds = _capability_kinds()
        if kind not in kinds:
            raise StateError(
                f"no capability kind {kind!r} is registered, so it projects no implementation classes "
                f"(known capability kinds: {', '.join(sorted(kinds))})"
            )
        return self.capability_classes.get(kind, {}).get(name)

    def rows_of(self, kind: str) -> Mapping[str, Any]:
        """Every published resource of ``kind``, by name; empty when the
        kind has none.

        The seam an inheriting resource resolves its chain over. A caller
        merges ITSELF into the result before resolving
        (``{**context.rows_of(kind), self.name: self}``), which is a no-op
        during the real walk (the registry already holds it) and is what
        makes a bare ``FinalizeContext()`` degrade to "this declaration
        alone" rather than to "nothing at all": an empty answer for a
        template that declares an env secret would be a wrong answer, not
        an obviously-missing one.

        An unregistered ``kind`` is REFUSED rather than answered with an
        empty mapping, because that degradation is indistinguishable from
        the legitimate one above: a typo in a future emitter would quietly
        stop merging its own chain, and the row would go on publishing
        plausible edges off its declaration alone. The values stay ``Any``
        because the rows genuinely are heterogeneous (the registry holds
        every kind's own dataclass) and the caller narrows; the KEY is the
        part that can be wrong with nothing noticing.
        """
        from agentworks.errors import StateError
        from agentworks.resources.kind import KIND_REGISTRY

        if kind not in KIND_REGISTRY:
            raise StateError(
                f"no resource kind {kind!r} is registered, so there are no rows to resolve a chain over "
                f"(known kinds: {', '.join(sorted(KIND_REGISTRY))})"
            )
        return self.rows.get(kind, {})


def build_context(resources: Mapping[str, Mapping[str, object]]) -> FinalizeContext:
    """Assemble the :class:`FinalizeContext` the finalize walk threads to every
    resource's ``dependencies``.

    Reads each present ``secret-backend`` node's impl off the code registry via
    :func:`_impl_for` (the whitelisted builder-reads-registry path, R11 / LLD
    b), so a ``secret`` can ask ``would_attempt`` without a consumer-side
    registry probe. The backend nodes are published before finalize and never
    materialize later, so the context is assembled once at the start of the
    build.

    ``resources`` is carried through as ``rows`` rather than snapshotted:
    the late-materialization pass walks new nodes against this same
    context, and a snapshot would hand them a row map from before pass 0.
    Only inheriting kinds read it, and no template is ever materialized
    late (reserved defaults are seeded in pass 0), so the two views agree
    either way; carrying the live map means they cannot stop agreeing.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    projected: dict[str, Mapping[str, type]] = {}
    for descriptor in capability_descriptors():
        projected[descriptor.kind] = MappingProxyType(
            {name: cast("type", _impl_for(descriptor.kind, name)) for name in resources.get(descriptor.kind, {})}
        )
    return FinalizeContext(
        capability_classes=MappingProxyType(projected),
        rows=resources,
    )


def build_graph(
    resources: Mapping[str, Mapping[str, object]],
    all_refs: Mapping[tuple[str, str], Sequence[ResourceReference]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
    readiness: Mapping[tuple[str, str], Readiness] | None = None,
    enablement: Mapping[tuple[str, str], Enablement] | None = None,
) -> DependencyGraph:
    """Build the frozen ``DependencyGraph`` from the finalized resource map and
    the two accumulated edge maps.

    ``all_outbound`` is keyed by source in source-emission order (each
    resource's ``dependencies()`` appended contiguously during the finalize
    walk), so a node's ``outbound`` preserves first-encountered order per LLD
    (a). ``all_refs`` is keyed by target; ``incoming`` retains its full edges
    and ``inbound`` projects each edge to the compatibility ``ReferenceEntry``
    shape, both preserving that target's incoming order.

    ``readiness`` is the verdict map the fold (:func:`fold_readiness`) produced,
    keyed by node; a node absent from it is stored ``ready`` (a node the fold
    did not reach, e.g. in a test that builds a graph without folding).
    ``enablement`` is the per-node opt-in map; a node absent from it is stored
    ``enabled``. ``finalize`` derives it as a pure projection of the composed
    enablement-source marks (a node is ``disabled`` iff some injected source
    marks it), so a disabled node is producible now; a build with no sources
    yields all-``enabled``. This builder only STORES the projected axis (it
    never recomputes it); the finalize fold distributes a disabled dependency's
    state and its carried reason.
    """
    from agentworks.resources.reference import ReferenceEntry

    verdicts = readiness or {}
    opt_in = enablement or {}
    inbound: dict[tuple[str, str], list[ReferenceEntry]] = {}
    for target, refs in all_refs.items():
        for ref in refs:
            inbound.setdefault(target, []).append(
                ReferenceEntry(source=ref.source, usage=ref.usage, declared_by=ref.declared_by)
            )

    nodes: dict[tuple[str, str], _Node] = {}
    for kind, kind_dict in resources.items():
        for name in kind_dict:
            key = (kind, name)
            nodes[key] = _Node(
                key=key,
                outbound=tuple(all_outbound.get(key, ())),
                incoming=tuple(all_refs.get(key, ())),
                inbound=tuple(inbound.get(key, ())),
                enablement=opt_in.get(key, Enablement.enabled),
                readiness=verdicts.get(key, Readiness.ready()),
                impl=_impl_for(kind, name),
            )

    return DependencyGraph(_nodes=MappingProxyType(nodes))


# -- The readiness fold (LLD c) ---------------------------------------------


@cache
def _capability_kinds() -> frozenset[str]:
    """The kinds whose node readiness comes from their IMPL (a
    config-independent host-support check) rather than from a resource-level
    ``not_ready`` hook: the capability kinds, which the descriptor table
    enumerates.

    Cached and lazily collected for the same reason as
    :func:`_capability_registry_loaders`, which it shares its key set with by
    construction. The two stay separate accessors because they answer
    different questions of the table: which kinds the fold dispatches on, and
    where each kind's impl lives.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    return frozenset(descriptor.kind for descriptor in capability_descriptors())


# These capability kinds invoke host-readiness code. Guide registry builds
# consult this shared policy to suppress every such invocation. Keep the set
# next to the dispatch below until capability descriptors own the policy.
HOST_PROBING_CAPABILITY_KINDS = frozenset({"vm-platform", "secret-backend"})


@runtime_checkable
class _ReadinessResource(Protocol):
    """A consuming resource that self-determines its readiness from its
    dependencies' states (``vm-site`` and ``git-credential``). The fold
    dispatches on this structural shape, so a resource that opts out
    (implements no ``not_ready``) is simply always ready."""

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness: ...


def fold_readiness(
    resources: Mapping[str, Mapping[str, object]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
    enablement: Mapping[tuple[str, str], Enablement] | None = None,
    disabled_marks: Mapping[tuple[str, str], DisabledMark] | None = None,
    *,
    probe_host: bool = True,
) -> dict[tuple[str, str], Readiness]:
    """Compute each present node's readiness verdict, dependency-first.

    Walks the built edge map in reverse-topological order (dependencies before
    dependents; cycle detection has already guaranteed acyclicity) and, for each
    node, hands its ``not_ready`` hook the ``DependencyState`` of each of its
    ALREADY-FOLDED dependencies, storing the returned :class:`Readiness`. A
    dependency with no present node yet (a deferred auto-declare target not
    materialized until finalize pass 5) contributes no ``DependencyState`` (LLD
    c): a resource's ``not_ready`` indexes only the deps it declares interest in.

    ``enablement`` is the per-node opt-in map (a node absent from it is treated
    ``enabled``): the seam the plugin rebuild fills. A DISABLED dependency is
    handed to its dependents with ``readiness=None`` (enablement is the axis
    that answers for it, LLD c), so a propagating dependent (``vm-site``) reports
    the disabled hint. ``disabled_marks`` (defaulted to ``None``, treated empty)
    supplies the disabling reason for a disabled dependency, threaded onto its
    :class:`DependencyState.disabled_reason` so the propagating hint reads the
    remediation clause (e.g. "enable plugin `<name>`") rather than the generic
    "enable its unit" fallback; it never changes which node is disabled (that is
    ``enablement``'s job), so it is purely additive. The fold imposes NO
    propagation rule (R4): it only distributes states; each node's hook decides
    its own verdict. Returns the verdict map, which finalize hands to
    :func:`build_graph` and grows for late-materialized nodes.
    """
    present = {(kind, name) for kind, kind_dict in resources.items() for name in kind_dict}
    readiness: dict[tuple[str, str], Readiness] = {}
    for key in _reverse_topo_order(present, all_outbound):
        readiness[key] = node_readiness(
            key, resources, all_outbound, readiness, enablement, disabled_marks, probe_host=probe_host
        )
    return readiness


def node_readiness(
    key: tuple[str, str],
    resources: Mapping[str, Mapping[str, object]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
    readiness: Mapping[tuple[str, str], Readiness],
    enablement: Mapping[tuple[str, str], Enablement] | None = None,
    disabled_marks: Mapping[tuple[str, str], DisabledMark] | None = None,
    *,
    probe_host: bool = True,
) -> Readiness:
    """One node's readiness verdict, given the verdicts of its dependencies.

    A DISABLED node's readiness is not computed (enablement answers for it, LLD
    c); a ready placeholder is stored, and the node is instead handed to its
    dependents with ``readiness=None``. An enabled capability node computes from
    its impl (config-independent host support, LLD c's table). An enabled
    consuming resource with a ``not_ready(deps)`` hook (``vm-site`` and
    ``git-credential``) is handed its dependencies' ``DependencyState``s and
    decides for itself. Any other node (a secret, a template, a resource that
    opts out) is ready. Exposed (not just the fold's inner loop) so finalize's
    materialize pass can fold a single late-materialized node the same way.
    ``disabled_marks`` (defaulted, treated empty) supplies the disabling reason
    threaded onto a disabled dependency's ``DependencyState.disabled_reason``;
    omitting it (a direct test call) leaves the reason ``None`` and a
    propagating hook reads its generic fallback, so the parameter is additive.
    """
    opt_in = enablement or {}
    if opt_in.get(key, Enablement.enabled) is Enablement.disabled:
        return Readiness.ready()  # placeholder; enablement answers for a disabled node
    kind, name = key
    if kind in _capability_kinds():
        if not probe_host and kind in HOST_PROBING_CAPABILITY_KINDS:
            return Readiness.unavailable("host readiness unavailable: guide does not inspect the workstation")
        return _capability_node_readiness(kind, name)
    resource = resources[kind][name]
    if not isinstance(resource, _ReadinessResource):
        return Readiness.ready()
    if not probe_host:
        return Readiness.unavailable("host readiness unavailable: guide does not inspect the workstation")
    deps: dict[tuple[str, str], DependencyState] = {}
    for ref in all_outbound.get(key, ()):
        target = (ref.kind, ref.name)
        if target not in readiness:
            # Not a present, folded node (a deferred auto-declare target). The
            # hook indexes only the deps it cares about, so a skipped one it
            # does not consult never becomes a missing key (LLD c).
            continue
        dep_enabled = opt_in.get(target, Enablement.enabled) is Enablement.enabled
        mark = None if dep_enabled else (disabled_marks or {}).get(target)
        deps[target] = DependencyState(
            enablement=Enablement.enabled if dep_enabled else Enablement.disabled,
            readiness=readiness[target] if dep_enabled else None,
            impl=_impl_for(*target),
            disabled_reason=mark.reason if mark is not None else None,
        )
    return resource.not_ready(deps)


def _capability_node_readiness(kind: str, name: str) -> Readiness:
    """A capability node's own readiness: its impl's config-independent
    host-support check (LLD c's table).

    The verdict is the kind's own, so it comes from the kind's descriptor
    rather than from an if-chain here. Each ``readiness`` callable lives
    beside the capability it interrogates: ``vm-platform`` wraps
    ``unsupported_reason`` into the host-support sentence, ``secret-backend``
    asks the backend class, and ``harness-integration`` /
    ``git-credential-provider`` have no host-support concept and are always
    ready. The fold's job is to know WHEN to ask, not WHAT the answer is.

    Distinct from the config-dependent ``Capability.not_ready(config)`` a
    CONSUMING resource (``vm-site``) uses; this is the capability node's own
    verdict, which such a consumer then propagates.
    """
    from agentworks.capabilities.descriptor import descriptor_for

    return descriptor_for(kind).readiness(name, _impl_for(kind, name))


def _reverse_topo_order(
    present: set[tuple[str, str]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
) -> list[tuple[str, str]]:
    """Present nodes in reverse-topological order (dependencies before
    dependents): an iterative DFS post-order over ``all_outbound``, following
    only edges to present targets. The graph is acyclic (cycle detection ran
    first); a visited set makes this defensively cycle-safe regardless.
    """
    visited: set[tuple[str, str]] = set()
    order: list[tuple[str, str]] = []
    for root in present:
        if root in visited:
            continue
        # Each stack frame is (node, whether its children have been pushed).
        stack: list[tuple[tuple[str, str], bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for ref in all_outbound.get(node, ()):
                target = (ref.kind, ref.name)
                if target in present and target not in visited:
                    stack.append((target, False))
    return order


def _impl_for(kind: str, name: str) -> object | None:
    """Return the capability implementation the graph node carries, or ``None``
    for non-capability kinds.

    WHITELISTED BUILDER EXEMPTION (R11 / LLD b guard): the capability ``Entry``
    rows do not carry their impl, so the graph *builder* reads the code registry
    to stamp it onto each capability node here. This is the sanctioned
    builder-reads-registry path, distinct from (and not to be confused with) a
    *consumer* probing the live registry at op time, which the guard bans. The
    impl is the exact class stored in the kind's registry.

    A kind with no descriptor is not a capability kind, which is what makes the
    ``None`` return total: the table IS the capability-kind enumeration.

    A published capability row whose name has no registered implementation is a
    framework/publisher invariant violation (the capability resources mirror the
    code registry), not a config error, so this fails fast with ``StateError``
    rather than storing ``None`` and deferring a confusing ``AttributeError`` to
    the phase-3 fold or phase-4 resolution. The build stays total for malformed
    *config* (R1); this is a different failure class.
    """
    registry = _capability_registry_loaders().get(kind)
    if registry is None:
        return None
    impl = registry().get(name)
    if impl is None:
        from agentworks.errors import StateError

        raise StateError(f"{kind} row {name!r} has a registry row but no registered implementation")
    return impl


@cache
def _capability_registry_loaders() -> Mapping[str, Callable[[], Mapping[str, object]]]:
    """Each capability kind's lazy code-registry accessor, which IS its
    descriptor's ``registry`` field.

    Everything here stays lazy for the same reason it always was: this module
    is imported by ``agentworks.resources`` before the capability packages, so
    neither the table nor the registries may be reached at module load. The
    descriptor table is collected on first call and cached; each value is
    still a callable that imports its own registry when invoked.

    Read-only, because the cache means every caller for the life of the
    process shares this one object: a plain dict here would let any of them
    reshape the loader map for all the others.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    return MappingProxyType({descriptor.kind: descriptor.registry for descriptor in capability_descriptors()})
