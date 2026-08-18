"""Human projection for completed resource graph results."""

from __future__ import annotations

from agentworks import output
from agentworks.resources.graph_query import GraphEdge, GraphEdgeType, GraphIdentity, GraphResult, group_graph_result
from agentworks.resources.render import sanitize_fact_line


def _identity(value: GraphIdentity) -> str:
    return (
        f"{sanitize_fact_line(value.node_type.value)} {sanitize_fact_line(value.kind)}/{sanitize_fact_line(value.name)}"
    )


def _edge(value: GraphEdge) -> str:
    suffix = "declared" if value.edge_type is GraphEdgeType.DECLARED else "live-usage, current config"
    return (
        f"{_identity(value.source)} -{sanitize_fact_line(value.relationship.value)}-> "
        f"{_identity(value.target)} [{suffix}]"
    )


def render_graph_result(result: GraphResult) -> None:
    """Emit a flat distance-grouped view without deriving graph facts."""
    output.info(f"Graph: {sanitize_fact_line(result.query.focus.kind)}/{sanitize_fact_line(result.query.focus.name)}")
    output.detail(f"Direction: {sanitize_fact_line(result.query.direction.value)}")
    depth = "all" if result.query.depth_limit is None else str(result.query.depth_limit)
    output.detail(f"Depth: {depth}")
    output.info("")

    for group in group_graph_result(result):
        output.info(f"Distance {group.distance}")
        with output.section():
            output.info("Nodes")
            with output.section():
                for node in group.nodes:
                    output.info(
                        f"{sanitize_fact_line(node.node_type.value)} "
                        f"{sanitize_fact_line(node.kind)}/{sanitize_fact_line(node.name)}"
                    )
            if group.edges:
                output.info("Edges")
                with output.section():
                    for edge in group.edges:
                        output.info(_edge(edge))
                        with output.section():
                            if edge.usage is not None:
                                output.info(f"Usage: {sanitize_fact_line(edge.usage)}")
                            if edge.declared_by is not None:
                                output.info(
                                    f"Declared by: {sanitize_fact_line(edge.declared_by.kind)}/"
                                    f"{sanitize_fact_line(edge.declared_by.name)}"
                                )
