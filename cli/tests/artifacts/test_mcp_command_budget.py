"""Optional MCP admission measures the selected launch and preserves independent delivery."""

from __future__ import annotations

import shlex

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.errors import ConfigError
from tests.artifacts.test_mcp_session_delivery import mcp
from tests.artifacts.test_native_delivery import artifact, context
from tests.artifacts.test_rendered_launch_args import integration
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize(
    "config,payload",
    [
        ({"developer_instructions": "d" * 15000}, "x" * 25000),
        ({"initial_prompt": "p" * 40000}, "x"),
        ({}, "'" * 6000),
    ],
)
def test_large_complete_launch_defers_mcp_and_preserves_legacy_files(config, payload, monkeypatch):
    server = mcp(metadata={"type": "stdio", "command": "server", "args": [payload], "env": {}})
    skill = artifact(ArtifactType.SKILL)
    current = context(server, skill)
    monkeypatch.setattr("agentworks.plugins.codex.harness_integration.session_mcp_conflicts", lambda *a, **k: {})
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *a, **k: "/home/alice/.codex")
    access = RecordingVMAccess([], Admin())
    baseline = integration(
        "codex", "CodexIntegration", {**config, "enabled_workarounds": ["session-home-skills"]}, artifacts=current
    ).start(RunContext(admin_access=access), intent=HarnessLaunchIntent.CREATE)
    selected = integration(
        "codex",
        "CodexIntegration",
        {**config, "enabled_workarounds": ["session-home-skills", "session-mcp-overrides"]},
        artifacts=current,
    )
    actual = selected.start(RunContext(admin_access=access), intent=HarnessLaunchIntent.CREATE)
    assert actual.command == baseline.command
    assert actual.artifacts.files == baseline.artifacts.files and actual.artifacts.files
    assert {entry.input_id for entry in actual.artifacts.deferred} == {server.identity}
    assert selected._state == {"fresh_pending": None}


@pytest.mark.parametrize("mode", ["fresh", "resume", "picker"])
def test_complete_budget_rerenders_selected_mode_without_changing_binding(mode):
    from agentworks.plugins.codex.artifacts import session_artifacts

    server = mcp(metadata={"type": "stdio", "command": "server", "args": ["x" * 25000], "env": {}})
    current = context(server)
    selected = integration("codex", "CodexIntegration", {"developer_instructions": "d" * 15000}, artifacts=current)
    selected._artifact_plan = session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
    selected._state["session_id"] = "01954b59-c484-7de4-9e70-0b660f7fa562"
    before = selected._state.copy()
    if mode == "resume":
        command = selected._resume_command(before["session_id"], msg="fixture", legacy_marker=None)
    elif mode == "picker":
        command = selected._picker_command(msg="fixture", legacy_marker=None)
    else:
        command = selected._fresh_command(msg="fixture", legacy_marker=None)
    assert selected._state == before
    assert not selected._artifact_plan.argv
    assert {entry.input_id for entry in selected._artifact_plan.application.deferred} == {server.identity}
    assert "mcp_servers." not in command
    tokens = shlex.split(shlex.split(command)[2])
    native = tokens[tokens.index("exec") + 1 :]
    assert native[0] == "codex"
    assert ("resume" in native) == (mode != "fresh")
    assert (before["session_id"] in native) == (mode == "resume")


@pytest.mark.parametrize("mcp_enabled", [False, True])
def test_existing_legacy_command_limit_remains_strict(mcp_enabled, monkeypatch):
    server, agent = mcp(), artifact(ArtifactType.AGENT)
    monkeypatch.setattr("agentworks.plugins.codex.harness_integration.session_mcp_conflicts", lambda *a, **k: {})
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *a, **k: "/home/alice/.codex")
    methods = ["session-agent-config", *(["session-mcp-overrides"] if mcp_enabled else [])]
    selected = integration(
        "codex",
        "CodexIntegration",
        {"developer_instructions": "d" * 40000, "enabled_workarounds": methods},
        artifacts=context(server, agent),
    )
    with pytest.raises(ConfigError):
        selected.start(RunContext(admin_access=RecordingVMAccess([], Admin())), intent=HarnessLaunchIntent.CREATE)
