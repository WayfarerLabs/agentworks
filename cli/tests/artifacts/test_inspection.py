"""Read-only inspection follows actual ancestry and excludes artifact contents."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agentworks.artifacts.inspection import resolve_context
from agentworks.db import SessionMode
from agentworks.errors import NotFoundError, ValidationError
from tests.artifacts._fixtures import group

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.fixture
def lineage(db: Database) -> Database:
    for name in ("vm", "other"):
        db.insert_vm(name, site="local", hostname=name)
    db.insert_workspace("work", workspace_path="/work", vm_name="vm", linux_group="ws-work", template="default")
    db.insert_workspace(
        "other-work", workspace_path="/other", vm_name="other", linux_group="ws-other", template="default"
    )
    db.insert_agent("developer", "vm", "agt-developer", "default")
    db.insert_agent("stranger", "other", "agt-stranger", "default")
    db.insert_session("review", "work", "default", SessionMode.AGENT, agent_name="developer", socket_path="/socket")
    db.insert_session("admin-review", "work", "default", SessionMode.ADMIN)
    return db


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"admin": True},
        {"vm_name": "vm", "admin": True, "agent_name": "developer"},
        {"workspace_name": "work", "admin": True},
        {"workspace_name": "work", "agent_name": "developer"},
        {"session_name": "review", "workspace_name": "other-work"},
        {"session_name": "review", "agent_name": "stranger"},
        {"session_name": "review", "admin": True},
        {"session_name": "admin-review", "agent_name": "developer"},
        {"session_name": "review", "vm_name": "other"},
    ],
)
def test_conflicting_or_missing_context_is_rejected(lineage: Database, selector: dict) -> None:
    with pytest.raises(ValidationError):
        resolve_context(lineage, **selector)


@pytest.mark.parametrize(
    "selector",
    [{"vm_name": "unknown"}, {"session_name": "unknown"}, {"workspace_name": "unknown"}, {"agent_name": "unknown"}],
)
def test_unknown_owner_is_an_error(lineage: Database, selector: dict) -> None:
    with pytest.raises(NotFoundError):
        resolve_context(lineage, **selector)


def test_workspace_and_agent_do_not_acquire_admin_ancestors(lineage: Database) -> None:
    workspace = resolve_context(lineage, workspace_name="work")
    assert workspace.vm.name == "vm" and workspace.workspace is not None
    assert workspace.agent is None and not workspace.admin
    agent = resolve_context(lineage, agent_name="developer")
    assert agent.vm.name == "vm" and agent.agent is not None
    assert agent.workspace is None and not agent.admin
    vm = resolve_context(lineage, vm_name="vm")
    assert not vm.admin and vm.agent is None and vm.workspace is None


def test_session_selects_actual_user_and_accepts_matching_ancestor_flags(lineage: Database) -> None:
    agent = resolve_context(lineage, session_name="review", workspace_name="work", agent_name="developer", vm_name="vm")
    assert agent.agent is not None and agent.agent.name == "developer" and not agent.admin
    admin = resolve_context(lineage, session_name="admin-review", admin=True, vm_name="vm")
    assert admin.admin and admin.agent is None and admin.workspace is not None


def test_inspection_shows_handled_ancestor_metadata_without_acquisition(db: Database, monkeypatch, capsys) -> None:
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
    from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
    from agentworks.origin import Origin
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    item = tuple(fixture.captures["agent"].inputs.items())[0]
    fixture.save(
        "agent",
        inherited=fixture.captures["vm"].inputs,
        files=(
            OwnedArtifactFile(
                path="/home/worker/.agents/skills/review/SKILL.md",
                sha256="a" * 64,
                origins=(item.origin_identity,),
                native_identity="review",
            ),
        ),
    )
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="test")
    )

    def forbid(*args, **kwargs):
        raise AssertionError("inspection must not acquire content or resolve secrets")

    monkeypatch.setattr("agentworks.artifacts.state.capture_owner", forbid)
    monkeypatch.setattr("agentworks.artifacts.capture.capture_artifacts", forbid)
    monkeypatch.setattr("agentworks.secrets.orchestration.resolve_for_command", forbid)
    result = inspect_artifacts(db, fixture.registry, agent_name="agent", integration_name="shell")
    assert [owner.scope for owner in result.owners] == ["vm", "agent"]
    assert result.owners[0].integrations[0].status == "inactive"
    user = result.owners[1]
    assert user.artifacts[0].origin_id == item.origin_identity
    assert user.integrations[0].status == "current"
    assert user.integrations[0].recorded_handled == (
        tuple(fixture.captures["vm"].inputs.items())[0].identity,
        item.identity,
    )
    assert user.integrations[0].placements[0].native_identity == "review"
    encoded = json.dumps(inspection_data(result))
    assert item.content.text not in encoded
    render_artifacts(result)
    assert item.content.text not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["missing", "stale", "incomplete", "malformed"])
def test_unknown_or_stale_evidence_never_becomes_empty_success(db: Database, case: str) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts
    from agentworks.artifacts.state import CapturedArtifacts, write_capture
    from agentworks.db import AppliedStateKey, VersionedPayload
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    fixture.save("agent", complete=case != "incomplete")
    if case == "missing":
        db.instance_state.clear_applied_slice("agent", "agent", AppliedStateKey.ARTIFACT_INPUTS)
    elif case == "stale":
        snapshot = fixture.captures["agent"]
        write_capture(db, "agent", "agent", "agent", CapturedArtifacts("f" * 64, snapshot.inputs), operation="test")
    elif case == "malformed":
        db.instance_state.replace_applied_slices(
            "agent",
            "agent",
            "test",
            {AppliedStateKey.HARNESS_NATIVE_SETUP: VersionedPayload(2, {"secret": "never-print-me"})},
        )
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    owner = result.owners[-1]
    assert owner.bundles == ("agent-bundle",)
    if case == "missing":
        assert owner.capture_status == "missing"
        assert owner.artifacts[0].input_id is None
    elif case == "stale":
        assert owner.capture_status == "stale" and owner.artifacts[0].input_id is not None
    elif case == "incomplete":
        assert owner.integrations[0].status == "incomplete"
        assert not owner.integrations[0].recorded_handled
    else:
        assert owner.diagnostics
        assert "never-print-me" not in repr(result)


def test_without_integration_filter_inspection_does_not_enumerate_installed_plugins(db: Database) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts
    from tests.artifacts.test_routing import graph

    fixture = graph(db)
    result = inspect_artifacts(db, fixture.registry, workspace_name="workspace")
    assert [owner.scope for owner in result.owners] == ["vm", "workspace"]
    assert all(not owner.integrations for owner in result.owners)
    assert all(owner.artifacts for owner in result.owners)


def test_cli_json_reads_existing_state_without_publication_or_disclosing_bodies(db: Database, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    fixture.save("agent")
    monkeypatch.setattr("agentworks.db.DB_PATH", db.path)
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: object())

    def registry(config, **kwargs):
        assert not kwargs["include_live_resources"]
        assert not kwargs["probe_host_readiness"]
        return fixture.registry

    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", registry)
    before = tuple(db.instance_state.get_applied_slices("agent", "agent"))
    result = CliRunner().invoke(app, ["artifacts", "show", "--agent", "agent", "--output", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["command"] == "artifacts.show"
    assert [owner["scope"] for owner in data["data"]["owners"]] == ["vm", "agent"]
    assert tuple(fixture.captures["agent"].inputs.items())[0].content.text not in result.stdout
    assert tuple(db.instance_state.get_applied_slices("agent", "agent")) == before


def test_cli_refuses_stale_state_without_migration(tmp_path, monkeypatch) -> None:
    import sqlite3

    from typer.testing import CliRunner

    from agentworks.cli import app
    from agentworks.resources.registry import Registry
    from tests.database_support import build_schema

    path = tmp_path / "old.db"
    build_schema(path, 37)
    before = path.read_bytes()
    monkeypatch.setattr("agentworks.db.DB_PATH", path)
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: object())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config, **kwargs: Registry.empty())
    result = CliRunner().invoke(app, ["artifacts", "show", "--vm", "old"])
    assert result.exit_code != 0
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 37


def test_session_inspection_uses_runtime_diamond_order(db: Database) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import write_native_setup
    from agentworks.schema import CapabilityBlock
    from agentworks.secrets.orchestration import SecretTarget
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("vm", "agent", "workspace"), counts={"vm": 3})
    vm = fixture.captures["vm"].inputs
    fixture.save("vm", routes={"vm-0": "user", "vm-1": "workspace", "vm-2": "session"})
    fixture.save("agent", inherited=group(vm.hints["vm-0"]), routes={"vm-0": "session", "agent-0": "session"})
    fixture.save("workspace", inherited=group(vm.hints["vm-1"]), routes={"vm-1": "session", "workspace-0": "session"})
    db.insert_session("review", "workspace", "default", SessionMode.AGENT, agent_name="agent", socket_path="/socket")
    block = CapabilityBlock.of("shell")
    owner = SetupInputs(
        "session", "review", "session", (block,), SecretTarget(vm={}, agent={}, workspace={}, session={})
    )
    prepared = fixture.route().inputs
    record = SetupRecord(
        component="session",
        integration="shell",
        destination_id="d" * 64,
        declaration=owner.declaration(block),
        complete=True,
        artifact_inputs=tuple(item.identity for item in prepared.items()),
    )
    write_native_setup(db, "session", "review", NativeSetupState(records=(record,)), operation="fixture")
    result = inspect_artifacts(db, fixture.registry, session_name="review")
    assert [owner.scope for owner in result.owners] == ["vm", "agent", "workspace", "session"]
    assert result.owners[-1].integrations[0].status == "current"
    assert result.owners[-1].integrations[0].recorded_handled == tuple(item.identity for item in prepared.items())


def test_worked_manifests_build_without_acquiring_their_sources(tmp_path, monkeypatch) -> None:
    from importlib.resources import files

    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from tests.conftest import write_cfg

    example = files("agentworks.artifacts").joinpath("examples/scoped-artifacts.yaml").read_text()
    path = write_cfg(tmp_path, example, settings='[plugins]\nsystem = ["codex"]\n')

    def forbid(*args, **kwargs):
        raise AssertionError("declaration loading must not capture sources")

    monkeypatch.setattr("agentworks.artifacts.capture.capture_artifacts", forbid)
    registry = load_request_registry(load_config(path), include_live_resources=False, probe_host_readiness=False)
    bundle = registry.lookup("artifact-bundle", "team-artifacts")
    assert all(getattr(bundle, kind) for kind in ("hints", "rules", "skills", "agents"))


def test_removed_bundle_reference_preserves_unknown_and_historical_metadata(db: Database) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts
    from agentworks.db import VersionedPayload
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    fixture.save("agent")
    db.instance_state.put_desired_overlay(
        "agent", "agent", VersionedPayload(1, {"artifacts": {"bundles": ["missing-bundle"]}})
    )
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    owner = result.owners[-1]
    assert owner.bundles == ("missing-bundle",)
    assert owner.capture_status == "unavailable"
    assert owner.artifacts and not owner.artifacts[0].declared
    assert owner.integrations[0].status == "unavailable"


def test_integration_config_values_are_excluded_from_both_outputs(db: Database, capsys) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
    from agentworks.harness_setup.model import NativeSetupState
    from agentworks.harness_setup.state import write_native_setup
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    record = fixture.save("agent")
    confidential = "operator-supplied-private-value"
    record = record.model_copy(update={"declaration": {"config": {"private_setting": confidential}}})
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="fixture")
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    assert result.owners[-1].integrations[0].status == "stale"
    assert confidential not in json.dumps(inspection_data(result))
    render_artifacts(result)
    assert confidential not in capsys.readouterr().out


@pytest.mark.parametrize("component", ["vm", "agent", "workspace"])
@pytest.mark.parametrize("count", [0, 1])
def test_inactive_inspection_projects_only_applicable_vm_inputs(db: Database, component: str, count: int) -> None:
    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data
    from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
    from agentworks.origin import Origin
    from tests.artifacts.test_routing import graph

    fixture = graph(db, counts={"vm": count, "agent": count, "workspace": count})
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="test")
    )
    result = inspect_artifacts(
        db,
        fixture.registry,
        vm_name="vm" if component == "vm" else None,
        agent_name="agent" if component == "agent" else None,
        workspace_name="workspace" if component == "workspace" else None,
        integration_name="shell",
    )
    expected = (
        (*fixture.captures["vm"].inputs.items(), *fixture.captures["agent"].inputs.items())
        if component == "agent"
        else tuple(fixture.captures[component].inputs.items())
    )
    assert result.owners[-1].integrations[0].status == "inactive"
    assert result.owners[-1].integrations[0].passthrough_inputs == tuple(item.identity for item in expected)
    destination = ("user" if component == "vm" else "session") if expected else None
    assert result.owners[-1].integrations[0].passthrough_destination == destination
    encoded = json.loads(json.dumps(inspection_data(result)))
    integration = encoded["owners"][-1]["integrations"][0]
    assert integration["passthrough_inputs"] == [item.identity for item in expected]
    assert integration["passthrough_destination"] == destination
