"""Native plans apply at the defining scope and preserve scoped inputs."""

from __future__ import annotations

import sys
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
from tests.native_setup_fixtures import LocalFixtureTransport


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
    result = integration.vm_init(SimpleNamespace(artifacts=received(*items), runner=object(), environment={}))
    applied = {origin for file in result.files for origin in file.origins}
    expected = {
        item.origin_identity
        for item in items
        if integration_type is not CodexIntegration or item.content.type is ArtifactType.SKILL
    }
    assert applied == expected
    if integration_type is CodexIntegration:
        assert {entry.input_id for entry in result.deferred} == {
            item.identity for item in items if item.content.type is not ArtifactType.SKILL
        }
        assert {entry.destination for entry in result.deferred} == {"user"}
        assert all(file.path.startswith("/etc/codex/skills/") for file in result.files)
    else:
        assert result.deferred == ()
    if integration_type is ClaudeCodeIntegration:
        memory = next(file for file in result.files if file.path == "/etc/claude-code/CLAUDE.md")
        assert memory.generated_section
        assert all(file.path.startswith("/etc/claude-code/.claude/") for file in result.files if file is not memory)
    if integration_type is ShellIntegration:
        assert all(file.path.startswith("/opt/agentworks/artifacts/") for file in result.files)
    else:
        assert calls[0]["vm_only"] is True
        assert "home" not in calls[0]
        assert "workspace" not in calls[0]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")
@pytest.mark.parametrize("override", [None, b"", b" \n", b"Existing override instructions\n"])
def test_codex_selects_native_instruction_file_without_changing_it(tmp_path, override):
    target = LocalFixtureTransport(tmp_path / "target")
    root = target.home / ".codex"
    root.mkdir()
    ordinary = root / "AGENTS.md"
    ordinary.write_bytes(b"Existing ordinary instructions\n")
    selected = root / "AGENTS.override.md"
    if override is not None:
        selected.write_bytes(override)
    path = codex.guidance_path(target, str(root))
    assert path == str(selected if override and override.strip() else ordinary)
    assert ordinary.read_bytes() == b"Existing ordinary instructions\n"
    if override is not None:
        assert selected.read_bytes() == override


@pytest.mark.parametrize("root", ["/home/alice/.codex", "/workspace"])
def test_codex_aggregates_all_guidance_without_losing_same_named_scoped_inputs(root):
    inputs = received(
        *(
            artifact(kind, owner=owner)
            for owner in ("outer", "inner")
            for kind in (ArtifactType.HINT, ArtifactType.RULE)
        )
    )
    plan = codex.outer_artifacts(
        inputs, skills_root=f"{root}/skills", agents_root=f"{root}/agents", instructions_path=f"{root}/AGENTS.md"
    )
    assert len(plan.files) == 1
    assert plan.files[0].generated_section
    assert plan.files[0].origins == tuple(item.origin_identity for item in inputs.items())
    assert plan.files[0].data.count(b"before {{session_name}} after") == 4
    assert plan.deferred == ()
