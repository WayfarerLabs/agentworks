"""Declared native collisions preserve one evidenced receiving-facet owner."""

from __future__ import annotations

import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts import routing
from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.artifacts.native import probe as native_probe
from agentworks.artifacts.native.common import render_setup_artifacts
from agentworks.artifacts.native.layout import NativeUserLayout
from agentworks.artifacts.publication import publish_artifacts, validate_application
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
from agentworks.harness_setup import dispatch
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.harness_setup.state import read_native_setup
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context, enabled, owned
from tests.conftest import _FakeTarget
from tests.native_setup_fixtures import LocalFixtureTransport


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest file publication helpers locally")
@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
@pytest.mark.parametrize("facet", ["user", "workspace"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_reapplies_current_incumbent_content_and_defers_newcomer_in_either_order(
    tmp_path, db, monkeypatch, implementation, facet, artifact_type
):
    target = LocalFixtureTransport(tmp_path)
    native = str(target.home / ".native")
    workspace = target.home / "project"
    workspace.mkdir()
    module = import_module(implementation.__module__)
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: native)
    monkeypatch.setattr(
        module, "user_layout", lambda *args, **kwargs: NativeUserLayout(native, native + "/config.toml")
    )
    setup_method = "setup_user" if facet == "user" else "setup_workspace"
    if hasattr(module, setup_method):
        monkeypatch.setattr(module, setup_method, Mock(return_value=ArtifactApplication()))
    component = "agent" if facet == "user" else "workspace"
    incumbent = replace(
        artifact(artifact_type),
        origin=ArtifactOrigin("vm", "vm", "upstream", bundle="original", entry="review"),
    )
    incomplete: list[str] = []
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "runner": target,
        "prior": None,
        "checkpoint": lambda claims: None,
        "report_incomplete": incomplete.append,
        "artifacts": received(incumbent),
    }
    invocation = (
        UserSetupInvocation(**common, username="fixture", home=str(target.home))
        if facet == "user"
        else WorkspaceSetupInvocation(**common, workspace_name="ws", root=str(workspace), linux_group="")
    )
    integration = implementation.for_setup(owner_kind=component, owner_name="local", facet=facet, config={})
    setup = getattr(integration, f"{facet}_init")
    initial = setup(invocation)
    assert initial.files and not initial.deferred and not incomplete
    applied = publish_artifacts(target, initial.files, (), lambda files: None, roots=(str(target.home),)).files
    initial_paths = {file.path for file in applied}
    content = replace(incumbent.content, text="Updated incumbent persona.\n")
    if artifact_type is ArtifactType.SKILL:
        content = replace(
            content,
            members=(
                replace(
                    content.members[0], data=content.members[0].data.replace(b"Use this skill.", b"Updated skill.")
                ),
                *content.members[1:],
            ),
        )
    incumbent = replace(incumbent, content=content, origin=replace(incumbent.origin, bundle="replacement"))
    newcomer = replace(artifact(artifact_type), origin=ArtifactOrigin(component, component, "newcomer", entry="review"))
    independent = replace(artifact(ArtifactType.RULE), origin=newcomer.origin)
    rendered: dict[str, bytes] | None = None
    for contenders in ((incumbent, newcomer), (newcomer, incumbent)):
        saved = SetupRecord(
            component=component,
            integration=implementation.name,
            destination_id="a" * 64,
            declaration={},
            artifact_files=applied,
        )
        invocation = replace(
            invocation,
            prior=SetupRecord.model_validate_json(saved.model_dump_json()),
            artifacts=received(*contenders, independent),
        )
        plan = setup(invocation)
        validate_application(plan, invocation.artifacts, facet, integration=implementation.name)
        assert [(item.input_id, item.destination) for item in plan.deferred] == [(newcomer.identity, "session")]
        assert {origin for file in plan.files for origin in file.origins} == {
            incumbent.origin_identity,
            independent.origin_identity,
        }
        assert all(
            name in plan.deferred[0].reason for name in (incumbent.origin.resource_name, newcomer.origin.resource_name)
        )
        current = {file.path: file.data for file in plan.files}
        assert rendered is None or current == rendered
        rendered = current
        applied = publish_artifacts(target, plan.files, applied, lambda files: None, roots=(str(target.home),)).files
        assert initial_paths <= {file.path for file in applied}
        assert all(Path(file.path).read_bytes() == file.data for file in plan.files)
        assert any(b"Updated" in file.data for file in plan.files if incumbent.origin_identity in file.origins)
    assert len(incomplete) == 2
    # Withdrawal still retires the incumbent through ordinary owned-file cleanup.
    remaining = setup(replace(invocation, artifacts=received(independent)))
    applied = publish_artifacts(target, remaining.files, applied, lambda files: None, roots=(str(target.home),)).files
    assert not any(Path(path).exists() for path in initial_paths)
    assert {origin for file in applied for origin in file.origins} == {independent.origin_identity}


