"""Human projection for completed resource graph results."""

from __future__ import annotations

from agentworks import output
from agentworks.resources.graph_query import GraphEdge, GraphEdgeType, GraphIdentity, GraphResult, group_graph_result


def _identity(value: GraphIdentity) -> str:
    return f"{value.node_type.value} {value.kind}/{value.name}"


def _edge(value: GraphEdge) -> str:
    suffix = "declared" if value.edge_type is GraphEdgeType.DECLARED else "live-usage, current config"
    return f"{_identity(value.source)} -{value.relationship.value}-> {_identity(value.target)} [{suffix}]"


def render_graph_result(result: GraphResult) -> None:
    """Emit a flat distance-grouped view without deriving graph facts."""
    output.info(f"Graph: {result.query.focus.kind}/{result.query.focus.name}")
    output.detail(f"Direction: {result.query.direction.value}")
    depth = "all" if result.query.depth_limit is None else str(result.query.depth_limit)
    output.detail(f"Depth: {depth}")
    output.info("")

    for group in group_graph_result(result):
        output.info(f"Distance {group.distance}")
        with output.section():
            output.info("Nodes")
            with output.section():
                for node in group.nodes:
                    output.info(f"{node.node_type.value} {node.kind}/{node.name}")
            if group.edges:
                output.info("Edges")
                with output.section():
                    for edge in group.edges:
                        output.info(_edge(edge))
                        with output.section():
                            if edge.usage is not None:
                                output.info(f"Usage: {edge.usage}")
                            if edge.declared_by is not None:
                                output.info(f"Declared by: {edge.declared_by.kind}/{edge.declared_by.name}")
