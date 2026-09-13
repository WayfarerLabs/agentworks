"""Owning setup capture, buffering and cleanup through real pipeline operations."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.agents.realize import realize_agent
from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec
from agentworks.artifacts.model import ArtifactGroup, ArtifactOwner
from agentworks.artifacts.routing import inspect_owner_artifacts, session_artifacts
from agentworks.artifacts.state import read_captures
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, VMSetupInvocation
from agentworks.errors import ExternalError, StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.lifecycle import prepare_agent_setup, prepare_vm_setup, prepare_workspace_setup
from agentworks.harness_setup.state import read_native_setup
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityBlock
from agentworks.vms.admin import AdminConfig
from agentworks.vms.templates import ResolvedVMTemplate
from agentworks.workspaces.realize import realize_workspace
from agentworks.workspaces.templates import ResolvedTemplate
from tests.conftest import _StubRegistry
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


@pytest.fixture
def owner(db, tmp_path):
    source = tmp_path / "hint.md"
    source.write_text("first capture\n")
    bundle = ArtifactBundle(name="team", hints={"setup": HintArtifactSpec(source=str(source))})
    config = SimpleNamespace(artifact_bundles={"team": bundle}, agent_templates={})
    registry = cast(Registry, _StubRegistry(config))
    vm = db.insert_vm("vm", "lima", "vm")
    target = LocalFixtureTransport(tmp_path / "native")
    return SimpleNamespace(
        db=db,
        vm=vm,
        source=source,
        target=target,
        config=config,
        registry=registry,
        artifacts=ArtifactsConfig(bundles=["team"]),
    )


def user_call(owner: SimpleNamespace) -> UserSetupInvocation:
    return UserSetupInvocation(
        vm=owner.vm,
        runner=owner.target,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(owner.target.home),
    )


def test_common_capture_without_activation_and_vm_admin_separation(owner):
    prepared = prepare_vm_setup(
        owner.db,
        owner.registry,
        name="vm",
        template=ResolvedVMTemplate("default", artifacts=owner.artifacts),
        admin=AdminConfig(artifacts=owner.artifacts),
    )
    assert [item.component for item in prepared] == ["vm", "admin"]
    assert not read_captures(owner.db, "vm", "vm")
    for inputs in prepared:
        invocation = (
            VMSetupInvocation(vm=owner.vm, runner=owner.target, prior=None, checkpoint=lambda claims: None)
            if inputs.component == "vm"
            else user_call(owner)
        )
        run_setup(owner.db, owner.registry, inputs, invocation, operation="vm-init")
    captured = read_captures(owner.db, "vm", "vm")
    assert set(captured) == {"vm", "admin"}
    vm_item = tuple(captured["vm"].inputs.items())[0]
    admin_item = tuple(captured["admin"].inputs.items())[0]
    assert vm_item.content.digest == admin_item.content.digest
    assert vm_item.identity != admin_item.identity
    assert not read_native_setup(owner.db, "vm", "vm").records
    assert owner.target.commands == []


def test_vm_admin_capture_shares_revision_and_refreshes_next_operation(owner, monkeypatch, tmp_path):
    from agentworks.package_sources import PackageCapture

    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    source = repository / "hint.md"
    source.write_text("first capture\n")
    git("add", ".")
    git("commit", "-qm", "first")
    first = git("rev-parse", "HEAD")
    original_popen = subprocess.Popen

    def local_transport(command, *args, **kwargs):
        command = [str(repository) if value == "https://fixture.invalid/repo.git" else value for value in command]
        command = ["protocol.file.allow=always" if value == "protocol.file.allow=never" else value for value in command]
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_transport)
    owner.config.artifact_bundles["team"] = ArtifactBundle(
        name="team", hints={"setup": HintArtifactSpec(source="git::https://fixture.invalid/repo.git//hint.md")}
    )
    original_capture = PackageCapture.capture
    advanced = False

    def capture_then_advance(self, reference):
        nonlocal advanced
        captured = original_capture(self, reference)
        if not advanced:
            source.write_text("second capture\n")
            git("add", ".")
            git("commit", "-qm", "second")
            advanced = True
        return captured

    monkeypatch.setattr(PackageCapture, "capture", capture_then_advance)

    def prepare() -> tuple[SetupInputs, ...]:
        return prepare_vm_setup(
            owner.db,
            owner.registry,
            name="vm",
            template=ResolvedVMTemplate("default", artifacts=owner.artifacts),
            admin=AdminConfig(artifacts=owner.artifacts),
        )

    captured = [item.artifact_snapshot for item in prepare()]
    assert all(snapshot is not None for snapshot in captured)
    inputs = [tuple(snapshot.inputs.items())[0] for snapshot in captured if snapshot is not None]
    assert [item.provenance.commit for item in inputs] == [first, first]
    assert [item.content.text for item in inputs] == ["first capture\n", "first capture\n"]
    assert [item.origin.component for item in inputs] == ["vm", "admin"]
    assert inputs[0].identity != inputs[1].identity
    refreshed = [item.artifact_snapshot for item in prepare()]
    assert [tuple(snapshot.inputs.items())[0].provenance.commit for snapshot in refreshed if snapshot is not None] == [
        git("rev-parse", "HEAD"),
        git("rev-parse", "HEAD"),
    ]


@pytest.mark.parametrize("component", ["agent", "workspace"])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_buffered_capture_commits_atomically_with_realized_owner(owner, monkeypatch, tmp_path, component, fail_commit):
    import agentworks.artifacts.state as captures

    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *a, **k: owner.target)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: owner.target)
    monkeypatch.setattr("agentworks.agents.initializer.create_new_agent_user", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.initializer.create_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    project = owner.target.root / "project"
    project.mkdir()
    monkeypatch.setattr("agentworks.workspaces.backends.vm.create_vm_workspace", lambda *a, **k: str(project))
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.generate_vscode_workspace", lambda *a, **k: tmp_path / "stub.code-workspace"
    )
    original = captures.write_capture
    observations = []

    def commit(db, kind, name, part, snapshot, **kwargs):
        assert db._conn.in_transaction
        row = db.get_agent(name) if component == "agent" else db.get_workspace(name)
        assert row is not None
        observations.append((kind, name, part))
        original(db, kind, name, part, snapshot, **kwargs)
        if fail_commit:
            raise StateError("fixture transaction failure")

    monkeypatch.setattr(captures, "write_capture", commit)
    if component == "agent":
        template = ResolvedAgentTemplate("default", artifacts=owner.artifacts)
        inputs = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="new", template=template)

        def realize() -> object:
            return realize_agent(
                owner.db,
                owner.config,
                owner.registry,
                name="new",
                vm=owner.vm,
                template=template,
                credential_requests=(),
                credential_redactions=(),
                setup_inputs=inputs,
            )
    else:
        workspace_template = ResolvedTemplate("default", artifacts=owner.artifacts)
        inputs = prepare_workspace_setup(owner.db, owner.registry, vm=owner.vm, name="new", template=workspace_template)

        def realize() -> object:
            return realize_workspace(
                owner.db,
                owner.config,
                owner.registry,
                name="new",
                vm=owner.vm,
                template=workspace_template,
                setup_inputs=inputs,
            )

    assert inputs is not None and inputs.artifact_snapshot is not None
    assert not read_captures(owner.db, component, "new")
    if fail_commit:
        with pytest.raises((StateError, ExternalError)):
            realize()
        assert not read_captures(owner.db, component, "new")
        assert not read_native_setup(owner.db, component, "new").records
        assert (owner.db.get_agent("new") if component == "agent" else owner.db.get_workspace("new")) is None
    else:
        realize()
        snapshot = read_captures(owner.db, component, "new")[component]
        assert snapshot == inputs.artifact_snapshot
        assert not read_native_setup(owner.db, component, "new").records
    assert observations == [(component, "new", component)]


def test_reinit_refreshes_source_and_failed_capture_cannot_publish_old_success(owner):
    owner.db.insert_agent("agent", "vm", "worker")
    template = ResolvedAgentTemplate("default", artifacts=owner.artifacts)
    first = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert first is not None
    run_setup(owner.db, owner.registry, first, user_call(owner), operation="agent-init")
    old = read_captures(owner.db, "agent", "agent")["agent"]
    owner.source.write_text("changed source\n")
    second = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert second is not None
    assert read_captures(owner.db, "agent", "agent")["agent"] == old
    run_setup(owner.db, owner.registry, second, user_call(owner), operation="agent-reinit")
    current = read_captures(owner.db, "agent", "agent")["agent"]
    assert tuple(current.inputs.items())[0].content.text == "changed source\n"
    assert tuple(current.inputs.items())[0].identity != tuple(old.inputs.items())[0].identity
    owner.source.unlink()
    with pytest.raises(StateError):
        prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert read_captures(owner.db, "agent", "agent")["agent"] == current
    assert not read_native_setup(owner.db, "agent", "agent").records


@pytest.mark.parametrize("modified", [False, True])
@pytest.mark.parametrize("remove_activation", [False, True])
def test_owned_effect_removal_uses_native_state_and_blocks_pending_cleanup(owner, modified, remove_activation):
    owner.db.insert_agent("agent", "vm", "worker", template="configured")
    workspace = owner.db.insert_workspace("project", str(owner.target.root / "project"), "vm", "project")
    template = ResolvedAgentTemplate(
        "configured", artifacts=owner.artifacts, harness_integrations=[CapabilityBlock.of("shell")]
    )
    owner.config.agent_templates["configured"] = AgentTemplate(
        name="configured", artifacts=owner.artifacts, harness_integrations=[CapabilityBlock.of("shell")]
    )
    inputs = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert inputs is not None
    run_setup(owner.db, owner.registry, inputs, user_call(owner), operation="agent-init")
    previous = read_native_setup(owner.db, "agent", "agent").records[0]
    assert previous.complete and len(previous.artifact_files) == 2
    managed = Path(previous.artifact_files[0].path)
    if modified:
        managed.write_text("operator change")
    updated = replace(
        template,
        artifacts=ArtifactsConfig(),
        harness_integrations=[] if remove_activation else template.harness_integrations,
    )
    owner.config.agent_templates["configured"] = AgentTemplate(
        name="configured", artifacts=updated.artifacts, harness_integrations=updated.harness_integrations
    )
    removal = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=updated)
    assert removal is not None
    run_setup(owner.db, owner.registry, removal, user_call(owner), operation="agent-reinit")
    state = read_native_setup(owner.db, "agent", "agent")
    view = inspect_owner_artifacts(owner.db, owner.registry, replace(removal, artifact_snapshot=None), "shell")
    if modified:
        assert managed.exists()
        assert state.records[0].pending_cleanup and not state.records[0].complete
        assert len(state.records[0].artifact_files) == 1
        assert view.status == ("retirement" if remove_activation else "incomplete")
        with pytest.raises(StateError):
            session_artifacts(owner.db, owner.registry, owner.vm, workspace, "agent", "shell", ())
    else:
        assert not managed.exists()
        assert not state.records if remove_activation else state.records[0].complete
        assert view.status == ("inactive" if remove_activation else "current")
        assert not session_artifacts(
            owner.db,
            owner.registry,
            owner.vm,
            workspace,
            "agent",
            "shell",
            ArtifactGroup(ArtifactOwner("session", "session", "s1")),
        ).inputs


def test_repeated_owner_setup_does_not_rewrite_unchanged_native_files(owner, monkeypatch):
    from agentworks.native_files import NativeFiles

    owner.db.insert_agent("agent", "vm", "worker")
    template = ResolvedAgentTemplate(
        "default", artifacts=owner.artifacts, harness_integrations=[CapabilityBlock.of("shell")]
    )
    first = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert first is not None
    run_setup(owner.db, owner.registry, first, user_call(owner), operation="agent-init")
    previous = read_native_setup(owner.db, "agent", "agent").records[0]

    def unexpected_write(*args, **kwargs):
        raise AssertionError("unchanged native files must not be republished")

    monkeypatch.setattr(NativeFiles, "publish", unexpected_write)
    second = prepare_agent_setup(owner.db, owner.registry, vm=owner.vm, name="agent", template=template)
    assert second is not None
    run_setup(owner.db, owner.registry, second, user_call(owner), operation="agent-reinit")
    current = read_native_setup(owner.db, "agent", "agent").records[0]
    assert current.complete and current.artifact_files == previous.artifact_files
    assert current.artifact_inputs == previous.artifact_inputs


def test_user_setup_publishes_vm_inputs_without_vm_activation(db, tmp_path):
    from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
    from agentworks.origin import Origin
    from tests.artifacts.test_routing import graph

    fixture = graph(db, active=("agent",))
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="test")
    )
    fixture.registry.finalize(probe_host_readiness=False)
    target = LocalFixtureTransport(tmp_path / "native")
    invocation = UserSetupInvocation(
        vm=fixture.vm,
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(target.home),
    )
    run_setup(db, fixture.registry, fixture.owners["agent"], invocation, operation="agent-init")
    record = read_native_setup(db, "agent", "agent").records[0]
    expected = (*fixture.captures["vm"].inputs.items(), *fixture.captures["agent"].inputs.items())
    assert record.complete
    assert record.artifact_inputs == tuple(item.identity for item in expected)
    for item in expected:
        assert any(
            item.origin_identity in file.origins and Path(file.path).read_bytes() == item.content.text.encode()
            for file in record.artifact_files
        )
    assert all(Path(file.path).is_relative_to(target.home) for file in record.artifact_files)
    assert not read_native_setup(db, "vm", "vm").records
    assert [item.origin.component for item in fixture.route().inputs.items()] == ["workspace"]
