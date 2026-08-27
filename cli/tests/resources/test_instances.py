"""Registry-backed direct live-user projections."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from typer.testing import CliRunner

from agentworks.bootstrap import build_registry
from agentworks.cli import app
from agentworks.config import load_config
from agentworks.db import Database
from agentworks.resources import GraphDirection, Registry, show_graph
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph_query import GraphEdgeType, GraphNodeType
from agentworks.resources.inspect import list_resources
from tests.conftest import ManifestDoc, write_manifests


def _write_base(config_path: Path, *manifests: ManifestDoc) -> None:
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
    )
    if manifests:
        write_manifests(config_path.parent, *manifests)


def _seed_basic(tmp_path: Path) -> tuple[Database, Registry]:
    cfg = tmp_path / "config.toml"
    _write_base(cfg, ManifestDoc("vm-template", "custom", {"cpus": 4}))
    config = load_config(cfg, warn_issues=False)
    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-default", site="lima-local", hostname="lima--vm-default", template=None)
    db.insert_vm("vm-custom", site="lima-local", hostname="lima--vm-custom", template="custom")
    return db, build_registry(config, live_database=db)


def test_list_resources_populates_direct_live_owner_counts(tmp_path: Path) -> None:
    db, registry = _seed_basic(tmp_path)
    try:
        listing = list_resources(registry, kinds=("vm-template",))
    finally:
        db.close()

    assert {row.name: row.used_by_count for row in listing.rows} == {
        "custom": 1,
        "default": 1,
    }


def test_graph_query_projects_live_usage_from_finalized_registry(tmp_path: Path) -> None:
    db, registry = _seed_basic(tmp_path)
    try:
        result = show_graph(
            registry,
            ResourceIdentity("vm-template", "custom"),
            GraphDirection.DEPENDENTS,
            1,
        )
    finally:
        db.close()

    assert {
        (node.node_type, node.kind, node.name, node.distance)
        for node in result.nodes
        if node.node_type is GraphNodeType.LIVE_INSTANCE
    } == {(GraphNodeType.LIVE_INSTANCE, "vm", "vm-custom", 1)}
    assert [
        (edge.source.kind, edge.source.name, edge.target.kind, edge.target.name)
        for edge in result.edges
        if edge.edge_type is GraphEdgeType.LIVE_USAGE
    ] == [("vm", "vm-custom", "vm-template", "custom")]


def _column_cells(stdout: str, header: str, next_header: str) -> list[str]:
    lines = stdout.splitlines()
    heading = next(line for line in lines if header in line and next_header in line)
    start, stop = heading.index(header), heading.index(next_header)
    body = lines[lines.index(heading) + 2 :]
    return [line[start:stop].strip() for line in body if line.strip()]


def test_list_view_renders_dash_for_kind_without_used_by_contract(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    db = Database(tmp_path / "renderer.db")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    try:
        with patch("agentworks.cli.commands.resource.get_db", return_value=db):
            result = CliRunner().invoke(app, ["resource", "list", "--kind", "secret-backend"])
    finally:
        db.close()

    assert result.exit_code == 0, result.stdout
    cells = _column_cells(result.stdout, "USED BY", "DESCRIPTION")
    assert cells
    assert set(cells) == {"-"}
