"""Declared native collisions preserve one evidenced receiving-facet owner."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactInputs, ArtifactType
from agentworks.artifacts.native.common import render_setup_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.harness_setup.model import SetupRecord
from agentworks.plugins.claude import artifacts as claude
from tests.artifacts._fixtures import group, received
from tests.artifacts.test_native_delivery import artifact, context, enabled, owned
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize("prior_owner", ["withdrawn", "ambiguous", "different-type", "changed-scope"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_requires_unique_current_owner_evidence(db, prior_owner, artifact_type):
    first = artifact(artifact_type, owner="first")
    first = replace(first, origin=replace(first.origin, component="vm", resource_kind="vm"))
    second = replace(first, origin=replace(first.origin, resource_name="second"))
    other_type = ArtifactType.AGENT if artifact_type is ArtifactType.SKILL else ArtifactType.SKILL
    independent = artifact(other_type, owner="local")
    old = {
        "withdrawn": replace(first, origin=replace(first.origin, resource_name="withdrawn")),
        "ambiguous": first,
        "different-type": replace(artifact(other_type, owner="first"), origin=first.origin),
        "changed-scope": replace(first, origin=replace(first.origin, component="agent", resource_kind="agent")),
    }[prior_owner]
    old_group = group(old)
    old_file = claude.outer_artifacts(ArtifactInputs(deferred={old_group.owner: old_group}), "/native").files[0]
    origins = (first.origin_identity, second.origin_identity) if prior_owner == "ambiguous" else None
    prior = SetupRecord(
        component="agent",
        integration="claude-code",
        destination_id="a" * 64,
        declaration={},
        artifact_files=(owned(old_file, origins=origins),),
    )
    for contenders in ((first, second), (second, first)):
        incomplete = Mock()
        access = RecordingVMAccess([], Admin())
        invocation = UserSetupInvocation(
            vm=db.insert_vm(f"vm-{contenders[0].origin.resource_name}", "site", "host"),
            access=access,
            files=access.files(),
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
def test_cross_owner_guidance_coexists_under_distinct_delivery_names(db, tool, artifact_type):
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    first, second = (artifact(artifact_type, owner=owner) for owner in ("first-owner", "second-owner"))
    first = replace(first, origin=replace(first.origin, component="vm", resource_kind="vm"))
    incomplete = Mock()
    access = RecordingVMAccess([], Admin())
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=access,
        files=access.files(),
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
    base = (
        "agentworks-hints"
        if artifact_type is ArtifactType.HINT
        else "agentworks-rule-review"
        if tool == "codex"
        else "review"
    )
    from pathlib import PurePosixPath

    if tool == "codex":
        delivered = {PurePosixPath(file.package_root).name for file in setup.files}
    else:
        delivered = {PurePosixPath(file.path).stem for file in setup.files}
    assert delivered == {base, f"vm-{base}"}
    incomplete.assert_not_called()
    session = adapter.session_artifacts(
        context=context(first, second), extra_args=[], enabled_workarounds=enabled(adapter.session_artifacts)
    )
    assert not session.application.deferred
    assert {origin for file in session.application.files for origin in file.origins} == {
        first.origin_identity,
        second.origin_identity,
    }
