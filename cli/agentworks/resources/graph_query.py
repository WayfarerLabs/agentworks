"""Deterministic resource-graph queries and closed result projections."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from agentworks.db.database import Database
from agentworks.errors import AgentworksError, StateError
from agentworks.resources.access import ResourceIdentity, resolve_resource
from agentworks.resources.kind import KIND_REGISTRY, InstanceRef
from agentworks.resources.reference import RefRelationship, ResourceReference

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path
    from types import TracebackType

    from agentworks.machine_output import JsonObject
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


class LiveSourceState(StrEnum):
    UNOPENED = "unopened"
    ABSENT = "absent"
    OPEN = "open"
    CLOSED = "closed"


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
                and self.target.node_type is GraphNodeType.RESOURCE
                and isinstance(self.relationship, RefRelationship)
                and self.usage is not None
            )
        else:
            valid = (
                self.source.node_type is GraphNodeType.LIVE_INSTANCE
                and self.target.node_type is GraphNodeType.RESOURCE
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


def live_graph_identity(ref: InstanceRef) -> GraphIdentity:
    return GraphIdentity(GraphNodeType.LIVE_INSTANCE, ref.instance_kind, ref.instance_name)


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


def declared_graph_edge(ref: ResourceReference) -> GraphEdge:
    declared_by = None
    if ref.declared_by is not None:
        declared_by = ResourceIdentity(ref.declared_by[0], ref.declared_by[1])
    return GraphEdge(
        edge_type=GraphEdgeType.DECLARED,
        source=GraphIdentity(GraphNodeType.RESOURCE, ref.source[0], ref.source[1]),
        target=GraphIdentity(GraphNodeType.RESOURCE, ref.kind, ref.name),
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


class DatabaseLiveSource:
    """Lazy, request-scoped projection of persisted live instance facts."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.state = LiveSourceState.UNOPENED
        self._db: Database | None = None
        self._read_transaction_context: AbstractContextManager[None] | None = None
        self._entered = False

    def __enter__(self) -> DatabaseLiveSource:
        if self._entered or self.state is LiveSourceState.CLOSED:
            raise StateError("a live graph source is single-use", entity_kind="database")
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            if self._read_transaction_context is not None:
                self._read_transaction_context.__exit__(exc_type, exc_value, traceback)
        finally:
            try:
                if self._db is not None:
                    self._db.close()
            finally:
                self.state = LiveSourceState.CLOSED
        return False

    def supports(self, kind: str) -> bool:
        self._ensure_openable()
        handler = KIND_REGISTRY.get(kind)
        return handler is not None and callable(getattr(handler, "instances", None))

    def instances_for(
        self,
        registry: Registry,
        current: GraphIdentity,
        row: object,
    ) -> tuple[InstanceRef, ...]:
        self._ensure_openable()
        if self.state is LiveSourceState.UNOPENED:
            self._open()
        if self.state is LiveSourceState.ABSENT:
            return ()

        handler = KIND_REGISTRY.get(current.kind)
        method = getattr(handler, "instances", None) if handler is not None else None
        if not callable(method) or self._db is None:
            raise StateError(
                "the resource kind has no live-instance source",
                entity_kind=current.kind,
                entity_name=current.name,
            )
        try:
            return tuple(method(self._db, registry, row))
        except Exception as error:
            raise StateError(
                "live-instance projection failed",
                entity_kind=current.kind,
                entity_name=current.name,
            ) from error

    def _ensure_openable(self) -> None:
        if self.state is LiveSourceState.CLOSED:
            raise StateError("the live graph source is closed", entity_kind="database")

    def _open(self) -> None:
        try:
            self.database_path.stat()
        except FileNotFoundError:
            self.state = LiveSourceState.ABSENT
            return
        except OSError as error:
            raise StateError("state database inspection failed", entity_kind="database") from error

        try:
            database = Database(self.database_path, read_only=True)
        except AgentworksError:
            raise
        except Exception as error:
            raise StateError("state database open failed", entity_kind="database") from error

        try:
            transaction = database.read_transaction()
            transaction.__enter__()
        except BaseException as error:
            database.close()
            if isinstance(error, Exception) and not isinstance(error, AgentworksError):
                raise StateError("state database transaction failed", entity_kind="database") from error
            raise
        self._db = database
        self._read_transaction_context = transaction
        self.state = LiveSourceState.OPEN


