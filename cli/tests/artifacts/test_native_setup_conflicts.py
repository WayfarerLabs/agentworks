"""Native setup defers colliding origins before rendering independent artifacts."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.common import defer_setup_conflicts
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
from tests.conftest import _FakeTarget


@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
@pytest.mark.parametrize("facet", ["user", "workspace"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_defers_all_colliding_origins_and_applies_independent_rule(
    db, monkeypatch, implementation, facet, artifact_type
):
    module = import_module(implementation.__module__)
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.native")
    native_setup = Mock()
    setup_method = "setup_user" if facet == "user" else "setup_workspace"
    if hasattr(module, setup_method):
        monkeypatch.setattr(module, setup_method, native_setup)
    if implementation is CodexIntegration:
        monkeypatch.setattr(module, "guidance_path", lambda runner, root: root + "/AGENTS.md")
    outer = replace(artifact(artifact_type), origin=ArtifactOrigin("vm", "vm", "vm", entry="review"))
    component = "agent" if facet == "user" else "workspace"
    local = replace(artifact(artifact_type), origin=ArtifactOrigin(component, component, "local", entry="review"))
    independent = replace(artifact(ArtifactType.RULE), origin=local.origin)
    incomplete = Mock()
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "runner": _FakeTarget(),
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
def test_new_session_delivery_keeps_cross_origin_names_strict(adapter, artifact_type):
    module = import_module(f"agentworks.plugins.{adapter}.artifacts")
    outer = artifact(artifact_type, owner="outer")
    local = artifact(artifact_type, owner="local")
    with pytest.raises(NativeArtifactConflictError):
        module.session_artifacts(
            context=context(outer, local),
            configured=None,
            extra_args=["--single", "fixture task"] if adapter == "grok" else [],
            enabled_workarounds=enabled(module.session_artifacts),
        )


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
        runner=_FakeTarget(),
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
