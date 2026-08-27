"""Deterministic resource-graph queries and closed result projections."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError, StateError
from agentworks.resources.access import ResourceIdentity, resolve_resource
from agentworks.resources.reference import RefRelationship, ResourceReference

if TYPE_CHECKING:
    from agentworks.machine_output import JsonObject
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.registry import Registry


class GraphDirection(StrEnum):
    DEPENDENCIES = "dependencies"
    DEPENDENTS = "dependents"
    BOTH = "both"


class GraphNodeType(StrEnum):
    RESOURCE = "resource"
    LIVE_INSTANCE = "live-instance"


class GraphEdgeType(StrEnum):
    DECLARED = "declared"
    LIVE_USAGE = "live-usage"


@dataclass(frozen=True, slots=True)
class GraphIdentity:
    node_type: GraphNodeType
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class GraphQuery:
    focus: ResourceIdentity
    direction: GraphDirection
    depth_limit: int | None


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_type: GraphNodeType
    kind: str
    name: str
    distance: int


GRAPH_TRAVERSED_RELATIONSHIPS: frozenset[RefRelationship] = frozenset(
    {
        RefRelationship.USES,
        RefRelationship.INHERITS,
    }
)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_type: GraphEdgeType
    source: GraphIdentity
    target: GraphIdentity
    relationship: RefRelationship
    usage: str | None
    declared_by: ResourceIdentity | None

    def __post_init__(self) -> None:
        if self.edge_type is GraphEdgeType.DECLARED:
            valid = (
                self.source.node_type is GraphNodeType.RESOURCE
                and isinstance(self.relationship, RefRelationship)
                and self.usage is not None
            )
        else:
            valid = (
                self.source.node_type is GraphNodeType.LIVE_INSTANCE
                and self.relationship is RefRelationship.USES
                and self.usage is None
                and self.declared_by is None
            )
        if not valid:
            raise ValueError(f"invalid {self.edge_type.value} graph edge")


@dataclass(frozen=True, slots=True)
class GraphResult:
    query: GraphQuery
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


@dataclass(frozen=True, slots=True)
class GraphDistanceGroup:
    distance: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


@dataclass(frozen=True, slots=True)
class FocusedGraphFacts:
    """Direct declared relationships and current live usage for one resource."""

    dependencies: tuple[GraphEdge, ...]
    dependents: tuple[GraphEdge, ...]
    used_by: tuple[InstanceRef, ...] | None


def resource_graph_identity(value: ResourceIdentity) -> GraphIdentity:
    return GraphIdentity(GraphNodeType.RESOURCE, value.kind, value.name)


def node_type_rank(value: GraphNodeType) -> int:
    return 0 if value is GraphNodeType.RESOURCE else 1


def identity_key(value: GraphIdentity) -> tuple[int, str, str]:
    return (node_type_rank(value.node_type), value.kind, value.name)


def nullable_text_key(value: str | None) -> tuple[int, str]:
    return (0, "") if value is None else (1, value)


def nullable_resource_key(value: ResourceIdentity | None) -> tuple[int, str, str]:
    return (0, "", "") if value is None else (1, value.kind, value.name)


def edge_fact_key(edge: GraphEdge) -> tuple[object, ...]:
    return (
        edge.edge_type.value,
        identity_key(edge.source),
        identity_key(edge.target),
        edge.relationship.value,
        nullable_text_key(edge.usage),
        nullable_resource_key(edge.declared_by),
    )


def reference_graph_edge(registry: Registry, ref: ResourceReference) -> GraphEdge:
    """Project one retained reference with both nodes' public types."""
    target_type = GraphNodeType.LIVE_INSTANCE if registry.graph.is_live(ref.kind, ref.name) else GraphNodeType.RESOURCE
    if registry.graph.is_live(*ref.source):
        return _live_usage_edge(
            GraphIdentity(GraphNodeType.LIVE_INSTANCE, ref.source[0], ref.source[1]),
            GraphIdentity(target_type, ref.kind, ref.name),
        )
    declared_by = None
    if ref.declared_by is not None:
        declared_by = ResourceIdentity(ref.declared_by[0], ref.declared_by[1])
    return GraphEdge(
        edge_type=GraphEdgeType.DECLARED,
        source=GraphIdentity(GraphNodeType.RESOURCE, ref.source[0], ref.source[1]),
        target=GraphIdentity(target_type, ref.kind, ref.name),
        relationship=ref.relationship,
        usage=ref.usage,
        declared_by=declared_by,
    )


