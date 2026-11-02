"""Known ancestor placement survives replay through an inactive facet."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentworks.artifacts.model import ArtifactType
from tests.artifacts.test_native_delivery import artifact
from tests.artifacts.test_native_probe import probe
from tests.conftest import requires_posix_shell

pytestmark = requires_posix_shell


@pytest.mark.parametrize(
    "tool,class_name,workaround,placement",
    [
        ("claude", "ClaudeCodeIntegration", "session-agent-definitions", "user"),
        ("codex", "CodexIntegration", "session-agent-config", "user"),
        ("grok", "GrokBuildIntegration", "session-agent-definitions", "user"),
        ("grok", "GrokBuildIntegration", "session-agent-definitions", "workspace"),
    ],
)
@pytest.mark.parametrize("prior_state", ["matching", "renamed", "competing"])
def test_inactive_facet_replays_known_persona_without_reusing_old_policy_paths(
    tmp_path, db, monkeypatch, tool, class_name, workaround, placement, prior_state
):
    import shlex
    from dataclasses import replace
    from importlib import import_module
    from types import SimpleNamespace

    from agentworks.artifacts.application import ArtifactApplication, SessionArtifactContext
    from agentworks.artifacts.declarations import AgentArtifactSpec
    from agentworks.artifacts.model import ArtifactGroup, ArtifactInputs, ArtifactOwner
    from agentworks.artifacts.routing import session_artifacts
    from agentworks.artifacts.state import capture_owner, write_capture
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import write_native_setup
    from agentworks.origin import Origin
    from agentworks.plugins.claude import artifacts as claude_adapter
    from agentworks.plugins.codex import artifacts as codex_adapter
    from agentworks.plugins.grok import artifacts as grok_adapter
    from agentworks.schema import CapabilityConfig
    from agentworks.ssh import SSHResult
    from tests.artifacts._fixtures import group, received
    from tests.artifacts.test_native_delivery import owned
    from tests.artifacts.test_routing import graph

    fixture = graph(db)
    source_owner = "agent" if placement == "user" else "workspace"
    bundle_name = f"{source_owner}-bundle"
    source = tmp_path / "review.md"
    source.write_text("---\nname: review\ndescription: Review changes\n---\nReview the implementation.\n")
    bundle = fixture.registry.lookup("artifact-bundle", bundle_name)
    fixture.registry.add(
        "artifact-bundle",
        bundle_name,
        bundle.model_copy(update={"hints": {}, "agents": {"review": AgentArtifactSpec(source=f"file::{source}")}}),
        Origin.built_in(source="replay-fixture"),
    )
    captured = capture_owner(
        fixture.registry, source_owner, source_owner, source_owner, fixture.owners[source_owner].artifacts
    )
    write_capture(db, source_owner, source_owner, source_owner, captured, operation="fixture")
    persona = captured.inputs.agents["review"]
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    native_root = home / {"claude": ".claude", "codex": ".codex", "grok": ".grok"}[tool]
    placement_root = native_root if placement == "user" else workspace / ".grok"

    def outer(inputs: ArtifactInputs) -> ArtifactApplication:
        if tool == "codex":
            return codex_adapter.outer_artifacts(
                inputs,
                skills_root=str(home / ".agents/skills"),
                agents_root=str(native_root / "agents"),
                instructions_path=str(native_root / "AGENTS.md"),
            )
        if tool == "claude":
            return claude_adapter.outer_artifacts(inputs, str(placement_root))
        return grok_adapter.outer_artifacts(inputs, str(placement_root))

    earlier = outer(received(persona))
    # Unrelated old entries must remain outside the new-carrier policy check.
    unrelated = artifact(ArtifactType.AGENT if tool == "grok" else ArtifactType.RULE, name="unrelated", owner="old")
    unrelated_files = outer(received(unrelated)).files
    all_files = (*earlier.files, *unrelated_files)
    for file in all_files:
        path = Path(file.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.data)
    settings = (
        {"claudeMdExcludes": ["**/rules/agentworks-rule-unrelated.md"]}
        if tool == "claude"
        else {"subagents": {"toggle": {"unrelated": False}}}
        if tool == "grok"
        else {}
    )
    probe(tmp_path, tool=tool, identities=(), settings=settings)
    if tool == "codex":
        (native_root / "AGENTS.override.md").write_text("Unrelated native guidance override\n")
    implementation = getattr(import_module(f"agentworks.plugins.{tool}.harness_integration"), class_name)
    record = SetupRecord(
        component=source_owner,
        integration=implementation.name,
        destination_id="d" * 64,
        declaration=fixture.owners[source_owner].declaration(implementation.name, CapabilityConfig()),
        complete=True,
        artifact_inputs=tuple(item.identity for item in captured.inputs.items()),
        artifact_files=tuple(owned(file) for file in all_files),
    )
    write_native_setup(db, source_owner, source_owner, NativeSetupState(records=(record,)), operation="fixture")
    if prior_state != "matching":
        for file in earlier.files:
            path = Path(file.path)
            path.write_text(path.read_text().replace("review", "renamed"))
    if placement == "workspace":
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        (workspace / ".gitignore").write_text(".grok/agents/review.md\n")
    routed = session_artifacts(
        db,
        fixture.registry,
        fixture.vm,
        replace(fixture.workspace, workspace_path=str(workspace)),
        "agent",
        implementation.name,
        ArtifactGroup(ArtifactOwner("session", "session", "session")),
    )
    assert persona.identity in {item.identity for item in routed.inputs.items()}
    assert routed.ancestor_files == record.artifact_files
    environment = {
        "HOME": str(home),
        "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "grok": "GROK_HOME"}[tool]: str(native_root),
    }
    requests = []

    def run(command, *, env=None, **kwargs):
        argv = ["bash", "-c", command]
        if "input_data" in kwargs:
            argv = shlex.split(shlex.split(command)[-1])
            argv[2] = (
                argv[2]
                .replace("/etc/claude-code", str(tmp_path / "etc/claude-code"))
                .replace("/etc/codex", str(tmp_path / "etc/codex"))
            )
            requests.append(json.loads(kwargs["input_data"]))
        observed = subprocess.run(
            argv,
            input=kwargs.get("input_data"),
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        return SSHResult(observed.returncode, observed.stdout, observed.stderr)

    inputs = routed.inputs
    if prior_state == "competing":
        inputs = replace(inputs, local=group(artifact(ArtifactType.AGENT, name="renamed", owner="session")))
    prepared = SessionArtifactContext(
        inputs=inputs,
        home=str(home),
        directory=str(home / "private"),
        session_uuid="u",
        run_id="r",
        ancestor_files=routed.ancestor_files,
        environment=environment,
    )
    integration = implementation(
        implementation.name,
        {"enabled_workarounds": [workaround]},
        session_name="session",
        vm_name="vm",
        workspace_name="workspace",
        workspace_path=str(workspace),
        target=None,
        admin=True,
        state={},
        artifact_context=prepared,
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "fixture-launch")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    result = integration.start(
        RunContext(admin_target=SimpleNamespace(run=run)), intent=HarnessLaunchIntent.RESUME_OR_NEW
    )
    assert result.command == "fixture-launch"
    assert warnings
    if prior_state == "competing":
        assert inputs.local is not None
        assert {item.input_id for item in result.artifacts.deferred} == {
            item.identity for item in inputs.items() if item.identity != persona.identity
        }
        assert any(persona.origin_identity in file.origins for file in result.artifacts.files)
    proposed = integration._artifact_plan.application.files
    selected_request = next(
        request for request in reversed(requests) if set(request["paths"]) == {file.path for file in proposed}
    )
    assert set(selected_request["entries"]) == {
        file.path for file in (*earlier.files, *proposed) if file.native_identity
    }
    assert set(selected_request["paths"]) == {file.path for file in proposed}
    assert set(selected_request["identities"]) == {file.native_identity for file in proposed if file.native_identity}
    assert not any(file.path in selected_request["proposed"] for file in earlier.files)
    assert not any(file.path in selected_request["paths"] for file in unrelated_files)


def test_recognition_hint_cannot_acknowledge_a_mismatched_proposed_entry():
    from agentworks.artifacts.native.probe import _inventory_problems

    path = "/native/review.md"
    result = _inventory_problems(
        [{"kind": "agent", "path": path, "name": "previous"}],
        ("agent:review",),
        {path: "agent:review"},
        recognized_entries={path: "agent:previous"},
    )
    assert [(problem.code, problem.identity, problem.path) for problem in result] == [
        ("native-artifact-identity-mismatch", "agent:review", path)
    ]
