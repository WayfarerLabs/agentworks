"""``DependencyGraph``: the framework's retained, queryable dependency graph.

``Registry.finalize`` builds one of these from the reference walk it already
runs and hands it to the ``Registry`` to retain (``Registry.graph``). The graph
is the single access path for structural questions the codebase previously
answered by re-walking ``dependencies(context)`` or reading a ``references``
field off each resource dataclass:

- ``edges_of``: a node's outbound reference edges (who it points at).
- ``dependents_of``: a node's inbound references (who points at it), the
  replacement for the removed per-resource ``references`` field.
- ``reachable_from``: the transitive closure of ``edges_of`` from a node.
- ``readiness_of`` / ``is_ready``: the node's stored readiness verdict.
- ``impl_of``: a capability node's stamped implementation (for secret
  resolution to reach a backend off the graph, not the live registry).

Every method is a pure read over the frozen node map; none recomputes: the
finalize fold stores each node's readiness verdict, and the projection surfaces,
site selection, and secret resolution all read it here rather than recomputing.

See ``docs/sdd/2026-07-27-registry-readiness-refactor/graph-lld.md`` (LLD a) for
the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.resources.reference import ReferenceEntry, ResourceReference
    from agentworks.secrets.backends import SecretBackend


class Enablement(Enum):
    """Whether a node is opted in (``enabled``) or opted out (``disabled``).

    Distinct from readiness: enablement is the operator's opt-in decision,
    readiness is whether the node can run on this host. Every node this effort
    produces is ``enabled``; the axis is modeled so the plugin rebuild can
    produce ``disabled`` without re-touching the core.
    """

    enabled = "enabled"
    disabled = "disabled"


@dataclass(frozen=True)
class Readiness:
    """A node's readiness verdict: can it run on this host, and if not, why.

    ``reason is None`` means ready; a string is the operator-facing reason it is
    not. Construct via ``Readiness.ready()`` / ``Readiness.blocked(reason)``
    rather than the raw constructor so call sites read as verdicts, not as
    ``str | None`` bookkeeping (the double-negative ``readiness_of`` exists to
    spare consumers, R10).
    """

    reason: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.reason is None

    @classmethod
    def ready(cls) -> Readiness:
        return cls(None)

    @classmethod
    def blocked(cls, reason: str) -> Readiness:
        return cls(reason)


@dataclass(frozen=True)
class DependencyState:
    """What the fold hands a node about ONE of its dependencies: the dep's
    enablement, its readiness (when enabled), and its capability impl.

    ``readiness`` is ``None`` iff the dep is disabled (readiness is computed
    only for enabled nodes; enablement is the axis that answers for a disabled
    node). ``impl`` is the dependency's capability implementation (a class for
    vm-platform/harness/git-credential-provider, an instance for secret-backend,
    ``None`` for a non-capability dep), carried so the depending node can run a
    config-dependent capability check WITHOUT reaching into a live registry
    (the impl came from the graph, so this stays guard-clean, R11). See LLD c.
    """

    enablement: Enablement
    readiness: Readiness | None
    impl: object | None


@dataclass(frozen=True)
class _Node:
    """One node in the graph: a published resource keyed by ``(kind, name)``.

    ``outbound`` is this node's declared edges (full ``ResourceReference``s,
    which carry target kind/name, usage, and source). ``inbound`` is the
    ``ReferenceEntry`` list of who points at this node, derived from the same
    edge walk and moved off the resource dataclass (caller inventory E).
    ``impl`` is the capability implementation for capability nodes (a class for
    vm-platform/harness/git-credential-provider, an instance for
    secret-backend), ``None`` otherwise.
    """

    key: tuple[str, str]
    outbound: tuple[ResourceReference, ...]
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

    def dependents_of(self, kind: str, name: str) -> tuple[ReferenceEntry, ...]:
        """Who references this node (its inbound ``ReferenceEntry`` list). The
        replacement for every ``getattr(resource, "references", ())`` reader.
        Raises ``KeyError`` on an unknown key."""
        return self._nodes[(kind, name)].inbound

    def reachable_from(self, kind: str, name: str) -> list[tuple[str, str]]:
        """The transitive closure of ``outbound`` from this node: every node
        reachable by following edges, excluding the start node, deduped, in
        first-encountered order.

        A graph-owned DFS over the frozen ``outbound`` edges, so consumers stop
        hand-rolling one. Cycle-safe via a visited set (the graph is acyclic
        after finalize's cycle pass, but this query may be called on a graph
        built before that pass in tests). Returns keys; callers resolve rows via
        ``registry.lookup``.
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
                target = (ref.kind, ref.name)
                if target not in visited:
                    visited.add(target)
                    ordered.append(target)
                    stack.append(target)
        return ordered

    def impl_of(self, kind: str, name: str) -> object | None:
        """The capability implementation stamped on this node (a class for
        vm-platform/harness/git-credential-provider, an instance for
        secret-backend), or ``None`` for a non-capability node. The sanctioned
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

    def is_ready(self, kind: str, name: str) -> bool:
        """``readiness_of(...).is_ready``, for the many call sites that only
        branch on the boolean."""
        return self.readiness_of(kind, name).is_ready


@dataclass(frozen=True)
class BuildContext:
    """The controlled context the graph builder threads to each resource's
    :meth:`~agentworks.declared_resource.DeclaredResource.dependencies` during
    the finalize walk.

    Today its one load-bearing field is ``available_backends``: the present
    ``secret-backend`` nodes (name + impl), which a ``secret`` reads to decide
    (via each backend's pure ``would_attempt``) which ``secret -> secret-backend``
    edges to emit. Every other resource ignores the context. This is a BUILDER
    INPUT supplied during the build, not a consumer reaching into a live
    registry, so the R11 guard whitelists the builder's own call (LLD b). A
    default-empty context (the greenness-scaffold alias passes one) yields no
    present backends, so a secret under it emits only its explicit mapping-key
    edges.
    """

    available_backends: tuple[tuple[str, SecretBackend], ...] = ()


def build_context(resources: Mapping[str, Mapping[str, object]]) -> BuildContext:
    """Assemble the :class:`BuildContext` the finalize walk threads to every
    resource's ``dependencies``.

    Reads each present ``secret-backend`` node's impl off the code registry via
    :func:`_impl_for` (the whitelisted builder-reads-registry path, R11 / LLD
    b), so a ``secret`` can ask ``would_attempt`` without a consumer-side
    registry probe. The backend nodes are published before finalize and never
    materialize later, so the context is assembled once at the start of the
    build.
    """
    backends = tuple(
        (name, cast("SecretBackend", _impl_for("secret-backend", name))) for name in resources.get("secret-backend", {})
    )
    return BuildContext(available_backends=backends)


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
    (a). ``all_refs`` is keyed by target; ``inbound`` projects each edge to the
    inbound ``ReferenceEntry`` on its target (the logic that lived in
    ``registry._references_tuple``), preserving that target's incoming order.

    ``readiness`` is the verdict map the fold (:func:`fold_readiness`) produced,
    keyed by node; a node absent from it is stored ``ready`` (a node the fold
    did not reach, e.g. in a test that builds a graph without folding).
    ``enablement`` is the per-node opt-in map; a node absent from it is stored
    ``enabled``. Every node this effort produces is ``enabled`` (no producer of
    disabled nodes ships, R7); the map is the seam the plugin rebuild fills, and
    the finalize fold already distributes a disabled dependency's state.
    """
    from agentworks.resources.reference import ReferenceEntry

    verdicts = readiness or {}
    opt_in = enablement or {}
    inbound: dict[tuple[str, str], list[ReferenceEntry]] = {}
    for target, refs in all_refs.items():
        for ref in refs:
            inbound.setdefault(target, []).append(ReferenceEntry(source=ref.source, usage=ref.usage))

    nodes: dict[tuple[str, str], _Node] = {}
    for kind, kind_dict in resources.items():
        for name in kind_dict:
            key = (kind, name)
            nodes[key] = _Node(
                key=key,
                outbound=tuple(all_outbound.get(key, ())),
                inbound=tuple(inbound.get(key, ())),
                enablement=opt_in.get(key, Enablement.enabled),
                readiness=verdicts.get(key, Readiness.ready()),
                impl=_impl_for(kind, name),
            )

    return DependencyGraph(_nodes=MappingProxyType(nodes))


# -- The readiness fold (LLD c) ---------------------------------------------

# The four capability kinds whose node readiness comes from their impl (a
# config-independent host-support check), not from a resource-level
# ``not_ready`` hook. The fold dispatches per kind here because the impl is
# heterogeneous (a class for platform/harness/provider, an instance for
# secret-backend) and each kind's host-support source differs (LLD c's table).
_CAPABILITY_KINDS = frozenset(
    {"vm-platform", "harness", "git-credential-provider", "secret-backend"},
)


@runtime_checkable
class _ReadinessResource(Protocol):
    """A consuming resource that self-determines its readiness from its
    dependencies' states (today only ``vm-site``). The fold dispatches on this
    structural shape, so a resource that opts out (implements no ``not_ready``)
    is simply always ready."""

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness: ...


def fold_readiness(
    resources: Mapping[str, Mapping[str, object]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
    enablement: Mapping[tuple[str, str], Enablement] | None = None,
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
    the "enable its unit" hint. The fold imposes NO propagation rule (R4): it
    only distributes states; each node's hook decides its own verdict. Returns
    the verdict map, which finalize hands to :func:`build_graph` and grows for
    late-materialized nodes.
    """
    present = {(kind, name) for kind, kind_dict in resources.items() for name in kind_dict}
    readiness: dict[tuple[str, str], Readiness] = {}
    for key in _reverse_topo_order(present, all_outbound):
        readiness[key] = node_readiness(key, resources, all_outbound, readiness, enablement)
    return readiness


def node_readiness(
    key: tuple[str, str],
    resources: Mapping[str, Mapping[str, object]],
    all_outbound: Mapping[tuple[str, str], Sequence[ResourceReference]],
    readiness: Mapping[tuple[str, str], Readiness],
    enablement: Mapping[tuple[str, str], Enablement] | None = None,
) -> Readiness:
    """One node's readiness verdict, given the verdicts of its dependencies.

    A DISABLED node's readiness is not computed (enablement answers for it, LLD
    c); a ready placeholder is stored, and the node is instead handed to its
    dependents with ``readiness=None``. An enabled capability node computes from
    its impl (config-independent host support, LLD c's table). An enabled
    consuming resource with a ``not_ready(deps)`` hook (today only ``vm-site``)
    is handed its dependencies' ``DependencyState``s and decides for itself. Any
    other node (a secret, a template, a resource that opts out) is ready.
    Exposed (not just the fold's inner loop) so finalize's materialize pass can
    fold a single late-materialized node the same way.
    """
    opt_in = enablement or {}
    if opt_in.get(key, Enablement.enabled) is Enablement.disabled:
        return Readiness.ready()  # placeholder; enablement answers for a disabled node
    kind, name = key
    if kind in _CAPABILITY_KINDS:
        return _capability_node_readiness(kind, name)
    resource = resources[kind][name]
    if not isinstance(resource, _ReadinessResource):
        return Readiness.ready()
    deps: dict[tuple[str, str], DependencyState] = {}
    for ref in all_outbound.get(key, ()):
        target = (ref.kind, ref.name)
        if target not in readiness:
            # Not a present, folded node (a deferred auto-declare target). The
            # hook indexes only the deps it cares about, so a skipped one it
            # does not consult never becomes a missing key (LLD c).
            continue
        dep_enabled = opt_in.get(target, Enablement.enabled) is Enablement.enabled
        deps[target] = DependencyState(
            enablement=Enablement.enabled if dep_enabled else Enablement.disabled,
            readiness=readiness[target] if dep_enabled else None,
            impl=_impl_for(*target),
        )
    return resource.not_ready(deps)


def _capability_node_readiness(kind: str, name: str) -> Readiness:
    """A capability node's own readiness: its impl's config-independent
    host-support check (LLD c's table). ``vm-platform`` wraps
    ``unsupported_reason``; ``secret-backend`` asks the backend instance;
    ``harness`` / ``git-credential-provider`` have no host-support concept and
    are always ready.
    """
    impl = _impl_for(kind, name)
    if kind == "vm-platform":
        reason = cast("type[VMPlatform]", impl).unsupported_reason()
        # Store the BARE host-support reason (e.g. "Windows only"); the vm-site
        # that depends on it wraps it into its own operator string, and the
        # platform row's own projection renders it directly. The readiness
        # vocabulary rename (R9.1) is a later phase; today's strings hold.
        return Readiness.ready() if reason is None else Readiness.blocked(reason)
    if kind == "secret-backend":
        return cast("SecretBackend", impl).not_ready()
    # harness, git-credential-provider: no host-support, no override.
    return Readiness.ready()


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
    impl is heterogeneous by design (a class for platform/harness/provider, an
    instance for secret-backend); the node just stores whatever the kind's
    registry holds.

    A published capability row whose name has no registered implementation is a
    framework/publisher invariant violation (the capability resources mirror the
    code registry), not a config error, so this fails fast with ``StateError``
    rather than storing ``None`` and deferring a confusing ``AttributeError`` to
    the phase-3 fold or phase-4 resolution. The build stays total for malformed
    *config* (R1); this is a different failure class.
    """
    registry = _CAPABILITY_REGISTRY_LOADERS.get(kind)
    if registry is None:
        return None
    impl = registry().get(name)
    if impl is None:
        from agentworks.errors import StateError

        raise StateError(f"{kind} row {name!r} has a registry row but no registered implementation")
    return impl


def _load_vm_platform_registry() -> Mapping[str, object]:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    return VM_PLATFORM_REGISTRY


def _load_harness_registry() -> Mapping[str, object]:
    from agentworks.capabilities.harness import HARNESS_REGISTRY

    return HARNESS_REGISTRY


def _load_git_credential_provider_registry() -> Mapping[str, object]:
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

    return GIT_CREDENTIAL_PROVIDER_REGISTRY


def _load_secret_backend_registry() -> Mapping[str, object]:
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    return SECRET_BACKEND_REGISTRY


# The four capability kinds and the (lazily-imported) code registry each reads
# its impl from. Lazy loaders avoid an import cycle at module load: this module
# is imported by ``agentworks.resources`` before the capability packages.
_CAPABILITY_REGISTRY_LOADERS: dict[str, Callable[[], Mapping[str, object]]] = {
    "vm-platform": _load_vm_platform_registry,
    "harness": _load_harness_registry,
    "git-credential-provider": _load_git_credential_provider_registry,
    "secret-backend": _load_secret_backend_registry,
}
