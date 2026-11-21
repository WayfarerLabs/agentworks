"""Declared native collisions preserve one evidenced receiving-facet owner."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import render_setup_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.harness_setup.model import SetupRecord
from agentworks.plugins.claude import artifacts as claude
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context, enabled, owned
from tests.conftest import _FakeTarget


@pytest.mark.parametrize("prior_owner", ["withdrawn", "ambiguous", "different-type", "changed-scope"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_setup_requires_unique_current_owner_evidence(db, prior_owner, artifact_type):
    first, second = (artifact(artifact_type, owner=owner) for owner in ("first", "second"))
    other_type = ArtifactType.AGENT if artifact_type is ArtifactType.SKILL else ArtifactType.SKILL
    independent = artifact(other_type, owner="second")
    old = {
        "withdrawn": artifact(artifact_type, owner="withdrawn"),
        "ambiguous": first,
        "different-type": artifact(other_type, owner="first"),
        "changed-scope": replace(first, origin=replace(first.origin, component="agent", resource_kind="agent")),
    }[prior_owner]
    old_file = claude.outer_artifacts(received(old), "/native").files[0]
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
