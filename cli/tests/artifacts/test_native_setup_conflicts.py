"""Native setup defers colliding origins before rendering independent artifacts."""

from __future__ import annotations

import sys
from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactInputs, ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.common import defer_setup_conflicts
from agentworks.artifacts.native.layout import NativeUserLayout
from agentworks.artifacts.native.probe import NativeArtifactConflictError, _inventory_problems
from agentworks.artifacts.publication import validate_application
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.harness_setup.model import SetupRecord
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import group, received
from tests.artifacts.test_native_delivery import artifact, context, owned
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize("suffix", ["", "'quoted", "'quoted\n"])
def test_setup_conflict_cause_and_hint_survive_redacted_persistence_and_inspection(db, suffix):
    from agentworks.artifacts.inspection import _integration_metadata
    from agentworks.artifacts.routing import ArtifactOwnerView
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.outcomes import Redactor
    from agentworks.harness_setup.state import read_native_setup, write_native_setup
    from agentworks.plugins.claude.artifacts import outer_artifacts
    from agentworks.schema import CapabilityConfig
    from agentworks.secrets.orchestration import SecretTarget

    cause, remedy, marker = (uuid4().hex for _ in range(3))
    secret = marker + suffix
    shown = secret.strip("\r\n")
    rendered = repr(f'/home/alice/fixture-prefix-"{shown}/native.json') if suffix else secret
    bad, independent = artifact(ArtifactType.SKILL), artifact(ArtifactType.RULE)
    inputs = received(bad, independent)
    incomplete: list[str] = []
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=(recorded := RecordingVMAccess([], Admin())),
        files=recorded.files(),
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete.append,
        artifacts=inputs,
        redactor=Redactor((secret,)),
        username="alice",
        home="/home/alice",
    )

    def check(files):
        if any(file.native_identity == "skill:review" for file in files):
            raise NativeArtifactConflictError(f"{cause} {rendered}", ("skill:review",), hint=f"{remedy} {rendered}")

    application = defer_setup_conflicts(outer_artifacts(inputs, "/home/alice/.claude"), invocation, check)
    assert {origin for file in application.files for origin in file.origins} == {independent.origin_identity}
    assert [item.input_id for item in application.deferred] == [bad.identity]
    record = SetupRecord(
        component="agent",
        integration="claude-code",
        destination_id="a" * 64,
        declaration={},
        artifact_inputs=tuple(item.identity for item in inputs.items()),
        deferred=application.deferred,
        incomplete_reasons=tuple(incomplete),
    )
    write_native_setup(db, "agent", "fixture", NativeSetupState(records=(record,)), operation="fixture-init")
    saved = read_native_setup(db, "agent", "fixture").records[0]
    owner = SetupInputs(
        kind="agent",
        name="fixture",
        component="agent",
        activations={"claude-code": CapabilityConfig()},
        target=SecretTarget(vm={}),
    )
    view = ArtifactOwnerView(owner, None, "current", "incomplete", record=saved)
    projected = _integration_metadata(owner, "claude-code", view)
    reason = projected.recorded_deferred[0].reason
    assert cause in reason and remedy in reason
    assert all(marker not in surface for surface in (saved.model_dump_json(), reason, *incomplete))
    assert all(cause in diagnostic and remedy in diagnostic for diagnostic in incomplete)


