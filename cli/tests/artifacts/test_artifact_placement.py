"""Native plans apply at the defining scope and preserve scoped inputs."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.codex.harness_integration import CodexIntegration
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact


@pytest.mark.parametrize("integration_type", [ShellIntegration, ClaudeCodeIntegration, CodexIntegration])
def test_vm_plans_apply_supported_types_without_descendant_context(monkeypatch, integration_type):
    integration = integration_type.for_setup(owner_name="box", owner_kind="vm", facet="vm", config={})
    items = tuple(
        replace(artifact(kind), origin=ArtifactOrigin("vm", "vm", "box", entry="review")) for kind in ArtifactType
    )
    if integration_type is not ShellIntegration:
        module = __import__(integration_type.__module__, fromlist=["probe_native"])
        calls = []
        monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: calls.append(kwargs))
    result = integration.vm_init(
        SimpleNamespace(artifacts=received(*items), runner=object(), environment={}, secrets={})
    )
    applied = {origin for file in result.files for origin in file.origins}
    expected = {
        item.origin_identity
        for item in items
        if integration_type is not CodexIntegration or item.content.type is not ArtifactType.AGENT
    }
    assert applied == expected
    if integration_type is CodexIntegration:
        assert {entry.input_id for entry in result.deferred} == {
            item.identity for item in items if item.content.type is ArtifactType.AGENT
        }
        assert {entry.destination for entry in result.deferred} == {"user"}
        assert all(file.path.startswith("/etc/codex/skills/") for file in result.files)
    else:
        assert result.deferred == ()
    if integration_type is ClaudeCodeIntegration:
        assert all(file.path.startswith("/etc/claude-code/.claude/") for file in result.files)
        assert not any(file.generated_section for file in result.files)
    if integration_type is ShellIntegration:
        assert all(file.path.startswith("/opt/agentworks/artifacts/") for file in result.files)
    else:
        assert calls[0]["vm_only"] is True
        assert "home" not in calls[0]
        assert "workspace" not in calls[0]


@pytest.mark.parametrize("root", ["/home/alice/.codex", "/workspace"])
def test_codex_preserves_individual_guidance_from_same_named_scoped_inputs(root):
    inputs = received(
        *(
            artifact(kind, owner=owner)
            for owner in ("outer", "inner")
            for kind in (ArtifactType.HINT, ArtifactType.RULE)
        )
    )
    plan = codex.outer_artifacts(inputs, skills_root=f"{root}/skills", agents_root=f"{root}/agents")
    assert len([file for file in plan.files if file.path.endswith("/SKILL.md")]) == 4
    assert len({file.path for file in plan.files}) == len(plan.files)
    assert {origin for file in plan.files for origin in file.origins} == {
        item.origin_identity for item in inputs.items()
    }
    assert all(len(file.origins) == 1 and not file.generated_section for file in plan.files)
    assert not plan.deferred
