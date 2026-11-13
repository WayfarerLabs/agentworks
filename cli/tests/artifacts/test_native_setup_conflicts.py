"""Native setup defers colliding origins before rendering independent artifacts."""

from __future__ import annotations

import sys
from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.common import defer_setup_conflicts
from agentworks.artifacts.native.layout import NativeUserLayout
from agentworks.artifacts.native.probe import NativeArtifactConflictError, _inventory_problems
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context, enabled
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
@pytest.mark.parametrize("facet", ["user", "workspace"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_defers_all_colliding_origins_and_applies_independent_rule(
    db, monkeypatch, implementation, facet, artifact_type
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
    outer = replace(artifact(artifact_type), origin=ArtifactOrigin("vm", "vm", "vm", entry="review"))
    component = "agent" if facet == "user" else "workspace"
    local = replace(artifact(artifact_type), origin=ArtifactOrigin(component, component, "local", entry="review"))
    independent = replace(artifact(ArtifactType.RULE), origin=local.origin)
    incomplete = Mock()
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "access": (recorded := RecordingVMAccess([], Admin())),
        "files": recorded.files(),
        "prior": None,
        "checkpoint": lambda claims: None,
        "report_incomplete": incomplete,
        "artifacts": received(outer, local, independent),
    }
    invocation: SetupInvocation
    if facet == "user":
        invocation = UserSetupInvocation(**common, username="alice", home="/home/alice")
    else:
        invocation = WorkspaceSetupInvocation(**common, workspace_name="ws", root="/workspace", linux_group="ws")
    integration = implementation.for_setup(owner_kind=component, owner_name="local", facet=facet, config={})
    result = getattr(integration, f"{facet}_init")(invocation)

    assert {item.input_id for item in result.deferred} == {outer.identity, local.identity}
    assert all(item.destination == "session" for item in result.deferred)
    assert result.files and {origin for file in result.files for origin in file.origins} == {
        independent.origin_identity
    }
    incomplete.assert_called_once()
    if implementation is not GrokBuildIntegration:
        native_setup.assert_called_once()


@pytest.mark.parametrize(
    "adapter,artifact_type",
    [
        ("claude", ArtifactType.SKILL),
        ("claude", ArtifactType.AGENT),
        ("codex", ArtifactType.AGENT),
        ("grok", ArtifactType.AGENT),
    ],
)
def test_session_cross_origin_names_are_rejected_or_left_unhandled(adapter, artifact_type):
    module = import_module(f"agentworks.plugins.{adapter}.artifacts")
    outer = artifact(artifact_type, owner="outer")
    local = artifact(artifact_type, owner="local")
    if adapter == "grok":
        result = module.session_artifacts(
            context=context(outer, local),
            extra_args=["--single", "fixture task"],
            enabled_workarounds=enabled(module.session_artifacts),
        )
        assert {item.input_id for item in result.application.deferred} == {outer.identity, local.identity}
        assert all(item.destination == "session" for item in result.application.deferred)
        assert result.application.files == ()
        assert result.argv == ()
        return
    with pytest.raises(NativeArtifactConflictError):
        module.session_artifacts(
            context=context(outer, local),
            extra_args=["--single", "fixture task"] if adapter == "grok" else [],
            enabled_workarounds=enabled(module.session_artifacts),
        )


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
        problems = _inventory_problems(inventory, tuple(entries.values()), entries)
        if problems:
            raise NativeArtifactConflictError(
                "fixture retained persona name", tuple(problem.identity for problem in problems), hint="fixture repair"
            )

    result = defer_setup_conflicts(plan, invocation, check)
    assert {item.input_id for item in result.deferred} == {renamed.identity, competing.identity}
    assert {origin for file in result.files for origin in file.origins} == {independent.origin_identity}
    assert incomplete.called


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest native probe")
def test_real_workspace_setup_defers_generated_helper_and_authored_skill_identity_collision(db, tmp_path):
    from agentworks.artifacts.capture import content_from_members
    from agentworks.artifacts.model import ArtifactMember
    from agentworks.artifacts.publication import validate_application
    from agentworks.plugins.codex.artifacts import outer_artifacts
    from tests.native_setup_fixtures import LocalFixtureTransport

    target = LocalFixtureTransport(tmp_path / "guest")
    login = target.root / "login-shell"
    login.write_text(
        login.read_text().replace('[ "$1" = "-lc" ] || exit 9', 'case "$1" in -lc|-lic) ;; *) exit 9 ;; esac')
    )
    workspace = target.home / "project"
    workspace.mkdir()
    owner = ArtifactOrigin("workspace", "workspace", "ws", entry="review")
    rule = replace(artifact(ArtifactType.RULE), origin=owner)
    generated = outer_artifacts(received(rule), skills_root="/skills", agents_root="/agents").files[0]
    assert generated.native_identity is not None
    name = generated.native_identity.removeprefix("skill:")
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
    result = integration.workspace_init(invocation)
    assert {item.input_id for item in result.deferred} == {rule.identity, skill.identity}
    assert all(item.destination == "session" for item in result.deferred)
    assert {origin for file in result.files for origin in file.origins} == {hint.origin_identity}
    assert incomplete and access.requests()
    validate_application(result, inputs, "workspace", integration="codex")
