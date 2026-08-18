"""Command-boundary tests for ``agw graph show`` and the resource cutover."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import NotFoundError, StateError
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph_query import (
    DatabaseLiveSource,
    GraphDirection,
    GraphNode,
    GraphNodeType,
    GraphQuery,
    GraphResult,
)
from tests.conftest import ManifestDoc, write_manifests


def _result(
    *,
    focus: ResourceIdentity | None = None,
    direction: GraphDirection = GraphDirection.BOTH,
    depth_limit: int | None = 1,
) -> GraphResult:
    if focus is None:
        focus = ResourceIdentity("secret", "legacy--name:part.with.dot")
    return GraphResult(
        query=GraphQuery(focus, direction, depth_limit),
        nodes=(GraphNode(GraphNodeType.RESOURCE, focus.kind, focus.name, 0),),
        edges=(),
    )


def test_graph_show_wires_one_query_with_closed_parsed_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentworks import bootstrap, config, db
    from agentworks.resources import graph_query, graph_render

    registry = object()
    calls: list[tuple[object, ...]] = []
    expected = _result(depth_limit=None, direction=GraphDirection.DEPENDENCIES)
    monkeypatch.setattr(config, "load_config", lambda **kwargs: calls.append(("config", kwargs)) or object())
    monkeypatch.setattr(
        bootstrap,
        "load_request_registry",
        lambda _config, **kwargs: calls.append(("registry", kwargs)) or registry,
    )
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "canonical.db")

    def query(
        actual_registry: object,
        focus: ResourceIdentity,
        direction: GraphDirection,
        depth_limit: int | None,
        source: DatabaseLiveSource,
    ) -> GraphResult:
        calls.append(("query", actual_registry, focus, direction, depth_limit, source))
        return expected

    monkeypatch.setattr(graph_query, "show_graph", query)
    monkeypatch.setattr(graph_render, "render_graph_result", lambda result: calls.append(("render", result)))

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "show",
            "secret/legacy--name:part.with.dot",
            "--direction",
            "dependencies",
            "--depth",
            "all",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0] == ("config", {"warn_issues": True, "workload_gated_issues_fatal": False})
    assert calls[1] == ("registry", {"warn": True, "probe_host_readiness": False})
    query_call = calls[2]
    assert query_call[0:5] == (
        "query",
        registry,
        ResourceIdentity("secret", "legacy--name:part.with.dot"),
        GraphDirection.DEPENDENCIES,
        None,
    )
    source = query_call[5]
    assert isinstance(source, DatabaseLiveSource)
    assert source.database_path == tmp_path / "canonical.db"
    assert calls[3] == ("render", expected)


def test_graph_show_json_projects_the_same_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import graph_query

    expected = _result(depth_limit=2, direction=GraphDirection.DEPENDENTS)
    monkeypatch.setattr(config, "load_config", lambda **kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **kwargs: object())
    monkeypatch.setattr(graph_query, "show_graph", lambda *_args: expected)

    result = CliRunner().invoke(
        app,
        ["graph", "show", "secret/token", "--direction", "dependents", "--depth", "2", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["command"] == "graph.show"
    assert document["data"]["query"] == {
        "focus": {"kind": expected.query.focus.kind, "name": expected.query.focus.name},
        "direction": "dependents",
        "depth_limit": 2,
    }
    assert document["data"]["nodes"] == [
        {
            "node_type": "resource",
            "kind": expected.query.focus.kind,
            "name": expected.query.focus.name,
            "distance": 0,
        }
    ]
    assert document["data"]["edges"] == []


def test_missing_ssh_keys_do_not_block_graph_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config whose only defect is a nonexistent operator SSH key path
    (the sample config's placeholder, before ``agw config init`` writes a
    real one) must not stop `graph show` from querying resource
    relationships: it needs no operator identity (host readiness probing
    is off, and no platform preflight touches ``config.operator``)."""
    from agentworks import config

    write_manifests(tmp_path, ManifestDoc("secret", "npm-token", description="npm registry token"))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"

        [secret_config]
        sources = ["env-var"]
        """)
    )
    assert not (tmp_path / "id.pub").exists()
    assert not (tmp_path / "id").exists()
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    result = CliRunner().invoke(app, ["graph", "show", "secret/npm-token"])

    assert result.exit_code == 0, result.output
    assert "secret/npm-token" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["graph", "show", "missing-slash"],
        ["graph", "show", "secret/token", "--depth", "0"],
        ["graph", "show", "secret/token", "--depth", "-1"],
        ["graph", "show", "secret/token", "--depth", "two"],
        ["graph", "show", "secret/token", "--direction", "sideways"],
        ["graph", "show", "secret/token", "--output", "yaml"],
    ],
)
def test_graph_grammar_errors_precede_config(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    from agentworks import config

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: (_ for _ in ()).throw(AssertionError()))
    result = CliRunner().invoke(app, argv)

    assert result.exit_code != 0
    assert result.stdout_bytes == b""


@pytest.mark.parametrize("error", [NotFoundError("missing"), StateError("query failed")])
def test_graph_query_failure_writes_no_partial_result(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import graph_query

    monkeypatch.setattr(config, "load_config", lambda **kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **kwargs: object())
    monkeypatch.setattr(graph_query, "show_graph", lambda *_args: (_ for _ in ()).throw(error))

    result = CliRunner().invoke(app, ["graph", "show", "secret/missing", "--output", "json"])

    assert result.exit_code != 0
    assert result.stdout_bytes == b""
    assert result.exception is error


def test_resource_explain_is_config_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import config

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: (_ for _ in ()).throw(AssertionError()))
    result = CliRunner().invoke(app, ["resource", "explain", "secret"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["resource", "describe", "secret/token"],
        ["resource", "describe-kind", "secret"],
        ["resource", "schema", "--write"],
    ],
)
def test_retired_resource_spellings_do_not_dispatch(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code != 0
