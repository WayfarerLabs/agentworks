"""Database-backed and pending live resources join Registry finalization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentworks import db as db_module
from agentworks.bootstrap import build_registry
from agentworks.capabilities.secret_backend import OperatorImpact, TtyInteractionAccess
from agentworks.config import load_config
from agentworks.db import LATEST_VERSION, Database, SessionMode, VersionedPayload
from agentworks.doctor import _check_secrets
from agentworks.errors import NotFoundError, StateError
from agentworks.resources import InstanceRef, LiveResource, Registry
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph_query import (
    GraphDirection,
    GraphEdgeType,
    GraphNodeType,
    show_graph,
)
from agentworks.resources.live import LIVE_RESOURCE_KINDS
from agentworks.resources.live_publish import publish_database_live_resources
from agentworks.resources.reference import SecretReference
from agentworks.secrets.inspect import build_secret_table, describe_secret
from agentworks.secrets.verification import verify_secrets
from tests.conftest import ManifestDoc, write_cfg


def _persist_agent(db: Database, name: str, secret: str) -> None:
    if db.get_vm("box") is None:
        db.insert_vm("box", "lima-local", "box")
    db.insert_agent(name, "box", name)
    db.instance_state.put_desired_overlay(
        "agent",
        name,
        VersionedPayload(1, {"env": {"TOKEN": {"secret": secret}}}),
    )


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *manifests: ManifestDoc):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    config = load_config(write_cfg(tmp_path, *manifests), warn_issues=False)
    return path, config


def _direct_live_dependents(registry: Registry, kind: str, name: str) -> set[tuple[str, str]]:
    # Depth one preserves directness; LIVE_USAGE makes live-row misclassification observable.
    result = show_graph(
        registry,
        ResourceIdentity(kind, name),
        GraphDirection.DEPENDENTS,
        1,
    )
    return {(edge.source.kind, edge.source.name) for edge in result.edges if edge.edge_type is GraphEdgeType.LIVE_USAGE}


def test_database_publisher_frames_path_inspection_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.db"
    original_stat = Path.stat

    def fail_selected_path(candidate: Path, *args: object, **kwargs: object):  # noqa: ANN202
        if candidate == path:
            raise PermissionError("blocked")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_selected_path)
    with pytest.raises(StateError) as caught:
        publish_database_live_resources(Registry(), path)
    assert caught.value.entity_kind == "database"


def test_database_publisher_translates_malformed_current_schema_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_VERSION,))
    connection.commit()
    connection.close()

    with pytest.raises(StateError) as caught:
        publish_database_live_resources(Registry(), path)

    assert caught.value.entity_kind == "database"


def test_database_publisher_translates_malformed_current_row_shapes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    database.insert_vm("box", "lima-local", "box")
    database._conn.execute("ALTER TABLE vms DROP COLUMN admin_template")
    database._conn.commit()
    database.close()

    with pytest.raises(StateError) as caught:
        publish_database_live_resources(Registry(), path)

    assert caught.value.entity_kind == "database"


def test_agent_overlay_auto_declares_secret_and_live_graph_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "dev", "instance-token")
    db.close()

    registry = build_registry(config)

    secret = registry.lookup("secret", "instance-token")
    assert secret.origin.variant == "auto-declared"
    assert secret.origin.source == ("agent", "dev")
    assert secret.description
    result = show_graph(
        registry,
        ResourceIdentity("secret", "instance-token"),
        GraphDirection.DEPENDENTS,
        1,
    )
    assert any(
        edge.edge_type is GraphEdgeType.LIVE_USAGE
        and edge.source.node_type is GraphNodeType.LIVE_INSTANCE
        and (edge.source.kind, edge.source.name) == ("agent", "dev")
        for edge in result.edges
    )
    live_result = show_graph(
        registry,
        ResourceIdentity("agent", "dev"),
        GraphDirection.DEPENDENCIES,
        1,
    )
    assert live_result.nodes[0].node_type is GraphNodeType.LIVE_INSTANCE
    assert {(edge.target.node_type, edge.target.kind, edge.target.name) for edge in live_result.edges} >= {
        (GraphNodeType.LIVE_INSTANCE, "vm", "box"),
        (GraphNodeType.RESOURCE, "secret", "instance-token"),
    }
    assert [row.name for row in build_secret_table(config, registry).rows] == [
        "instance-token",
        "tailscale-auth-key",
    ]
    description = describe_secret(
        config,
        registry,
        "instance-token",
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert description.used_by == ()
    (verification,) = verify_secrets(
        config,
        registry,
        ["instance-token"],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert verification.name == "instance-token"
    doctor_group = _check_secrets(
        config,
        registry,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert "instance-token" in {
        check.secret_preview.name for check in doctor_group.checks if check.secret_preview is not None
    }


def test_names_only_commands_include_database_overlay_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "dev", "completion-token")
    db.close()
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config.source_path)

    secret_result = CliRunner().invoke(app, ["secret", "list", "--names-only"])
    resource_result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "secret", "--names-only"],
    )

    assert secret_result.exit_code == 0, secret_result.output
    assert resource_result.exit_code == 0, resource_result.output
    assert "completion-token" in secret_result.stdout.splitlines()
    assert "secret/completion-token" in resource_result.stdout.splitlines()


def test_secret_used_by_v1_projects_session_reachability_from_live_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "dev", "session-token")
    db.insert_workspace("work", "/work", "box", "work")
    db.insert_session(
        "run",
        "work",
        "default",
        SessionMode.AGENT,
        agent_name="dev",
        socket_path="/tmp/run.sock",
    )
    db.close()

    registry = build_registry(config)
    description = describe_secret(
        config,
        registry,
        "session-token",
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )

    assert _direct_live_dependents(registry, "secret", "session-token") == {("agent", "dev")}
    assert description.used_by == (InstanceRef("session", "run"),)


def test_secret_used_by_v1_preserves_mode_sensitive_effective_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.instance_specs import parse_vm_instance_specs

    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    db.insert_vm("box", "lima-local", "box")
    vm_overlays = parse_vm_instance_specs(
        '{"env":{"VM_TOKEN":{"secret":"vm-token"}}}',
        '{"env":{"ADMIN_TOKEN":{"secret":"admin-token"}}}',
    )
    assert vm_overlays is not None
    db.instance_state.put_desired_overlay("vm", "box", vm_overlays.payload)
    db.insert_workspace("work", "/work", "box", "work")
    db.instance_state.put_desired_overlay(
        "workspace",
        "work",
        VersionedPayload(1, {"env": {"WORK_TOKEN": {"secret": "workspace-token"}}}),
    )
    db.insert_agent("dev", "box", "dev")
    db.instance_state.put_desired_overlay(
        "agent",
        "dev",
        VersionedPayload(1, {"env": {"AGENT_TOKEN": {"secret": "agent-token"}}}),
    )
    db.insert_session("admin-run", "work", "default", SessionMode.ADMIN)
    db.insert_session(
        "agent-run",
        "work",
        "default",
        SessionMode.AGENT,
        agent_name="dev",
        socket_path="/tmp/agent-run.sock",
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('future-run', 'work', 'default', 'future-mode', 'dev', '/tmp/future-run.sock')"
    )
    db._conn.commit()
    for name in ("admin-run", "agent-run"):
        db.instance_state.put_desired_overlay(
            "session",
            name,
            VersionedPayload(1, {"env": {"SESSION_TOKEN": {"secret": "session-token"}}}),
        )

    registry = build_registry(config, live_database=db)

    def users(secret: str) -> tuple[InstanceRef, ...]:
        return registry.graph.compatibility_live_users_of("secret", secret) or ()

    known_sessions = (InstanceRef("session", "admin-run"), InstanceRef("session", "agent-run"))
    all_sessions = (*known_sessions, InstanceRef("session", "future-run"))
    assert users("vm-token") == all_sessions
    assert users("workspace-token") == all_sessions
    assert users("session-token") == known_sessions
    assert users("admin-token") == (InstanceRef("session", "admin-run"),)
    assert users("agent-token") == (InstanceRef("session", "agent-run"),)
    assert _direct_live_dependents(registry, "secret", "admin-token") == {("vm", "box")}
    assert _direct_live_dependents(registry, "secret", "agent-token") == {("agent", "dev")}
    db.close()


def test_secret_used_by_v1_crosses_a_declared_runtime_subgraph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(
        tmp_path,
        monkeypatch,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github", "source": {"mode": "secret"}}}),
        ManifestDoc("admin-template", "default", {"git_credentials": ["github"]}),
    )
    db = Database(path)
    db.insert_vm("box", "lima-local", "box")
    db.insert_workspace("work", "/work", "box", "work")
    db.insert_session("admin-run", "work", "default", SessionMode.ADMIN)

    registry = build_registry(config, live_database=db)

    secret = "git-token-github"
    vm_targets = {(ref.kind, ref.name) for ref in registry.graph.edges_of("vm", "box")}
    assert ("git-credential", "github") in vm_targets
    assert ("secret", secret) not in vm_targets
    assert ("secret", secret) in registry.graph.runtime_reachable_from("git-credential", "github")
    description = describe_secret(
        config,
        registry,
        secret,
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert description.used_by == (InstanceRef("session", "admin-run"),)
    db.close()


def test_missing_live_graph_focus_uses_domain_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    db.insert_vm("box", "lima-local", "box")
    db.close()
    registry = build_registry(config)

    with pytest.raises(NotFoundError) as caught:
        show_graph(
            registry,
            ResourceIdentity("vm", "missing"),
            GraphDirection.BOTH,
            1,
        )
    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "missing"


def test_explicit_secret_wins_over_live_auto_declaration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(
        tmp_path,
        monkeypatch,
        ManifestDoc("secret", "instance-token", description="Explicit"),
    )
    db = Database(path)
    _persist_agent(db, "dev", "instance-token")
    db.close()

    secret = build_registry(config).lookup("secret", "instance-token")

    assert secret.origin.variant == "operator-declared"
    assert secret.description == "Explicit"


def test_live_auto_declaration_tracks_multiple_owners_and_last_owner_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "one", "shared-token")
    _persist_agent(db, "two", "shared-token")

    registry = build_registry(config)
    assert _direct_live_dependents(registry, "secret", "shared-token") == {
        ("agent", "one"),
        ("agent", "two"),
    }

    db.delete_agent("one")
    assert build_registry(config).lookup("secret", "shared-token")

    db.delete_agent("two")
    with pytest.raises(KeyError):
        build_registry(config).lookup("secret", "shared-token")
    db.close()


def test_pending_live_resource_uses_normal_finalize_without_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)

    def publish(registry: Registry) -> None:
        registry.add_live(
            LiveResource(
                "agent",
                "pending",
                (
                    SecretReference(
                        name="pending-token",
                        kind="secret",
                        usage="the pending token",
                        source=("agent", "pending"),
                    ),
                ),
            )
        )

    prospective = build_registry(config, pending_publishers=(publish,))
    assert prospective.lookup("secret", "pending-token")
    with pytest.raises(KeyError):
        build_registry(config).lookup("secret", "pending-token")
    assert not path.exists()


def test_pending_replacement_claims_identity_without_mutating_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "dev", "durable-token")
    db.close()

    def publish(registry: Registry) -> None:
        registry.add_live(
            LiveResource(
                "agent",
                "dev",
                (
                    SecretReference(
                        name="candidate-token",
                        kind="secret",
                        usage="the candidate token",
                        source=("agent", "dev"),
                    ),
                ),
            )
        )

    prospective = build_registry(config, pending_publishers=(publish,))
    assert prospective.lookup("secret", "candidate-token")
    with pytest.raises(KeyError):
        prospective.lookup("secret", "durable-token")

    durable = build_registry(config)
    assert durable.lookup("secret", "durable-token")
    with pytest.raises(KeyError):
        durable.lookup("secret", "candidate-token")


def test_database_publishes_intrinsic_live_instance_relationships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    db.insert_vm("box", "lima-local", "box")
    db.insert_workspace("work", "/work", "box", "work")
    db.insert_agent("dev", "box", "dev")
    db.insert_session(
        "run",
        "work",
        "default",
        SessionMode.AGENT,
        agent_name="dev",
        socket_path="/tmp/run.sock",
    )
    db.insert_console("dash", "box")
    db.add_console_session("dash", "run", [{"cwd": None, "admin": False}])
    db.close()

    registry = build_registry(config)

    published = {
        ("vm", "box"),
        ("workspace", "work"),
        ("agent", "dev"),
        ("session", "run"),
        ("console", "dash"),
    }
    assert {kind for kind, name in published if registry.graph.is_live(kind, name)} == LIVE_RESOURCE_KINDS

    def targets(kind: str, name: str) -> set[tuple[str, str]]:
        return {(ref.kind, ref.name) for ref in registry.graph.edges_of(kind, name)}

    assert ("vm-site", "lima-local") in targets("vm", "box")
    assert ("vm", "box") in targets("workspace", "work")
    assert ("vm", "box") in targets("agent", "dev")
    assert targets("session", "run") >= {("workspace", "work"), ("agent", "dev")}
    assert targets("console", "dash") >= {("vm", "box"), ("session", "run")}


def test_caller_owned_read_only_database_publishes_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    writer = Database(path)
    _persist_agent(writer, "dev", "read-only-token")
    writer.close()

    reader = Database(path, read_only=True)
    try:
        registry = build_registry(config, live_database=reader)
    finally:
        reader.close()

    assert registry.lookup("secret", "read-only-token")


def test_finalized_registry_queries_do_not_reinspect_mutated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    _persist_agent(db, "dev", "snapshot-token")
    registry = build_registry(config, live_database=db)

    db.delete_agent("dev")
    result = show_graph(
        registry,
        ResourceIdentity("secret", "snapshot-token"),
        GraphDirection.DEPENDENTS,
        1,
    )
    assert any((edge.source.kind, edge.source.name) == ("agent", "dev") for edge in result.edges)
    description = describe_secret(
        config,
        registry,
        "snapshot-token",
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert description.used_by == ()
    with pytest.raises(KeyError):
        build_registry(config, live_database=db).lookup("secret", "snapshot-token")
    db.close()


def test_durable_stranded_selectors_are_omitted_without_default_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    db.insert_vm("stranded", "removed-site", "stranded", template="removed-vm-template")
    db.insert_workspace(
        "copied",
        "/work",
        "stranded",
        "work",
        template="removed-workspace-template",
    )
    db.instance_state.put_desired_overlay(
        "workspace",
        "copied",
        VersionedPayload(1, {"env": {"TOKEN": {"secret": "unresolved-token"}}}),
    )

    registry = build_registry(config, live_database=db)

    assert registry.graph.is_live("vm", "stranded")
    assert registry.graph.is_live("workspace", "copied")
    assert {(ref.kind, ref.name) for ref in registry.graph.edges_of("vm", "stranded")} == {
        ("admin-template", "default"),
    }
    assert {(ref.kind, ref.name) for ref in registry.graph.edges_of("workspace", "copied")} == {
        ("vm", "stranded"),
    }
    with pytest.raises(KeyError):
        registry.lookup("secret", "unresolved-token")
    db.close()


def test_empty_durable_selectors_do_not_substitute_default_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(
        tmp_path,
        monkeypatch,
        ManifestDoc("vm-template", "default", {"env": {"VM_TOKEN": {"secret": "vm-default-token"}}}),
        ManifestDoc(
            "admin-template",
            "default",
            {"env": {"ADMIN_TOKEN": {"secret": "admin-default-token"}}},
        ),
        ManifestDoc(
            "workspace-template",
            "default",
            {"env": {"WORK_TOKEN": {"secret": "workspace-default-token"}}},
        ),
        ManifestDoc(
            "agent-template",
            "default",
            {"env": {"AGENT_TOKEN": {"secret": "agent-default-token"}}},
        ),
    )
    db = Database(path)
    db.insert_vm("box", "lima-local", "box", template="", admin_template="")
    db.insert_workspace("work", "/work", "box", "work", template="")
    db.insert_agent("dev", "box", "dev", template="")
    db.instance_state.put_desired_overlay(
        "workspace",
        "work",
        VersionedPayload(1, {"env": {"OVERLAY_TOKEN": {"secret": "workspace-overlay-token"}}}),
    )

    registry = build_registry(config, live_database=db)

    expected_targets = {
        ("vm", "box"): {("vm-site", "lima-local")},
        ("workspace", "work"): {("vm", "box")},
        ("agent", "dev"): {("vm", "box")},
    }
    for (kind, name), expected in expected_targets.items():
        assert registry.graph.is_live(kind, name)
        assert {(ref.kind, ref.name) for ref in registry.graph.edges_of(kind, name)} == expected

    for secret in (
        "vm-default-token",
        "admin-default-token",
        "workspace-default-token",
        "agent-default-token",
        "tailscale-auth-key",
    ):
        assert _direct_live_dependents(registry, "secret", secret) == set()
    with pytest.raises(KeyError):
        registry.lookup("secret", "workspace-overlay-token")

    from agentworks.workspaces.templates import resolve_live_template

    empty_selected = resolve_live_template(db, registry, "work", "")
    default_selected = resolve_live_template(db, registry, "work", None)
    assert empty_selected.name == ""
    assert "WORK_TOKEN" not in empty_selected.env
    assert empty_selected.env["OVERLAY_TOKEN"].model_dump() == {"secret": "workspace-overlay-token"}
    assert default_selected.name == "default"
    assert default_selected.env["WORK_TOKEN"].model_dump() == {"secret": "workspace-default-token"}
    assert default_selected.env["OVERLAY_TOKEN"].model_dump() == {"secret": "workspace-overlay-token"}
    db.close()


def test_durable_missing_owner_keeps_live_node_without_inventing_owner_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, config = _registry(tmp_path, monkeypatch)
    db = Database(path)
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db.insert_workspace("orphan", "/work", "removed-vm", "work")

    registry = build_registry(config, live_database=db)

    assert registry.graph.is_live("workspace", "orphan")
    assert {(ref.kind, ref.name) for ref in registry.graph.edges_of("workspace", "orphan")} == {
        ("workspace-template", "default"),
    }
    db.close()


def test_absent_database_is_an_empty_live_publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, config = _registry(tmp_path, monkeypatch)

    registry = build_registry(config)

    assert registry.is_finalized
    assert not path.exists()