@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
@pytest.mark.parametrize("facet", ["user", "workspace"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
@pytest.mark.parametrize("established", [False, True], ids=["cold", "established"])
@pytest.mark.parametrize("competing", [False, True], ids=["same-canonical", "same-native"])
def test_setup_keeps_established_owner_or_defers_all_colliders(
    db, monkeypatch, implementation, facet, artifact_type, established, competing
):
    module = import_module(implementation.__module__)
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.native")
    monkeypatch.setattr(
        module,
        "user_layout",
        lambda *args, **kwargs: NativeUserLayout("/home/alice/.native", "/home/alice/.native/config.toml"),
    )
    native_setup = Mock(return_value=ArtifactApplication())
    setup_method = "setup_user" if facet == "user" else "setup_workspace"
    if hasattr(module, setup_method):
        monkeypatch.setattr(module, setup_method, native_setup)
    outer = replace(artifact(artifact_type), origin=ArtifactOrigin("vm", "vm", "vm", bundle="original", entry="review"))
    component = "agent" if facet == "user" else "workspace"
    local_name = "vm-review" if competing else "review"
    local = replace(
        artifact(artifact_type, name=local_name), origin=ArtifactOrigin(component, component, "local", entry=local_name)
    )
    independent = replace(artifact(ArtifactType.RULE), origin=replace(local.origin, entry="review"))
    outer_group = group(outer)
    incomplete = Mock()
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "access": (recorded := RecordingVMAccess([], Admin())),
        "files": recorded.files(),
        "prior": None,
        "checkpoint": lambda claims: None,
        "report_incomplete": incomplete,
        "artifacts": ArtifactInputs(deferred={outer_group.owner: outer_group}),
    }
    invocation: SetupInvocation
    if facet == "user":
        invocation = UserSetupInvocation(**common, username="alice", home="/home/alice")
    else:
        invocation = WorkspaceSetupInvocation(**common, workspace_name="ws", root="/workspace", linux_group="ws")
    integration = implementation.for_setup(owner_kind=component, owner_name="local", facet=facet, config={})
    setup = getattr(integration, f"{facet}_init")
    updated = b"Updated incumbent."
    if established:
        initial = setup(invocation)
        assert initial.files and not initial.deferred
        incomplete.assert_not_called()
        native_setup.reset_mock()
        prior = SetupRecord(
            component=component,
            integration=implementation.name,
            destination_id="a" * 64,
            declaration={},
            artifact_files=tuple(owned(file) for file in initial.files),
        )
        invocation = replace(invocation, prior=prior)
        outer = replace(
            outer,
            origin=replace(outer.origin, bundle="replacement"),
            content=replace(
                outer.content,
                text=updated.decode(),
                members=tuple(
                    replace(member, data=member.data.replace(b"Use this skill.", updated))
                    for member in outer.content.members
                ),
            ),
        )

    contenders = (outer, local)
    inputs = received(*contenders, independent)
    result = setup(replace(invocation, artifacts=inputs))
    validate_application(result, inputs, facet, integration=implementation.name)

    rejected = ((local,) if established else contenders) if competing else ()
    assert {(item.input_id, item.destination) for item in result.deferred} == {
        (item.identity, "session") for item in rejected
    }
    expected_origins = {independent.origin_identity}
    if not competing:
        expected_origins.update(item.origin_identity for item in contenders)
        names = {file.native_identity for file in result.files}
        assert {f"{artifact_type.value}:review", f"{artifact_type.value}:vm-review"} <= names
    elif established:
        expected_origins.add(outer.origin_identity)
    if established:
        assert any(updated in file.data for file in result.files if outer.origin_identity in file.origins)
    assert {origin for file in result.files for origin in file.origins} == expected_origins
    assert incomplete.call_count == int(competing)
    if implementation is not GrokBuildIntegration:
        native_setup.assert_called_once()


@pytest.mark.parametrize("override", [False, True])
def test_grok_cross_origin_rules_survive_competing_unhandled_personas(override):
    from agentworks.plugins.grok.artifacts import session_artifacts

    outer = artifact(ArtifactType.RULE, owner="outer")
    outer = replace(outer, content=replace(outer.content, text="outer guidance"))
    local = artifact(ArtifactType.RULE, owner="local")
    local = replace(local, content=replace(local.content, text="local guidance"))
    personas = tuple(artifact(ArtifactType.AGENT, owner=owner) for owner in ("outer", "local"))
    kwargs = {
        "context": context(outer, local, *personas),
        "extra_args": ["--rules", "override"] if override else [],
        "enabled_workarounds": ["session-home-skills", "session-agent-definitions"],
    }
    result = session_artifacts(**kwargs)
    assert result.argv == () and result.application.files
    assert {origin for file in result.application.files for origin in file.origins} == {
        outer.origin_identity,
        local.origin_identity,
    }
    assert {item.input_id for item in result.application.deferred} == {item.identity for item in personas}


def test_deferring_a_renamed_existing_entry_exposes_its_other_native_collision(db):
    from agentworks.plugins.claude.artifacts import outer_artifacts

    renamed = artifact(ArtifactType.AGENT)
    competing = artifact(ArtifactType.AGENT, name="other")
    independent = artifact(ArtifactType.RULE)
    inputs = received(renamed, competing, independent)
    plan = outer_artifacts(inputs, "/home/alice/.claude")
    incomplete = Mock()
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=(recorded := RecordingVMAccess([], Admin())),
        files=recorded.files(),
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete,
        artifacts=inputs,
        username="alice",
        home="/home/alice",
    )
    inventory = [{"kind": "agent", "path": "/home/alice/.claude/agents/review.md", "name": "other"}]

    def check(files):
        entries = {file.path: file.native_identity for file in files if file.native_identity}
        problems = _inventory_problems(inventory, entries)
        if problems:
            raise NativeArtifactConflictError(
                "fixture retained persona name", tuple(problem.identity for problem in problems), hint="fixture repair"
            )

    result = defer_setup_conflicts(plan, invocation, check)
    assert {item.input_id for item in result.deferred} == {renamed.identity, competing.identity}
    assert {origin for file in result.files for origin in file.origins} == {independent.origin_identity}
    assert incomplete.called


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest native probe")
@pytest.mark.parametrize("established", [False, True])
def test_real_workspace_setup_defers_generated_helper_and_authored_skill_identity_collision(db, tmp_path, established):
    from agentworks.artifacts.capture import content_from_members
    from agentworks.artifacts.model import ArtifactMember
    from tests.native_setup_fixtures import LocalFixtureAccount

    target = LocalFixtureAccount(tmp_path / "guest")
    workspace = target.home / "project"
    workspace.mkdir()
    owner = ArtifactOrigin("workspace", "workspace", "ws", entry="review")
    rule = replace(artifact(ArtifactType.RULE), origin=owner)
    name = "agentworks-rule-review"
    body = f"---\nname: {name}\ndescription: Fixture procedure\n---\nProcedure body.\n".encode()
    skill = replace(
        artifact(ArtifactType.SKILL, name=name),
        content=content_from_members(ArtifactType.SKILL, name, (ArtifactMember("SKILL.md", body, text=True),), name),
        origin=replace(owner, entry=name),
    )
    hint = replace(artifact(ArtifactType.HINT, name="independent"), origin=replace(owner, entry="independent"))
    inputs = received(rule, skill, hint)
    incomplete: list[str] = []
    access = target.access()
    invocation = WorkspaceSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=access,
        files=access.files(),
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete.append,
        artifacts=inputs,
        workspace_name="ws",
        root=str(workspace),
        linux_group="",
    )
    integration = CodexIntegration.for_setup(owner_kind="workspace", owner_name="ws", facet="workspace", config={})
    if established:
        initial = integration.workspace_init(replace(invocation, artifacts=received(skill)))
        invocation = replace(
            invocation,
            prior=SetupRecord(
                component="workspace",
                integration="codex",
                destination_id="a" * 64,
                declaration={},
                artifact_files=tuple(owned(file) for file in initial.files),
            ),
        )
    result = integration.workspace_init(invocation)
    assert {item.input_id for item in result.deferred} == (
        {rule.identity} if established else {rule.identity, skill.identity}
    )
    assert all(item.destination == "session" for item in result.deferred)
    expected = {hint.origin_identity, skill.origin_identity} if established else {hint.origin_identity}
    assert {origin for file in result.files for origin in file.origins} == expected
    assert incomplete and access.requests()
    validate_application(result, inputs, "workspace", integration="codex")