def _live_usage_edge(source: GraphIdentity, target: GraphIdentity) -> GraphEdge:
    return GraphEdge(
        edge_type=GraphEdgeType.LIVE_USAGE,
        source=source,
        target=target,
        relationship=RefRelationship.USES,
        usage=None,
        declared_by=None,
    )


def _neighbor_candidate_key(value: tuple[GraphIdentity, GraphEdge]) -> tuple[object, ...]:
    neighbor, edge = value
    return (*identity_key(neighbor), *edge_fact_key(edge))


def focused_graph_facts(
    registry: Registry,
    focus: ResourceIdentity,
) -> FocusedGraphFacts:
    """Return only declared edges touching ``focus`` and its current live users."""
    if not registry.is_finalized:
        raise StateError("graph queries require a finalized registry", entity_kind="registry")
    resolve_resource(registry, focus)
    focus_graph = resource_graph_identity(focus)

    dependencies = tuple(
        sorted(
            (reference_graph_edge(registry, ref) for ref in registry.graph.edges_of(focus.kind, focus.name)),
            key=edge_fact_key,
        )
    )
    dependents = tuple(
        sorted(
            (reference_graph_edge(registry, ref) for ref in registry.graph.incoming_edges_of(focus.kind, focus.name)),
            key=edge_fact_key,
        )
    )
    if any(edge.source != focus_graph for edge in dependencies):
        raise AssertionError("a focused dependency does not start at the focus")
    if any(edge.target != focus_graph for edge in dependents):
        raise AssertionError("a focused dependent does not end at the focus")

    return FocusedGraphFacts(
        dependencies,
        dependents,
        registry.graph.compatibility_live_users_of(focus.kind, focus.name),
    )


def show_graph(
    registry: Registry,
    focus: ResourceIdentity,
    direction: GraphDirection,
    depth_limit: int | None,
) -> GraphResult:
    """Return the deterministic fact graph reachable from one resource."""
    if not registry.is_finalized:
        raise StateError("graph queries require a finalized registry", entity_kind="registry")
    try:
        registry.lookup(focus.kind, focus.name)
    except KeyError:
        from agentworks.resources.live import LIVE_RESOURCE_KINDS

        if focus.kind in LIVE_RESOURCE_KINDS:
            raise NotFoundError(
                f"{focus.kind} {focus.name!r} not found",
                entity_kind=focus.kind,
                entity_name=focus.name,
            ) from None
        resolve_resource(registry, focus)
        raise AssertionError("resolve_resource returned for a missing graph focus") from None
    focus_is_live = registry.graph.is_live(focus.kind, focus.name)
    if not focus_is_live:
        resolve_resource(registry, focus)
    query = GraphQuery(focus, direction, depth_limit)
    focus_id = GraphIdentity(
        GraphNodeType.LIVE_INSTANCE if focus_is_live else GraphNodeType.RESOURCE,
        focus.kind,
        focus.name,
    )
    distance = {focus_id: 0}
    queue = deque([focus_id])
    edges_by_key: dict[tuple[object, ...], GraphEdge] = {}

    while queue:
        current = queue.popleft()
        current_distance = distance[current]
        if depth_limit is not None and current_distance >= depth_limit:
            continue

        candidates: list[tuple[GraphIdentity, GraphEdge]] = []

        if direction in {GraphDirection.DEPENDENCIES, GraphDirection.BOTH}:
            for ref in registry.graph.edges_of(current.kind, current.name):
                if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                    edge = reference_graph_edge(registry, ref)
                    candidates.append((edge.target, edge))

        if direction in {GraphDirection.DEPENDENTS, GraphDirection.BOTH}:
            for ref in registry.graph.incoming_edges_of(current.kind, current.name):
                if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                    edge = reference_graph_edge(registry, ref)
                    candidates.append((edge.source, edge))

        for neighbor, edge in sorted(candidates, key=_neighbor_candidate_key):
            edges_by_key.setdefault(edge_fact_key(edge), edge)
            if neighbor in distance:
                continue
            distance[neighbor] = current_distance + 1
            queue.append(neighbor)

    reached = set(distance)
    for source in sorted(reached, key=identity_key):
        for ref in registry.graph.edges_of(source.kind, source.name):
            if ref.relationship not in GRAPH_TRAVERSED_RELATIONSHIPS:
                continue
            edge = reference_graph_edge(registry, ref)
            if edge.target in reached:
                edges_by_key.setdefault(edge_fact_key(edge), edge)

    nodes = tuple(
        sorted(
            (
                GraphNode(identity.node_type, identity.kind, identity.name, node_distance)
                for identity, node_distance in distance.items()
            ),
            key=lambda node: (node.distance, node_type_rank(node.node_type), node.kind, node.name),
        )
    )

    def edge_order(edge: GraphEdge) -> tuple[object, ...]:
        return (
            max(distance[edge.source], distance[edge.target]),
            identity_key(edge.source),
            identity_key(edge.target),
            edge.edge_type.value,
            edge.relationship.value,
            nullable_text_key(edge.usage),
            nullable_resource_key(edge.declared_by),
        )

    return GraphResult(query, nodes, tuple(sorted(edges_by_key.values(), key=edge_order)))