@pytest.mark.parametrize("prior_owner", ["withdrawn", "ambiguous"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_does_not_choose_an_incumbent_from_withdrawn_or_ambiguous_prior_ownership(db, prior_owner, artifact_type):
    first, second = (artifact(artifact_type, owner=owner) for owner in ("first", "second"))
    independent = artifact(ArtifactType.RULE, name="independent", owner="second")
    old = artifact(artifact_type, owner="withdrawn")
    prior_files = tuple(owned(file) for file in claude.outer_artifacts(received(old), "/native").files)
    if prior_owner == "ambiguous":
        origins = (first.origin_identity, second.origin_identity)
        prior_files = tuple(file.model_copy(update={"origins": origins}) for file in prior_files)
    prior = SetupRecord(
        component="agent",
        integration="claude-code",
        destination_id="a" * 64,
        declaration={},
        artifact_files=prior_files,
    )
    for contenders in ((first, second), (second, first)):
        incomplete = Mock()
        invocation = UserSetupInvocation(
            vm=db.insert_vm(f"vm-{contenders[0].origin.resource_name}", "site", "host"),
            runner=_FakeTarget(),
            prior=prior,
            checkpoint=lambda claims: None,
            report_incomplete=incomplete,
            artifacts=received(*contenders, independent),
            username="fixture",
            home="/home/fixture",
        )
        plan = render_setup_artifacts(invocation, lambda inputs: claude.outer_artifacts(inputs, "/native"))
        assert {item.input_id for item in plan.deferred} == {first.identity, second.identity}
        assert {origin for file in plan.files for origin in file.origins} == {independent.origin_identity}
        incomplete.assert_called_once()


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.RULE, ArtifactType.HINT])
def test_cross_owner_guidance_names_warn_and_coexist_in_setup_and_session(db, monkeypatch, tool, artifact_type):
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    first, second = (artifact(artifact_type, owner=owner) for owner in ("first-owner", "second-owner"))
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.native.common.output.warn", warnings.append)
    incomplete = Mock()
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        runner=_FakeTarget(),
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete,
        artifacts=received(first, second),
        username="fixture",
        home="/home/fixture",
    )

    def render(inputs):
        if tool == "codex":
            return adapter.outer_artifacts(inputs, skills_root="/native/skills", agents_root="/native/agents")
        return adapter.outer_artifacts(inputs, "/native")

    setup = render_setup_artifacts(invocation, render)
    assert not setup.deferred
    assert {origin for file in setup.files for origin in file.origins} == {
        first.origin_identity,
        second.origin_identity,
    }
    assert len(warnings) == 1 and all(item.origin.resource_name in warnings[0] for item in (first, second))
    incomplete.assert_not_called()
    warnings.clear()
    session = adapter.session_artifacts(
        context=context(first, second), extra_args=[], enabled_workarounds=enabled(adapter.session_artifacts)
    )
    assert not session.application.deferred
    assert {origin for file in session.application.files for origin in file.origins} == {
        first.origin_identity,
        second.origin_identity,
    }
    assert any(all(item.origin.resource_name in warning for item in (first, second)) for warning in warnings)


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest probe and publication helpers locally")
@pytest.mark.parametrize("recreated", [False, True])
def test_dispatch_uses_incumbent_file_ownership_only_at_its_recorded_destination(tmp_path, db, monkeypatch, recreated):
    target = LocalFixtureTransport(tmp_path / "guest")
    binary = target.root / "bin"
    binary.mkdir()
    (binary / "python3").symlink_to(sys.executable)
    executable = binary / "claude"
    executable.write_text("#!/bin/sh\nexit 91\n")
    executable.chmod(0o700)
    machine_id = binary / "cat"
    machine_id.write_text(
        '#!/bin/sh\nif [ "$1" = /etc/machine-id ]; then echo fixture-machine; else exec /bin/cat "$@"; fi\n'
    )
    machine_id.chmod(0o700)
    program = native_probe._PROBE
    for guest_path in ("/etc/claude-code/.claude", "/etc/claude-code/managed-settings.json"):
        program = program.replace(repr(guest_path), repr(str(target.root / guest_path.removeprefix("/"))))
    monkeypatch.setattr(native_probe, "_PROBE", program)
    monkeypatch.setattr(dispatch, "ensure_harness_integration_enabled", lambda *args: None)
    monkeypatch.setattr(dispatch, "harness_integration_for", lambda name: ClaudeCodeIntegration)
    monkeypatch.setattr(SetupInputs, "declaration", lambda *args: {})
    vm = db.insert_vm("vm", "fixture", "vm")
    db.insert_agent("worker", "vm", "worker")
    inputs = SetupInputs(
        kind="agent",
        name="worker",
        component="agent",
        activations={"claude-code": CapabilityConfig.model_validate({})},
        target=SecretTarget(vm={}),
    )
    invocation = UserSetupInvocation(
        vm=vm,
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(target.home),
    )
    incumbent = replace(artifact(ArtifactType.SKILL), origin=ArtifactOrigin("vm", "vm", "vm", entry="review"))
    newcomer = replace(artifact(ArtifactType.SKILL), origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
    independent = replace(artifact(ArtifactType.RULE), origin=newcomer.origin)
    routed = received(incumbent)
    monkeypatch.setattr(routing, "setup_artifacts", lambda *args: routing.RoutingResult(routed))
    failures: list[SetupFailure] = []
    before = dispatch.run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-init").records[0]
    assert not failures and before.complete and before.artifact_files
    original = {
        Path(file.path).relative_to(target.home): Path(file.path).read_bytes() for file in before.artifact_files
    }
    old_home = target.home
    if recreated:
        old_home = target.root / "old-home"
        target.home.rename(old_home)
        target.home.mkdir()
        (target.home / ".profile").write_bytes((old_home / ".profile").read_bytes())
    routed = received(incumbent, newcomer, independent)

    state = dispatch.run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")

    assert not failures
    assert state == read_native_setup(db, "agent", "worker")
    after = state.records[0]
    assert (before.destination_id != after.destination_id) is recreated
    assert {item.input_id for item in after.deferred} == (
        {incumbent.identity, newcomer.identity} if recreated else {newcomer.identity}
    )
    assert {origin for file in after.artifact_files for origin in file.origins} == (
        {independent.origin_identity} if recreated else {incumbent.origin_identity, independent.origin_identity}
    )
    assert (target.home / ".claude/skills/review/SKILL.md").exists() is not recreated
    assert {path: (old_home / path).read_bytes() for path in original} == original