@pytest.mark.parametrize("kind", [ArtifactType.SKILL, ArtifactType.RULE])
def test_overlong_native_skill_delivery_defers_the_origin_without_truncating(db, monkeypatch, kind):
    from agentworks.artifacts.native.common import prepare_session_delivery, render_setup_artifacts
    from agentworks.plugins.claude import artifacts as claude

    too_long = artifact(kind, name="a" * 64)
    too_long = replace(too_long, origin=replace(too_long.origin, component="vm", resource_kind="vm"))
    independent = artifact(ArtifactType.SKILL, name="b" * 64)
    current = context(too_long, independent)
    access = RecordingVMAccess([], Admin())
    incomplete = Mock()
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=access,
        files=access.files(),
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete,
        artifacts=current.inputs,
        username="alice",
        home=current.home,
    )
    plan = render_setup_artifacts(
        invocation, lambda inputs: claude.outer_artifacts(inputs, "/native", guidance_as_skills=True)
    )
    assert [item.input_id for item in plan.deferred] == [too_long.identity]
    assert {file.native_identity for file in plan.files} == {f"skill:{independent.content.name}"}
    incomplete.assert_called_once()

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *args, **kwargs: "/native")
    session, _ = prepare_session_delivery(
        access,
        tool="claude",
        integration="claude-code",
        context=current,
        render=lambda selected: claude.session_artifacts(
            selected, extra_args=[], enabled_workarounds=["session-skill-plugin"]
        ),
        workspace="/workspace",
    )
    assert [item.input_id for item in session.application.deferred] == [too_long.identity]
    assert {origin for file in session.application.files for origin in file.origins} == {independent.origin_identity}
