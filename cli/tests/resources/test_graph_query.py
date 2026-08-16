"""Structural contracts for deterministic resource graph queries."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import BusyStateError, NotFoundError, StateError
from agentworks.machine_output import MachineOutputCommand, encode_json_envelope
from agentworks.origin import Origin
from agentworks.resources import KIND_REGISTRY, Registry
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import build_graph
from agentworks.resources.graph_query import (
    GRAPH_TRAVERSED_RELATIONSHIPS,
    DatabaseLiveSource,
    GraphDirection,
    GraphDistanceGroup,
    GraphEdge,
    GraphEdgeType,
    GraphIdentity,
    GraphNode,
    GraphNodeType,
    GraphQuery,
    GraphResult,
    LiveSourceState,
    graph_result_data,
    group_graph_result,
    show_graph,
)
from agentworks.resources.graph_render import render_graph_result
from agentworks.resources.kind import InstanceRef
from agentworks.resources.reference import RefRelationship, ResourceReference
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from agentworks.db.database import Database


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
        DatabaseLiveSource(tmp_path / "absent.db"),
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
        DatabaseLiveSource(tmp_path / "absent.db"),
    )
    inbound = [
        edge for edge in result.edges if edge.target.kind == "secret" and edge.target.name == "tailscale-auth-key"
    ]
    assert inbound
    assert all(edge.source.kind and edge.source.name for edge in inbound)
    assert all(edge.relationship is RefRelationship.USES for edge in inbound)
    assert all(edge.usage for edge in inbound)


def test_registry_and_focus_validation_precede_live_source_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    path = _PresentPath()
    unfinished = Registry.empty()
    unfinished_source = DatabaseLiveSource(cast("Path", path))
    with pytest.raises(StateError) as unfinished_error:
        show_graph(
            unfinished,
            ResourceIdentity("node", "focus"),
            GraphDirection.BOTH,
            1,
            unfinished_source,
        )
    assert unfinished_error.value.entity_kind == "registry"
    assert unfinished_source.state is LiveSourceState.CLOSED
    assert path.stat_count == 0

    registry = _registry(monkeypatch, ["present"], handler=_InstanceHandler(lambda name: iter(())))
    missing_source = DatabaseLiveSource(cast("Path", path))
    with pytest.raises(NotFoundError) as missing_error:
        show_graph(
            registry,
            ResourceIdentity("node", "missing"),
            GraphDirection.BOTH,
            1,
            missing_source,
        )
    assert missing_error.value.entity_kind == "node"
    assert missing_error.value.entity_name == "missing"
    assert missing_source.state is LiveSourceState.CLOSED
    assert path.stat_count == 0


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


class _Transaction(AbstractContextManager[None]):
    def __init__(self, counts: dict[str, int], exit_error: BaseException | None = None) -> None:
        self.counts = counts
        self.exit_error = exit_error

    def __enter__(self) -> None:
        self.counts["transaction_enter"] += 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, exc_value, traceback
        self.counts["transaction_exit"] += 1
        if self.exit_error is not None:
            raise self.exit_error
        return False


class _FakeDatabase:
    counts: dict[str, int]
    exit_error: BaseException | None = None
    close_error: BaseException | None = None

    def __init__(self, path: Path, *, read_only: bool = False, timeout: float | None = None) -> None:
        del path
        assert read_only is True
        assert timeout is None
        self.counts["database"] += 1

    def read_transaction(self) -> AbstractContextManager[None]:
        return _Transaction(self.counts, self.exit_error)

    def close(self) -> None:
        self.counts["close"] += 1
        if self.close_error is not None:
            raise self.close_error


class _QueryCountingDatabase(_FakeDatabase):
    def list_vms(self) -> tuple[()]:
        self.counts["list_vms"] += 1
        return ()

    def list_sessions(self) -> tuple[()]:
        self.counts["list_sessions"] += 1
        return ()


class _InstanceHandler:
    def __init__(self, values: Callable[[str], Iterator[InstanceRef]]) -> None:
        self.values = values
        self.calls: list[str] = []

    def instances(self, db: Database, registry: Registry, row: _Row) -> Iterator[InstanceRef]:
        del db, registry
        self.calls.append(row.name)
        return self.values(row.name)


class _ListQueryHandler:
    def __init__(self, query: Callable[[Database], Sequence[object]]) -> None:
        self.query = query
        self.calls: list[str] = []

    def instances(self, db: Database, registry: Registry, row: _Row) -> tuple[InstanceRef, ...]:
        del registry
        self.calls.append(row.name)
        self.query(db)
        return ()


def _install_fake_database(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from agentworks.resources import graph_query

    counts = {"database": 0, "transaction_enter": 0, "transaction_exit": 0, "close": 0}
    _FakeDatabase.counts = counts
    _FakeDatabase.exit_error = None
    _FakeDatabase.close_error = None
    monkeypatch.setattr(graph_query, "Database", _FakeDatabase)
    return counts


def test_live_source_is_lazy_then_reused_once_across_expanded_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counts = _install_fake_database(monkeypatch)
    database_path = tmp_path / "state.db"
    database_path.touch()
    handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", f"live-{name}"),)))
    registry = _registry(monkeypatch, ["a", "b", "c"], [_ref("a", "b"), _ref("b", "c")], handler=handler)
    source = DatabaseLiveSource(database_path)
    result = show_graph(registry, ResourceIdentity("node", "a"), GraphDirection.BOTH, None, source)

    assert counts == {"database": 1, "transaction_enter": 1, "transaction_exit": 1, "close": 1}
    assert handler.calls == ["a", "b", "c"]
    assert source.state is LiveSourceState.CLOSED
    assert {node.node_type for node in result.nodes} == {GraphNodeType.RESOURCE, GraphNodeType.LIVE_INSTANCE}
    live_edges = [edge for edge in result.edges if edge.edge_type is GraphEdgeType.LIVE_USAGE]
    assert len(live_edges) == 3
    assert all(edge.source.node_type is GraphNodeType.LIVE_INSTANCE for edge in live_edges)
    assert all(edge.target.node_type is GraphNodeType.RESOURCE for edge in live_edges)
    assert all(edge.relationship is RefRelationship.USES for edge in live_edges)


def test_dependencies_and_depth_boundary_do_not_inspect_live_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _CountingPath:
        def stat(self) -> None:
            raise AssertionError("path inspection was not demanded")

    reference = ResourceReference(
        name="platform",
        kind="platform",
        usage="SITE_TO_PLATFORM",
        source=("site", "site"),
    )
    registry = Registry.empty()
    registry.add(
        "platform",
        "platform",
        _Row("platform"),
        Origin.built_in(source="tests.graph-query"),
    )
    registry.add(
        "site",
        "site",
        _Row("site", (reference,)),
        Origin.built_in(source="tests.graph-query"),
    )
    registry.finalize()
    handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", name),)))
    monkeypatch.setitem(KIND_REGISTRY, "platform", SimpleNamespace())
    monkeypatch.setitem(KIND_REGISTRY, "site", handler)
    path = cast("Path", _CountingPath())
    dependency_result = show_graph(
        registry,
        ResourceIdentity("site", "site"),
        GraphDirection.DEPENDENCIES,
        1,
        DatabaseLiveSource(path),
    )
    boundary_result = show_graph(
        registry,
        ResourceIdentity("platform", "platform"),
        GraphDirection.DEPENDENTS,
        1,
        DatabaseLiveSource(path),
    )
    assert {(node.kind, node.name) for node in dependency_result.nodes} == {
        ("site", "site"),
        ("platform", "platform"),
    }
    assert {(node.kind, node.name) for node in boundary_result.nodes} == {
        ("site", "site"),
        ("platform", "platform"),
    }
    assert handler.calls == []


def test_platform_site_live_depth_demand_and_boundary_asymmetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counts = _install_fake_database(monkeypatch)
    database_path = tmp_path / "state.db"
    database_path.touch()
    site_handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", f"vm-for-{name}"),)))
    registry = _registry(monkeypatch, ["platform", "site"], [_ref("site", "platform")])
    monkeypatch.setitem(KIND_REGISTRY, "node", SimpleNamespace())
    shallow = show_graph(
        registry,
        ResourceIdentity("node", "platform"),
        GraphDirection.DEPENDENTS,
        1,
        DatabaseLiveSource(database_path),
    )
    assert counts["database"] == 0
    assert all(node.node_type is GraphNodeType.RESOURCE for node in shallow.nodes)

    monkeypatch.setitem(KIND_REGISTRY, "node", site_handler)
    deep = show_graph(
        registry,
        ResourceIdentity("node", "platform"),
        GraphDirection.DEPENDENTS,
        2,
        DatabaseLiveSource(database_path),
    )
    assert counts["database"] == 1
    assert any(node.node_type is GraphNodeType.LIVE_INSTANCE and node.distance == 2 for node in deep.nodes)


def test_missing_database_is_empty_and_source_is_single_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", name),)))
    registry = _registry(monkeypatch, ["focus"], handler=handler)
    source = DatabaseLiveSource(tmp_path / "missing.db")
    result = show_graph(registry, ResourceIdentity("node", "focus"), GraphDirection.DEPENDENTS, None, source)
    assert result.nodes == (GraphNode(GraphNodeType.RESOURCE, "node", "focus", 0),)
    assert handler.calls == []
    assert source.state is LiveSourceState.CLOSED
    with pytest.raises(StateError) as raised:
        source.supports("node")
    assert raised.value.entity_kind == "database"
    with pytest.raises(StateError):
        source.__enter__()


def test_absent_source_is_checked_once_and_reused_without_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    path = _MissingPath()
    handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", name),)))
    registry = _registry(monkeypatch, ["a", "b"], [_ref("a", "b")], handler=handler)
    result = show_graph(
        registry,
        ResourceIdentity("node", "a"),
        GraphDirection.BOTH,
        None,
        DatabaseLiveSource(cast("Path", path)),
    )
    assert {(node.kind, node.name) for node in result.nodes} == {("node", "a"), ("node", "b")}
    assert path.stat_count == 1
    assert handler.calls == []


def test_known_live_node_keeps_distinct_edges_from_each_expanded_resource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_database(monkeypatch)
    database_path = tmp_path / "state.db"
    database_path.touch()
    handler = _InstanceHandler(lambda name: iter((InstanceRef("vm", "shared"),)))
    registry = _registry(monkeypatch, ["a", "b"], [_ref("a", "b")], handler=handler)
    result = show_graph(
        registry,
        ResourceIdentity("node", "a"),
        GraphDirection.BOTH,
        None,
        DatabaseLiveSource(database_path),
    )
    live_nodes = [node for node in result.nodes if node.node_type is GraphNodeType.LIVE_INSTANCE]
    live_edges = [edge for edge in result.edges if edge.edge_type is GraphEdgeType.LIVE_USAGE]
    assert [(node.kind, node.name, node.distance) for node in live_nodes] == [("vm", "shared", 1)]
    assert {(edge.source.name, edge.target.name) for edge in live_edges} == {("shared", "a"), ("shared", "b")}


class _StatFailure:
    def __init__(self, error: OSError) -> None:
        self.error = error

    def stat(self) -> None:
        raise self.error


class _PresentPath:
    def __init__(self) -> None:
        self.stat_count = 0

    def stat(self) -> None:
        self.stat_count += 1


class _MissingPath:
    def __init__(self) -> None:
        self.stat_count = 0

    def stat(self) -> None:
        self.stat_count += 1
        raise FileNotFoundError


@pytest.mark.parametrize("error", [PermissionError(), NotADirectoryError(), OSError("unreadable")])
def test_non_missing_path_inspection_failures_are_typed(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    handler = _InstanceHandler(lambda name: iter(()))
    registry = _registry(monkeypatch, ["focus"], handler=handler)
    source = DatabaseLiveSource(cast("Path", _StatFailure(error)))
    with pytest.raises(StateError) as raised:
        show_graph(registry, ResourceIdentity("node", "focus"), GraphDirection.DEPENDENTS, 1, source)
    assert raised.value.entity_kind == "database"
    assert raised.value.__cause__ is error
    assert source.state is LiveSourceState.CLOSED


@pytest.mark.parametrize(
    "failure",
    [
        StateError("stale", entity_kind="database"),
        StateError("newer", entity_kind="database"),
        StateError("malformed", entity_kind="database"),
        BusyStateError(),
    ],
)
def test_typed_database_open_failures_propagate_whole_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: StateError
) -> None:
    from agentworks.resources import graph_query

    database_path = tmp_path / "state.db"
    database_path.touch()
    handler = _InstanceHandler(lambda name: iter(()))
    registry = _registry(monkeypatch, ["focus"], handler=handler)

    def fail_open(path: Path, *, read_only: bool = False) -> Any:
        del path, read_only
        raise failure

    monkeypatch.setattr(graph_query, "Database", fail_open)
    with pytest.raises(type(failure)) as raised:
        show_graph(
            registry,
            ResourceIdentity("node", "focus"),
            GraphDirection.DEPENDENTS,
            1,
            DatabaseLiveSource(database_path),
        )
    assert raised.value is failure


def test_disappearing_after_stat_is_an_open_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agentworks.resources import graph_query

    database_path = tmp_path / "state.db"
    database_path.touch()
    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(lambda name: iter(())))

    def vanish(path: Path, *, read_only: bool = False) -> Any:
        del path, read_only
        raise FileNotFoundError("race")

    monkeypatch.setattr(graph_query, "Database", vanish)
    with pytest.raises(StateError) as raised:
        _query_with_source(registry, database_path)
    assert raised.value.entity_kind == "database"
    assert isinstance(raised.value.__cause__, FileNotFoundError)


def test_directory_database_is_a_typed_whole_query_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = tmp_path / "state-directory"
    directory.mkdir()
    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(lambda name: iter(())))
    with pytest.raises(StateError):
        _query_with_source(registry, directory)


def test_transaction_entry_failure_closes_and_frames_untyped_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentworks.resources import graph_query

    failure = RuntimeError("TRANSACTION_ENTRY_SENTINEL")
    counts = {"close": 0}

    class _BrokenTransaction:
        def __enter__(self) -> None:
            raise failure

        def __exit__(self, *args: object) -> Literal[False]:
            del args
            return False

    class _EntryFailureDatabase:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            del path
            assert read_only is True

        def read_transaction(self) -> _BrokenTransaction:
            return _BrokenTransaction()

        def close(self) -> None:
            counts["close"] += 1

    database_path = tmp_path / "state.db"
    database_path.touch()
    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(lambda name: iter(())))
    monkeypatch.setattr(graph_query, "Database", _EntryFailureDatabase)
    with pytest.raises(StateError) as raised:
        _query_with_source(registry, database_path)
    assert raised.value.entity_kind == "database"
    assert raised.value.__cause__ is failure
    assert counts["close"] == 1


def _query_with_source(registry: Registry, database_path: Path) -> GraphResult:
    return show_graph(
        registry,
        ResourceIdentity("node", "focus"),
        GraphDirection.DEPENDENTS,
        1,
        DatabaseLiveSource(database_path),
    )


@pytest.mark.parametrize("during_iteration", [False, True])
def test_hook_call_and_iteration_failures_are_resource_framed_and_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, during_iteration: bool
) -> None:
    counts = _install_fake_database(monkeypatch)
    database_path = tmp_path / "state.db"
    database_path.touch()
    failure = RuntimeError("HOOK_SENTINEL")

    def values(name: str) -> Iterator[InstanceRef]:
        del name
        if not during_iteration:
            raise failure

        def broken() -> Iterator[InstanceRef]:
            yield InstanceRef("vm", "partial")
            raise failure

        return broken()

    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(values))
    with pytest.raises(StateError) as raised:
        _query_with_source(registry, database_path)
    assert raised.value.entity_kind == "node"
    assert raised.value.entity_name == "focus"
    assert raised.value.__cause__ is failure
    assert counts["transaction_exit"] == 1
    assert counts["close"] == 1


def test_control_signal_and_close_failure_propagate_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counts = _install_fake_database(monkeypatch)
    database_path = tmp_path / "state.db"
    database_path.touch()

    def interrupted(name: str) -> Iterator[InstanceRef]:
        del name
        raise KeyboardInterrupt

    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(interrupted))
    with pytest.raises(KeyboardInterrupt):
        _query_with_source(registry, database_path)
    assert counts["transaction_exit"] == 1
    assert counts["close"] == 1

    rollback_failure = RuntimeError("ROLLBACK_SENTINEL")
    _FakeDatabase.exit_error = rollback_failure
    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(lambda name: iter(())))
    with pytest.raises(RuntimeError) as raised:
        _query_with_source(registry, database_path)
    assert raised.value is rollback_failure
    assert counts["close"] == 2

    _FakeDatabase.exit_error = None
    close_failure = RuntimeError("CLOSE_SENTINEL")
    _FakeDatabase.close_error = close_failure
    registry = _registry(monkeypatch, ["focus"], handler=_InstanceHandler(lambda name: iter(())))
    with pytest.raises(RuntimeError) as raised:
        _query_with_source(registry, database_path)
    assert raised.value is close_failure


def test_repeated_hook_scale_uses_one_source_and_one_query_per_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.resources import graph_query

    counts = {
        "database": 0,
        "transaction_enter": 0,
        "transaction_exit": 0,
        "close": 0,
        "list_vms": 0,
        "list_sessions": 0,
    }
    _QueryCountingDatabase.counts = counts
    _QueryCountingDatabase.exit_error = None
    _QueryCountingDatabase.close_error = None
    monkeypatch.setattr(graph_query, "Database", _QueryCountingDatabase)

    identities = [("kind-a" if index % 2 == 0 else "kind-b", f"resource-{index:03}") for index in range(40)]
    references_by_source: dict[tuple[str, str], list[ResourceReference]] = {}
    for source_identity, target_identity in zip(identities, identities[1:], strict=False):
        references_by_source.setdefault(source_identity, []).append(
            ResourceReference(
                name=target_identity[1],
                kind=target_identity[0],
                usage=f"{source_identity[1]}-TO-{target_identity[1]}",
                source=source_identity,
            )
        )

    KIND_REGISTRY.pop("kind-a", None)
    KIND_REGISTRY.pop("kind-b", None)
    registry = Registry.empty()
    for kind, name in identities:
        registry.add(
            kind,
            name,
            _Row(name, tuple(references_by_source.get((kind, name), ()))),
            Origin.built_in(source="tests.graph-query"),
        )
    registry.finalize()
    handler_a = _ListQueryHandler(lambda db: db.list_vms())
    handler_b = _ListQueryHandler(lambda db: db.list_sessions())
    monkeypatch.setitem(KIND_REGISTRY, "kind-a", handler_a)
    monkeypatch.setitem(KIND_REGISTRY, "kind-b", handler_b)

    path = _PresentPath()
    source = DatabaseLiveSource(cast("Path", path))
    assert source.supports("kind-a") is True
    assert source.supports("kind-b") is True
    assert path.stat_count == 0
    result = show_graph(
        registry,
        ResourceIdentity(*identities[0]),
        GraphDirection.BOTH,
        None,
        source,
    )
    assert len(result.nodes) == 40
    assert path.stat_count == 1
    assert counts == {
        "database": 1,
        "transaction_enter": 1,
        "transaction_exit": 1,
        "close": 1,
        "list_vms": 20,
        "list_sessions": 20,
    }
    assert handler_a.calls == [name for kind, name in identities if kind == "kind-a"]
    assert handler_b.calls == [name for kind, name in identities if kind == "kind-b"]


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
    controls = "\x00\x07\t\n\x1b\x7f\x80\x9f"

    def unsafe(marker: str) -> str:
        return f"{marker[:1]}{controls}{marker[1:]}"

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
    assert all(control not in rendered for control in controls)
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
    document = encode_json_envelope(MachineOutputCommand.DOCTOR, graph_result_data(result))
    assert b"\\u007f\\u0080" in document
    assert b"\x7f" not in document
    assert b"\xc2\x80" not in document
