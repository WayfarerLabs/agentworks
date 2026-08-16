"""Human projection for completed resource graph results."""

from __future__ import annotations

from agentworks import output
from agentworks.resources.graph_query import GraphEdge, GraphEdgeType, GraphIdentity, GraphResult, group_graph_result
from agentworks.terminal import sanitize_terminal_output


def _safe(value: str) -> str:
    return sanitize_terminal_output(value).replace("\n", "").replace("\t", "")


def _identity(value: GraphIdentity) -> str:
    return f"{_safe(value.node_type.value)} {_safe(value.kind)}/{_safe(value.name)}"


def _edge(value: GraphEdge) -> str:
    suffix = "declared" if value.edge_type is GraphEdgeType.DECLARED else "live-usage, current config"
    return f"{_identity(value.source)} -{_safe(value.relationship.value)}-> {_identity(value.target)} [{suffix}]"


def render_graph_result(result: GraphResult) -> None:
    """Emit a flat distance-grouped view without deriving graph facts."""
    output.info(f"Graph: {_safe(result.query.focus.kind)}/{_safe(result.query.focus.name)}")
    output.detail(f"Direction: {_safe(result.query.direction.value)}")
    depth = "all" if result.query.depth_limit is None else str(result.query.depth_limit)
    output.detail(f"Depth: {depth}")
    output.info("")

    for group in group_graph_result(result):
        output.info(f"Distance {group.distance}")
        with output.section():
            output.info("Nodes")
            with output.section():
                for node in group.nodes:
                    output.info(f"{_safe(node.node_type.value)} {_safe(node.kind)}/{_safe(node.name)}")
            if group.edges:
                output.info("Edges")
                with output.section():
                    for edge in group.edges:
                        output.info(_edge(edge))
                        with output.section():
                            if edge.usage is not None:
                                output.info(f"Usage: {_safe(edge.usage)}")
                            if edge.declared_by is not None:
                                output.info(
                                    f"Declared by: {_safe(edge.declared_by.kind)}/{_safe(edge.declared_by.name)}"
                                )
