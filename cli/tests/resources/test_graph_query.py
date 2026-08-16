"""Structural policy tests for the graph-query relationship set."""

from agentworks.resources.graph_query import GRAPH_TRAVERSED_RELATIONSHIPS
from agentworks.resources.reference import RefRelationship


def test_every_relationship_is_explicitly_traversed() -> None:
    assert set(RefRelationship) == GRAPH_TRAVERSED_RELATIONSHIPS
