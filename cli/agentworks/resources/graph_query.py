"""Relationship policy for resource graph queries."""

from __future__ import annotations

from agentworks.resources.reference import RefRelationship

GRAPH_TRAVERSED_RELATIONSHIPS: frozenset[RefRelationship] = frozenset(
    {
        RefRelationship.USES,
        RefRelationship.INHERITS,
    }
)