def group_graph_result(result: GraphResult) -> tuple[GraphDistanceGroup, ...]:
    """Group canonically ordered facts by their first visible distance."""
    distances = {GraphIdentity(node.node_type, node.kind, node.name): node.distance for node in result.nodes}
    nodes_by_distance: dict[int, list[GraphNode]] = {}
    edges_by_distance: dict[int, list[GraphEdge]] = {}
    for node in result.nodes:
        nodes_by_distance.setdefault(node.distance, []).append(node)
    for edge in result.edges:
        edge_distance = max(distances[edge.source], distances[edge.target])
        edges_by_distance.setdefault(edge_distance, []).append(edge)
    return tuple(
        GraphDistanceGroup(
            distance=distance,
            nodes=tuple(nodes_by_distance.get(distance, ())),
            edges=tuple(edges_by_distance.get(distance, ())),
        )
        for distance in sorted(nodes_by_distance.keys() | edges_by_distance.keys())
    )


def graph_identity_data(identity: GraphIdentity) -> JsonObject:
    """Project one graph identity through the shared closed JSON shape."""
    return {
        "node_type": identity.node_type.value,
        "kind": identity.kind,
        "name": identity.name,
    }


def graph_edge_data(edge: GraphEdge) -> JsonObject:
    """Project one declared or live edge through the shared closed JSON shape."""
    return {
        "edge_type": edge.edge_type.value,
        "source": graph_identity_data(edge.source),
        "target": graph_identity_data(edge.target),
        "relationship": edge.relationship.value,
        "usage": edge.usage,
        "declared_by": (
            None if edge.declared_by is None else {"kind": edge.declared_by.kind, "name": edge.declared_by.name}
        ),
    }


def instance_ref_data(ref: InstanceRef) -> JsonObject:
    """Project one live instance identity without exposing provider state."""
    return {"kind": ref.instance_kind, "name": ref.instance_name}


def graph_result_data(result: GraphResult) -> JsonObject:
    """Project a closed graph result to the exact safe JSON v1 data shape."""
    return {
        "query": {
            "focus": {"kind": result.query.focus.kind, "name": result.query.focus.name},
            "direction": result.query.direction.value,
            "depth_limit": result.query.depth_limit,
        },
        "nodes": [
            {
                "node_type": node.node_type.value,
                "kind": node.kind,
                "name": node.name,
                "distance": node.distance,
            }
            for node in result.nodes
        ],
        "edges": [graph_edge_data(edge) for edge in result.edges],
    }