def focused_graph_facts(
    registry: Registry,
    focus: ResourceIdentity,
    live_source: DatabaseLiveSource,
) -> FocusedGraphFacts:
    """Return only declared edges touching ``focus`` and its current live users."""
    with live_source:
        if not registry.is_finalized:
            raise StateError("graph queries require a finalized registry", entity_kind="registry")
        resolved = resolve_resource(registry, focus)
        focus_graph = resource_graph_identity(focus)

        dependencies = tuple(
            sorted(
                (declared_graph_edge(ref) for ref in registry.graph.edges_of(focus.kind, focus.name)),
                key=edge_fact_key,
            )
        )
        dependents = tuple(
            sorted(
                (declared_graph_edge(ref) for ref in registry.graph.incoming_edges_of(focus.kind, focus.name)),
                key=edge_fact_key,
            )
        )
        if any(edge.source != focus_graph for edge in dependencies):
            raise AssertionError("a focused dependency does not start at the focus")
        if any(edge.target != focus_graph for edge in dependents):
            raise AssertionError("a focused dependent does not end at the focus")

        used_by: tuple[InstanceRef, ...] | None = None
        if live_source.supports(focus.kind):
            used_by = tuple(
                sorted(
                    live_source.instances_for(registry, focus_graph, resolved.resource),
                    key=lambda ref: identity_key(live_graph_identity(ref)),
                )
            )

    return FocusedGraphFacts(dependencies, dependents, used_by)


def show_graph(
    registry: Registry,
    focus: ResourceIdentity,
    direction: GraphDirection,
    depth_limit: int | None,
    live_source: DatabaseLiveSource,
) -> GraphResult:
    """Return the deterministic fact graph reachable from one resource."""
    with live_source:
        if not registry.is_finalized:
            raise StateError("graph queries require a finalized registry", entity_kind="registry")
        resolved_focus = resolve_resource(registry, focus)
        query = GraphQuery(focus, direction, depth_limit)
        focus_id = resource_graph_identity(focus)
        distance = {focus_id: 0}
        queue = deque([focus_id])
        edges_by_key: dict[tuple[object, ...], GraphEdge] = {}

        while queue:
            current = queue.popleft()
            current_distance = distance[current]
            if depth_limit is not None and current_distance >= depth_limit:
                continue

            row = resolved_focus.resource if current == focus_id else registry.lookup(current.kind, current.name)
            candidates: list[tuple[GraphIdentity, GraphEdge]] = []

            if direction in {GraphDirection.DEPENDENCIES, GraphDirection.BOTH}:
                for ref in registry.graph.edges_of(current.kind, current.name):
                    if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                        edge = declared_graph_edge(ref)
                        candidates.append((edge.target, edge))

            if direction in {GraphDirection.DEPENDENTS, GraphDirection.BOTH}:
                for ref in registry.graph.incoming_edges_of(current.kind, current.name):
                    if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                        edge = declared_graph_edge(ref)
                        candidates.append((edge.source, edge))

                if live_source.supports(current.kind):
                    for instance in live_source.instances_for(registry, current, row):
                        live = live_graph_identity(instance)
                        candidates.append((live, _live_usage_edge(live, current)))

            for neighbor, edge in sorted(candidates, key=_neighbor_candidate_key):
                edges_by_key.setdefault(edge_fact_key(edge), edge)
                if neighbor in distance:
                    continue
                distance[neighbor] = current_distance + 1
                if neighbor.node_type is GraphNodeType.RESOURCE:
                    queue.append(neighbor)

        reached_resources = {identity for identity in distance if identity.node_type is GraphNodeType.RESOURCE}
        for source in sorted(reached_resources, key=identity_key):
            for ref in registry.graph.edges_of(source.kind, source.name):
                if ref.relationship not in GRAPH_TRAVERSED_RELATIONSHIPS:
                    continue
                edge = declared_graph_edge(ref)
                if edge.target in reached_resources:
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
