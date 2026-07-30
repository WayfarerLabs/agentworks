"""``DependencyGraph``: the framework's retained, queryable dependency graph.

``Registry.finalize`` builds one of these from the reference walk it already
runs and hands it to the ``Registry`` to retain (``Registry.graph``). The graph
is the single access path for structural questions the codebase previously
answered by re-walking ``referenced_resources()`` or reading a ``references``
field off each resource dataclass:

- ``edges_of`` -- a node's outbound reference edges (who it points at).
- ``dependents_of`` -- a node's inbound references (who points at it), the
  replacement for the removed per-resource ``references`` field.
- ``reachable_from`` -- the transitive closure of ``edges_of`` from a node.
- ``readiness_of`` / ``is_ready`` -- the node's stored readiness verdict.

Every method is a pure read over the frozen node map; none recomputes. The
readiness fold (a later phase) fills real verdicts; until then every node is
constructed ``ready`` / ``enabled``, which is correct because nothing computes
readiness yet.

See ``docs/sdd/2026-07-27-registry-readiness-refactor/graph-lld.md`` (LLD a) for
the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.resources.reference import ReferenceEntry, ResourceReference


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
    ``Registry.finalize`` and queried through the five read methods below.

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


def build_graph(
    resources: Mapping[str, Mapping[str, object]],
    all_refs: Mapping[tuple[str, str], Sequence[ResourceReference]],
) -> DependencyGraph:
    """Build the frozen ``DependencyGraph`` from the finalized resource map and
    the accumulated reference map.

    ``all_refs`` is keyed by target (as ``Registry.finalize`` accumulates it);
    ``outbound`` re-keys it by source and ``inbound`` projects each edge to the
    inbound ``ReferenceEntry`` on its target (the logic that lived in
    ``registry._references_tuple``). Every node is constructed ``enabled`` /
    ``ready`` this effort; the readiness fold fills real verdicts later.
    """
    from agentworks.resources.reference import ReferenceEntry

    outbound: dict[tuple[str, str], list[ResourceReference]] = {}
    inbound: dict[tuple[str, str], list[ReferenceEntry]] = {}
    for target, refs in all_refs.items():
        for ref in refs:
            outbound.setdefault(ref.source, []).append(ref)
            inbound.setdefault(target, []).append(ReferenceEntry(source=ref.source, usage=ref.usage))

    nodes: dict[tuple[str, str], _Node] = {}
    for kind, kind_dict in resources.items():
        for name in kind_dict:
            key = (kind, name)
            nodes[key] = _Node(
                key=key,
                outbound=tuple(outbound.get(key, ())),
                inbound=tuple(inbound.get(key, ())),
                enablement=Enablement.enabled,
                readiness=Readiness.ready(),
                impl=_impl_for(kind, name),
            )

    return DependencyGraph(_nodes=MappingProxyType(nodes))


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
    registry holds. ``.get`` (not ``[name]``) keeps the builder total: a
    capability row with no registry entry gets ``impl=None`` rather than
    crashing the build.
    """
    if kind == "vm-platform":
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        return VM_PLATFORM_REGISTRY.get(name)
    if kind == "harness":
        from agentworks.capabilities.harness import HARNESS_REGISTRY

        return HARNESS_REGISTRY.get(name)
    if kind == "git-credential-provider":
        from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

        return GIT_CREDENTIAL_PROVIDER_REGISTRY.get(name)
    if kind == "secret-backend":
        from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

        return SECRET_BACKEND_REGISTRY.get(name)
    return None
