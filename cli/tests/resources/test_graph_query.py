"""Structural contracts for deterministic resource graph queries."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError, StateError
from agentworks.machine_output import MachineOutputCommand, encode_json_envelope
from agentworks.origin import Origin
from agentworks.resources import KIND_REGISTRY, Registry
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import build_graph
from agentworks.resources.graph_query import (
    GRAPH_TRAVERSED_RELATIONSHIPS,
    GraphDirection,
    GraphDistanceGroup,
    GraphEdge,
    GraphEdgeType,
    GraphIdentity,
    GraphNode,
    GraphNodeType,
    GraphQuery,
    GraphResult,
    focused_graph_facts,
    graph_result_data,
    group_graph_result,
    show_graph,
)
from agentworks.resources.graph_render import render_graph_result
from agentworks.resources.reference import RefRelationship, ResourceReference
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class _Row:
    name: str
    references: tuple[ResourceReference, ...] = ()
    origin: Origin | None = None

    def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
        del context
        return self.references


def _ref(
    source: str,
    target: str,
    *,
    relationship: RefRelationship = RefRelationship.USES,
    usage: str | None = None,
    declared_by: tuple[str, str] | None = None,
) -> ResourceReference:
    return ResourceReference(
        name=target,
        kind="node",
        usage=usage or f"{source}-TO-{target}",
        source=("node", source),
        relationship=relationship,
        declared_by=declared_by,
    )


def _registry(
    monkeypatch: pytest.MonkeyPatch,
    names: Sequence[str],
    references: Sequence[ResourceReference] = (),
    *,
    handler: object | None = None,
    reverse: bool = False,
) -> Registry:
    KIND_REGISTRY.pop("node", None)
    by_source: dict[str, list[ResourceReference]] = {}
    for reference in references:
        by_source.setdefault(reference.source[1], []).append(reference)
    registry = Registry.empty()
    ordered_names = reversed(names) if reverse else names
    for name in ordered_names:
        registry.add(
            "node",
            name,
            _Row(name, tuple(by_source.get(name, ()))),
            Origin.built_in(source="tests.graph-query"),
        )
    registry.finalize()
    monkeypatch.setitem(KIND_REGISTRY, "node", handler or SimpleNamespace())
    return registry


def _query(
    registry: Registry,
    tmp_path: Path,
    focus: str,
    direction: GraphDirection = GraphDirection.BOTH,
    depth: int | None = 1,
) -> GraphResult:
    return show_graph(
        registry,
        ResourceIdentity("node", focus),
        direction,
        depth,
    )


def _distances(result: GraphResult) -> dict[tuple[str, str], int]:
    return {(node.kind, node.name): node.distance for node in result.nodes}


def test_every_relationship_is_explicitly_traversed() -> None:
    assert set(RefRelationship) == GRAPH_TRAVERSED_RELATIONSHIPS


def test_graph_edge_variants_enforce_their_closed_shapes() -> None:
    resource = GraphIdentity(GraphNodeType.RESOURCE, "node", "resource")
    live = GraphIdentity(GraphNodeType.LIVE_INSTANCE, "vm", "live")
    declared = GraphEdge(
        GraphEdgeType.DECLARED,
        resource,
        resource,
        RefRelationship.USES,
        "USAGE_SENTINEL",
        None,
    )
    current = GraphEdge(
        GraphEdgeType.LIVE_USAGE,
        live,
        resource,
        RefRelationship.USES,
        None,
        None,
    )
    assert declared.usage == "USAGE_SENTINEL"
    assert current.source.node_type is GraphNodeType.LIVE_INSTANCE

    with pytest.raises(ValueError):
        GraphEdge(GraphEdgeType.DECLARED, live, resource, RefRelationship.USES, "x", None)
    with pytest.raises(ValueError):
        GraphEdge(GraphEdgeType.DECLARED, resource, resource, cast("RefRelationship", "unknown"), "x", None)
    with pytest.raises(ValueError):
        GraphEdge(GraphEdgeType.LIVE_USAGE, live, resource, RefRelationship.INHERITS, None, None)


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (GraphDirection.DEPENDENCIES, {"focus", "dependency"}),
        (GraphDirection.DEPENDENTS, {"focus", "dependent"}),
        (GraphDirection.BOTH, {"focus", "dependency", "dependent"}),
    ],
)
def test_direction_selects_each_incident_arm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    direction: GraphDirection,
    expected: set[str],
) -> None:
    references = [_ref("focus", "dependency"), _ref("dependent", "focus")]
    registry = _registry(monkeypatch, ["focus", "dependency", "dependent"], references)
    result = _query(registry, tmp_path, "focus", direction)
    assert {node.name for node in result.nodes} == expected
    assert result.query == GraphQuery(ResourceIdentity("node", "focus"), direction, 1)


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(1, {"a": 0, "b": 1}), (2, {"a": 0, "b": 1, "c": 2}), (None, {"a": 0, "b": 1, "c": 2, "d": 3})],
)
def test_finite_and_unbounded_depth_share_shortest_distance_bfs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    depth: int | None,
    expected: dict[str, int],
) -> None:
    registry = _registry(monkeypatch, ["a", "b", "c", "d"], [_ref("a", "b"), _ref("b", "c"), _ref("c", "d")])
    result = _query(registry, tmp_path, "a", GraphDirection.DEPENDENCIES, depth)
    assert {node.name: node.distance for node in result.nodes} == expected


def test_both_can_change_direction_at_each_expansion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = _registry(
        monkeypatch,
        ["a", "b", "c", "d"],
        [_ref("a", "b"), _ref("c", "b"), _ref("c", "d")],
    )
    result = _query(registry, tmp_path, "a", GraphDirection.BOTH, None)
    assert _distances(result) == {
        ("node", "a"): 0,
        ("node", "b"): 1,
        ("node", "c"): 2,
        ("node", "d"): 3,
    }


def test_diamond_keeps_known_node_edges_and_shortest_distance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    references = [_ref("a", "b"), _ref("a", "c"), _ref("b", "d"), _ref("c", "d")]
    result = _query(_registry(monkeypatch, ["a", "b", "c", "d"], references), tmp_path, "a", depth=None)
    assert _distances(result)[("node", "d")] == 2
    assert {(edge.source.name, edge.target.name) for edge in result.edges} == {
        ("a", "b"),
        ("a", "c"),
        ("b", "d"),
        ("c", "d"),
    }


def test_cycle_and_self_edge_are_finite_and_retained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    references = [_ref("a", "a"), _ref("a", "b"), _ref("b", "a")]
    registry = _registry(monkeypatch, ["a", "b"])
    incoming: dict[tuple[str, str], list[ResourceReference]] = {}
    outgoing: dict[tuple[str, str], list[ResourceReference]] = {}
    for reference in references:
        incoming.setdefault((reference.kind, reference.name), []).append(reference)
        outgoing.setdefault(reference.source, []).append(reference)
    registry._graph = build_graph(registry._resources, incoming, outgoing)

    result = _query(registry, tmp_path, "a", GraphDirection.BOTH, None)
    assert _distances(result) == {("node", "a"): 0, ("node", "b"): 1}
    assert len(result.edges) == 3


def test_exact_duplicates_collapse_but_parallel_facts_survive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exact = _ref("a", "b", usage="SAME")
    references = [
        exact,
        exact,
        _ref("a", "b", usage="OTHER"),
        _ref("a", "b", relationship=RefRelationship.INHERITS, usage="SAME"),
        _ref("a", "b", usage="SAME", declared_by=("node", "ancestor")),
    ]
    result = _query(_registry(monkeypatch, ["a", "b", "ancestor"], references), tmp_path, "a")
    assert len(result.edges) == 4
    assert {edge.usage for edge in result.edges} == {"SAME", "OTHER"}
    assert {edge.relationship for edge in result.edges} == set(RefRelationship)
    assert {edge.declared_by for edge in result.edges} == {None, ResourceIdentity("node", "ancestor")}
    assert [(edge.relationship, edge.usage, edge.declared_by) for edge in result.edges] == [
        (RefRelationship.INHERITS, "SAME", None),
        (RefRelationship.USES, "OTHER", None),
        (RefRelationship.USES, "SAME", None),
        (RefRelationship.USES, "SAME", ResourceIdentity("node", "ancestor")),
    ]


def test_induced_pass_adds_boundary_cross_edge_without_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    references = [_ref("focus", "a"), _ref("focus", "b"), _ref("a", "b"), _ref("b", "outside")]
    result = _query(
        _registry(monkeypatch, ["focus", "a", "b", "outside"], references),
        tmp_path,
        "focus",
        GraphDirection.DEPENDENCIES,
        1,
    )
    assert {node.name for node in result.nodes} == {"focus", "a", "b"}
    assert ("a", "b") in {(edge.source.name, edge.target.name) for edge in result.edges}
    assert all(edge.target.name != "outside" for edge in result.edges)


def test_no_neighbor_and_legacy_punctuation_are_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name = "legacy--name.with:punctuation"
    result = _query(_registry(monkeypatch, [name]), tmp_path, name, depth=None)
    assert result.nodes == (GraphNode(GraphNodeType.RESOURCE, "node", name, 0),)
    assert result.edges == ()


def test_focused_slice_retains_only_incident_edges_duplicates_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    duplicate = _ref("focus", "b", usage="same")
    references = [
        _ref("neighbor", "b", usage="induced"),
        duplicate,
        _ref("incoming", "focus", usage="inbound"),
        _ref(
            "focus",
            "a",
            relationship=RefRelationship.INHERITS,
            usage="base",
            declared_by=("node", "declarer"),
        ),
        duplicate,
    ]
    registry = _registry(
        monkeypatch,
        ["focus", "a", "b", "incoming", "neighbor", "declarer"],
        references,
    )

    facts = focused_graph_facts(
        registry,
        ResourceIdentity("node", "focus"),
    )

    assert [(edge.target.name, edge.relationship, edge.usage) for edge in facts.dependencies] == [
        ("a", RefRelationship.INHERITS, "base"),
        ("b", RefRelationship.USES, "same"),
        ("b", RefRelationship.USES, "same"),
    ]
    assert facts.dependencies[0].declared_by == ResourceIdentity("node", "declarer")
    assert [(edge.source.name, edge.target.name) for edge in facts.dependents] == [("incoming", "focus")]
    assert all(edge.source.name != "neighbor" for edge in (*facts.dependencies, *facts.dependents))
    assert facts.used_by is None


def test_focused_slice_includes_direct_edges_outside_traversal_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentworks.resources import graph_query

    references = [
        _ref("focus", "runtime", relationship=RefRelationship.USES),
        _ref("focus", "base", relationship=RefRelationship.INHERITS),
    ]
    registry = _registry(monkeypatch, ["focus", "runtime", "base"], references)
    monkeypatch.setattr(graph_query, "GRAPH_TRAVERSED_RELATIONSHIPS", frozenset({RefRelationship.USES}))

    facts = focused_graph_facts(
        registry,
        ResourceIdentity("node", "focus"),
    )
    traversed = show_graph(
        registry,
        ResourceIdentity("node", "focus"),
        GraphDirection.DEPENDENCIES,
        1,
    )

    assert [(edge.target.name, edge.relationship) for edge in facts.dependencies] == [
        ("base", RefRelationship.INHERITS),
        ("runtime", RefRelationship.USES),
    ]
    assert {(node.name, node.distance) for node in traversed.nodes} == {("focus", 0), ("runtime", 1)}


def test_focused_slice_keeps_edge_free_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(monkeypatch, ["focus"])
    facts = focused_graph_facts(
        registry,
        ResourceIdentity("node", "focus"),
    )
    assert facts.dependencies == facts.dependents == ()
    assert facts.used_by is None


def test_focused_slice_reads_each_incident_arm_once_without_expanding_neighbors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentworks.resources.graph import DependencyGraph

    registry = _registry(
        monkeypatch,
        ["focus", "dependency", "dependent", "outside"],
        [_ref("focus", "dependency"), _ref("dependent", "focus"), _ref("dependency", "outside")],
    )
    outgoing = DependencyGraph.edges_of
    incoming = DependencyGraph.incoming_edges_of
    calls: list[tuple[str, str, str]] = []

    def counted_outgoing(graph: DependencyGraph, kind: str, name: str) -> tuple[ResourceReference, ...]:
        calls.append(("out", kind, name))
        return outgoing(graph, kind, name)

    def counted_incoming(graph: DependencyGraph, kind: str, name: str) -> tuple[ResourceReference, ...]:
        calls.append(("in", kind, name))
        return incoming(graph, kind, name)

    monkeypatch.setattr(DependencyGraph, "edges_of", counted_outgoing)
    monkeypatch.setattr(DependencyGraph, "incoming_edges_of", counted_incoming)

    facts = focused_graph_facts(
        registry,
        ResourceIdentity("node", "focus"),
    )

    assert calls == [("out", "node", "focus"), ("in", "node", "focus")]
    assert [(edge.source.name, edge.target.name) for edge in facts.dependencies] == [("focus", "dependency")]
    assert [(edge.source.name, edge.target.name) for edge in facts.dependents] == [("dependent", "focus")]


@pytest.mark.parametrize("arm", ["dependencies", "dependents"])
def test_focused_slice_rejects_an_edge_that_does_not_touch_its_focus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arm: str,
) -> None:
    from agentworks.resources.graph import DependencyGraph

    registry = _registry(monkeypatch, ["focus"])
    if arm == "dependencies":
        monkeypatch.setattr(DependencyGraph, "edges_of", lambda *_args: (_ref("wrong", "target"),))
    else:
        monkeypatch.setattr(DependencyGraph, "incoming_edges_of", lambda *_args: (_ref("source", "wrong"),))

    with pytest.raises(AssertionError):
        focused_graph_facts(
            registry,
            ResourceIdentity("node", "focus"),
        )


def test_real_registry_inbound_edges_retain_relationship_and_usage(tmp_path: Path) -> None:
    config_path = write_cfg(
        tmp_path,
        ManifestDoc("vm-template", "default", {"apt": ["zsh"]}),
    )
    registry = build_registry(load_config(config_path, warn_issues=False))
    result = show_graph(
        registry,
        ResourceIdentity("secret", "tailscale-auth-key"),
        GraphDirection.DEPENDENTS,
        1,
    )
    inbound = [
        edge for edge in result.edges if edge.target.kind == "secret" and edge.target.name == "tailscale-auth-key"
    ]
    assert inbound
    assert all(edge.source.kind and edge.source.name for edge in inbound)
    assert all(edge.relationship is RefRelationship.USES for edge in inbound)
    assert all(edge.usage for edge in inbound)


def test_registry_and_focus_validation_precede_query(monkeypatch: pytest.MonkeyPatch) -> None:
    unfinished = Registry.empty()
    with pytest.raises(StateError) as unfinished_error:
        show_graph(
            unfinished,
            ResourceIdentity("node", "focus"),
            GraphDirection.BOTH,
            1,
        )
    assert unfinished_error.value.entity_kind == "registry"

    registry = _registry(monkeypatch, ["present"])
    with pytest.raises(NotFoundError) as missing_error:
        show_graph(
            registry,
            ResourceIdentity("node", "missing"),
            GraphDirection.BOTH,
            1,
        )
    assert missing_error.value.entity_kind == "node"
    assert missing_error.value.entity_name == "missing"


def test_total_order_is_independent_of_registry_insertion_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = [f"n-{index:03}" for index in range(250)]
    references = [_ref(names[index], names[index + 1]) for index in range(len(names) - 1)]
    references.extend([_ref("n-000", "n-002", usage="parallel-a"), _ref("n-000", "n-002", usage="parallel-b")])
    forward = _query(_registry(monkeypatch, names, references), tmp_path, "n-000", depth=None)
    reverse = _query(
        _registry(monkeypatch, names, list(reversed(references)), reverse=True), tmp_path, "n-000", depth=None
    )
    assert forward == reverse
    assert len(forward.nodes) == 250


def test_grouping_assigns_each_edge_once_at_maximum_endpoint_distance() -> None:
    focus = ResourceIdentity("node", "a")
    a = GraphIdentity(GraphNodeType.RESOURCE, "node", "a")
    b = GraphIdentity(GraphNodeType.RESOURCE, "node", "b")
    c = GraphIdentity(GraphNodeType.RESOURCE, "node", "c")
    ab = GraphEdge(GraphEdgeType.DECLARED, a, b, RefRelationship.USES, "AB", None)
    cb = GraphEdge(GraphEdgeType.DECLARED, c, b, RefRelationship.INHERITS, "CB", None)
    result = GraphResult(
        GraphQuery(focus, GraphDirection.DEPENDENCIES, None),
        (
            GraphNode(GraphNodeType.RESOURCE, "node", "a", 0),
            GraphNode(GraphNodeType.RESOURCE, "node", "b", 1),
            GraphNode(GraphNodeType.RESOURCE, "node", "c", 2),
        ),
        (ab, cb),
    )
    assert group_graph_result(result) == (
        GraphDistanceGroup(0, (result.nodes[0],), ()),
        GraphDistanceGroup(1, (result.nodes[1],), (ab,)),
        GraphDistanceGroup(2, (result.nodes[2],), (cb,)),
    )


def test_human_projection_emits_every_unique_fact_once_in_result_order(captured_output: Any) -> None:
    focus = GraphIdentity(GraphNodeType.RESOURCE, "focus-kind", "FOCUS_NODE_SENTINEL")
    declared_target = GraphIdentity(GraphNodeType.RESOURCE, "target-kind", "DECLARED_TARGET_SENTINEL")
    null_source = GraphIdentity(GraphNodeType.RESOURCE, "null-kind", "NULL_SOURCE_SENTINEL")
    live = GraphIdentity(GraphNodeType.LIVE_INSTANCE, "live-kind", "LIVE_NODE_SENTINEL")
    declared_edge = GraphEdge(
        GraphEdgeType.DECLARED,
        focus,
        declared_target,
        RefRelationship.INHERITS,
        "DECLARED_USAGE_SENTINEL",
        ResourceIdentity("declarer-kind", "DECLARER_FACT_SENTINEL"),
    )
    null_provenance_edge = GraphEdge(
        GraphEdgeType.DECLARED,
        null_source,
        focus,
        RefRelationship.USES,
        "NULL_PROVENANCE_USAGE_SENTINEL",
        None,
    )
    live_edge = GraphEdge(
        GraphEdgeType.LIVE_USAGE,
        live,
        null_source,
        RefRelationship.USES,
        None,
        None,
    )
    result = GraphResult(
        GraphQuery(ResourceIdentity("focus-kind", "FOCUS_NODE_SENTINEL"), GraphDirection.BOTH, 1),
        (
            GraphNode(GraphNodeType.RESOURCE, "focus-kind", "FOCUS_NODE_SENTINEL", 0),
            GraphNode(GraphNodeType.RESOURCE, "null-kind", "NULL_SOURCE_SENTINEL", 1),
            GraphNode(GraphNodeType.RESOURCE, "target-kind", "DECLARED_TARGET_SENTINEL", 1),
            GraphNode(GraphNodeType.LIVE_INSTANCE, "live-kind", "LIVE_NODE_SENTINEL", 1),
        ),
        (declared_edge, null_provenance_edge, live_edge),
    )
    groups = group_graph_result(result)
    assert groups[1].nodes == result.nodes[1:]
    assert groups[1].edges == result.edges

    render_graph_result(result)
    messages = [message for _, _, message in captured_output.lines]
    assert sum("DECLARED_USAGE_SENTINEL" in message for message in messages) == 1
    assert sum("NULL_PROVENANCE_USAGE_SENTINEL" in message for message in messages) == 1
    assert sum("DECLARER_FACT_SENTINEL" in message for message in messages) == 1
    assert sum("DECLARED_TARGET_SENTINEL" in message for message in messages) == 2
    assert sum("LIVE_NODE_SENTINEL" in message for message in messages) == 2
    assert sum("NULL_SOURCE_SENTINEL" in message for message in messages) == 3
    edge_facts = [
        ("FOCUS_NODE_SENTINEL", "DECLARED_TARGET_SENTINEL", RefRelationship.INHERITS.value),
        ("NULL_SOURCE_SENTINEL", "FOCUS_NODE_SENTINEL", RefRelationship.USES.value),
        ("LIVE_NODE_SENTINEL", "NULL_SOURCE_SENTINEL", RefRelationship.USES.value),
    ]
    edge_positions: list[int] = []
    for source_name, target_name, relationship in edge_facts:
        matching = [
            index
            for index, message in enumerate(messages)
            if source_name in message and target_name in message and relationship in message
        ]
        assert len(matching) == 1
        edge_positions.append(matching[0])
    assert edge_positions == sorted(edge_positions)
    node_positions = [
        next(index for index, message in enumerate(messages) if marker in message)
        for marker in ("NULL_SOURCE_SENTINEL", "DECLARED_TARGET_SENTINEL", "LIVE_NODE_SENTINEL")
    ]
    assert node_positions == sorted(node_positions)
    assert max(node_positions) < min(edge_positions)
    detail_counts = [
        sum(level == 3 for _, level, _ in captured_output.lines[start + 1 : stop])
        for start, stop in zip(edge_positions, [*edge_positions[1:], len(captured_output.lines)], strict=True)
    ]
    assert detail_counts == [2, 1, 0]
    assert edge_positions[0] < next(
        index for index, message in enumerate(messages) if "DECLARED_USAGE_SENTINEL" in message
    )
    assert any("LIVE_NODE_SENTINEL" in message and "NULL_SOURCE_SENTINEL" in message for message in messages)


def test_human_projection_sanitizes_controls_from_every_dynamic_fact(captured_output: Any) -> None:
    ordinary_unicode = "café 雪"
    hostile = "\x00\x07\t\n\x1b\x7f\x80\x9f\u2028\u2029\u202e\u2066\ud800"

    def unsafe(marker: str) -> str:
        return f"{marker[:1]}{hostile}{marker[1:]} {ordinary_unicode}"

    source = GraphIdentity(GraphNodeType.LIVE_INSTANCE, unsafe("live-kind"), unsafe("LIVE-NAME"))
    target = GraphIdentity(GraphNodeType.RESOURCE, unsafe("resource-kind"), unsafe("RESOURCE-NAME"))
    result = GraphResult(
        GraphQuery(ResourceIdentity(unsafe("focus-kind"), unsafe("FOCUS-NAME")), GraphDirection.BOTH, 1),
        (
            GraphNode(GraphNodeType.RESOURCE, target.kind, target.name, 0),
            GraphNode(GraphNodeType.LIVE_INSTANCE, source.kind, source.name, 1),
        ),
        (
            GraphEdge(GraphEdgeType.LIVE_USAGE, source, target, RefRelationship.USES, None, None),
            GraphEdge(
                GraphEdgeType.DECLARED,
                target,
                target,
                RefRelationship.INHERITS,
                unsafe("USAGE-FACT"),
                ResourceIdentity(unsafe("declarer-kind"), unsafe("DECLARER-NAME")),
            ),
        ),
    )
    render_graph_result(result)
    messages = [message for _, _, message in captured_output.lines]
    rendered = "".join(messages)
    assert all(
        not any(unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in message)
        for message in messages
    )
    assert ordinary_unicode in rendered
    for marker in (
        "focus-kind",
        "FOCUS-NAME",
        "resource-kind",
        "RESOURCE-NAME",
        "live-kind",
        "LIVE-NAME",
        "USAGE-FACT",
        "declarer-kind",
        "DECLARER-NAME",
    ):
        assert marker in rendered


def test_json_projection_has_only_the_closed_safe_scalar_shape_and_nulls() -> None:
    source = GraphIdentity(GraphNodeType.LIVE_INSTANCE, "vm", "legacy--vm.with:punctuation")
    target = GraphIdentity(GraphNodeType.RESOURCE, "node", "target")
    result = GraphResult(
        GraphQuery(ResourceIdentity("node", "target"), GraphDirection.BOTH, None),
        (
            GraphNode(GraphNodeType.RESOURCE, "node", "target", 0),
            GraphNode(GraphNodeType.LIVE_INSTANCE, "vm", "legacy--vm.with:punctuation", 1),
        ),
        (GraphEdge(GraphEdgeType.LIVE_USAGE, source, target, RefRelationship.USES, None, None),),
    )
    data = graph_result_data(result)
    assert data == {
        "query": {
            "focus": {"kind": "node", "name": "target"},
            "direction": "both",
            "depth_limit": None,
        },
        "nodes": [
            {"node_type": "resource", "kind": "node", "name": "target", "distance": 0},
            {
                "node_type": "live-instance",
                "kind": "vm",
                "name": "legacy--vm.with:punctuation",
                "distance": 1,
            },
        ],
        "edges": [
            {
                "edge_type": "live-usage",
                "source": {"node_type": "live-instance", "kind": "vm", "name": "legacy--vm.with:punctuation"},
                "target": {"node_type": "resource", "kind": "node", "name": "target"},
                "relationship": "uses",
                "usage": None,
                "declared_by": None,
            }
        ],
    }


def test_existing_envelope_encodes_graph_data_before_write_and_escapes_controls() -> None:
    result = GraphResult(
        GraphQuery(ResourceIdentity("node", "name\x7f\x80"), GraphDirection.BOTH, None),
        (GraphNode(GraphNodeType.RESOURCE, "node", "name\x7f\x80", 0),),
        (),
    )
    document = encode_json_envelope(MachineOutputCommand.GRAPH_SHOW, graph_result_data(result))
    assert b"\\u007f\\u0080" in document
    assert b"\x7f" not in document
    assert b"\xc2\x80" not in document
