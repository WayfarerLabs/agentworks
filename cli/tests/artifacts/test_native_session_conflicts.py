"""Native name conflicts become terminal unhandled inputs without stale carriers."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import ArtifactInputs, ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.common import NativeSessionArtifacts
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessLaunchIntent, HarnessStart
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.errors import StateError
from agentworks.ssh import SSHResult
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, enabled, owned
from tests.artifacts.test_native_probe import probe
from tests.conftest import requires_posix_shell

pytestmark = requires_posix_shell

TOOLS = [("claude", "ClaudeCodeIntegration"), ("codex", "CodexIntegration"), ("grok", "GrokBuildIntegration")]


def guest(tmp_path: Path, tool: str) -> tuple[SimpleNamespace, Path, Path, Path, dict[str, str]]:
    probe(tmp_path, tool=tool, identities=())
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    native = home / {"claude": ".claude", "codex": ".codex", "grok": ".grok"}[tool]
    environment = {
        "HOME": str(home),
        "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(native),
        "CODEX_HOME": str(native),
        "GROK_HOME": str(native),
    }

    def run(command, *, env=None, **kwargs):
        argv = ["bash", "-c", command]
        if "input_data" in kwargs:
            argv = shlex.split(shlex.split(command)[-1])
            argv[2] = (
                argv[2]
                .replace("/etc/claude-code", str(tmp_path / "etc/claude-code"))
                .replace("/etc/codex", str(tmp_path / "etc/codex"))
            )
        result = subprocess.run(
            argv,
            input=kwargs.get("input_data"),
            env={**os.environ, **environment, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        return SSHResult(result.returncode, result.stdout, result.stderr)

    return SimpleNamespace(run=run), home, workspace, native, environment


def launch(
    implementation: Any,
    adapter: ModuleType,
    runner: SimpleNamespace | None,
    home: Path,
    workspace: Path,
    environment: dict[str, str],
    inputs: ArtifactInputs,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ancestors: tuple[OwnedArtifactFile, ...] = (),
) -> tuple[HarnessStart, NativeSessionArtifacts]:
    context = SessionArtifactContext(
        inputs=inputs,
        home=str(home),
        directory=str(home / "private/u/r"),
        session_uuid="u",
        run_id="r",
        ancestor_files=ancestors,
        environment=environment,
    )
    integration = implementation(
        implementation.name,
        {"enabled_workarounds": enabled(adapter.session_artifacts)},
        session_name="session",
        vm_name="vm",
        workspace_name="workspace",
        workspace_path=str(workspace),
        target=None,
        admin=True,
        state={},
        artifact_context=context,
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "fixture-launch")
    result = integration.start(RunContext(admin_target=runner), intent=HarnessLaunchIntent.RESUME_OR_NEW)
    validate_session_application(result.artifacts, context, integration=implementation.name)
    return result, integration._artifact_plan


def assert_persona_carriers(tool: str, plan: NativeSessionArtifacts, expected: set[str]) -> None:
    files = {
        file.native_identity.removeprefix("agent:"): file.path
        for file in plan.application.files
        if file.native_identity is not None and file.native_identity.startswith("agent:")
    }
    assert set(files) == expected
    if tool == "codex":
        overrides = dict(token.split("=", 1) for token in plan.argv if token.startswith("agents."))
        assert {key.split(".")[1] for key in overrides} == expected
        for name in expected:
            assert json.loads(overrides[f"agents.{name}.config_file"]) == files[name]
    else:
        definitions = json.loads(plan.argv[plan.argv.index("--agents") + 1])
        assert set(definitions) == expected


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("artifact_type", [ArtifactType.AGENT, ArtifactType.SKILL])
def test_setup_operator_collision_is_reconsidered_at_session_without_blocking_other_inputs(
    tmp_path, db, monkeypatch, tool, class_name, artifact_type
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    implementation = getattr(module, class_name)
    runner, home, workspace, native, environment = guest(tmp_path, tool)
    bad = replace(artifact(artifact_type), origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
    if artifact_type is ArtifactType.SKILL:
        root = home / ".agents" if tool == "codex" else native
        existing = root / "skills/operator/SKILL.md"
        body = "---\nname: review\ndescription: Operator skill\n---\nOperator instructions.\n"
    else:
        existing = native / "agents" / ("operator.toml" if tool == "codex" else "operator.md")
        body = (
            'name="review"\ndescription="Operator persona"\ndeveloper_instructions="Operator instructions."\n'
            if tool == "codex"
            else "---\nname: review\ndescription: Operator persona\n---\nOperator instructions.\n"
        )
    existing.parent.mkdir(parents=True)
    existing.write_text(body)
    incomplete = Mock()
    setup = implementation.for_setup(owner_kind="agent", owner_name="worker", facet="user", config={})
    application = setup.user_init(
        UserSetupInvocation(
            vm=db.insert_vm("vm", "site", "host"),
            runner=runner,
            prior=None,
            checkpoint=lambda claims: None,
            report_incomplete=incomplete,
            artifacts=received(bad),
            username="worker",
            home=str(home),
            environment=environment,
        )
    )
    assert [item.input_id for item in application.deferred] == [bad.identity]
    assert not application.files and incomplete.called
    good = artifact(ArtifactType.AGENT, name="other")
    guidance = artifact(ArtifactType.RULE)
    warnings = Mock()
    monkeypatch.setattr("agentworks.output.warn", warnings)
    result, plan = launch(
        implementation, adapter, runner, home, workspace, environment, received(bad, good, guidance), monkeypatch
    )
    assert result.command == "fixture-launch"
    assert existing.read_text() == body
    assert any(good.origin_identity in file.origins for file in plan.application.files)
    if tool == "claude" and artifact_type is ArtifactType.SKILL:
        assert not plan.application.deferred
        assert any(bad.origin_identity in file.origins for file in plan.application.files)
        assert "--plugin-dir" in plan.argv
    else:
        assert {item.input_id for item in plan.application.deferred} == {bad.identity}
        assert all(item.destination == "session" for item in plan.application.deferred)
        assert not any(bad.origin_identity in file.origins for file in plan.application.files)
        assert_persona_carriers(tool, plan, {good.content.name})
        assert warnings.called


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("conflict", ["input-owners", "ancestor"])
def test_session_name_conflict_rerenders_remaining_personas_and_keeps_origin_identity(
    tmp_path, monkeypatch, tool, class_name, conflict
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    implementation = getattr(module, class_name)
    runner, home, workspace, native, environment = guest(tmp_path, tool)
    bad = artifact(ArtifactType.AGENT)
    outer = replace(bad, origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
    good = artifact(ArtifactType.AGENT, name="other")
    guidance = artifact(ArtifactType.RULE)
    ancestors: tuple[OwnedArtifactFile, ...] = ()
    if conflict == "ancestor":
        files = (
            adapter.outer_artifacts(
                received(outer),
                skills_root=str(home / ".agents/skills"),
                agents_root=str(native / "agents"),
                instructions_path=str(native / "AGENTS.md"),
            ).files
            if tool == "codex"
            else adapter.outer_artifacts(received(outer), str(native)).files
        )
        for file in files:
            path = Path(file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file.data)
        ancestors = tuple(owned(file) for file in files)
        inputs = received(bad, good, guidance)
        rejected = {bad.identity}
    else:
        inputs = received(outer, bad, good, guidance)
        rejected = {outer.identity, bad.identity}
    result, plan = launch(
        implementation, adapter, runner, home, workspace, environment, inputs, monkeypatch, ancestors=ancestors
    )
    assert result.command == "fixture-launch"
    assert {item.input_id for item in result.artifacts.deferred} == rejected
    assert all(item.destination == "session" for item in result.artifacts.deferred)
    assert any(good.origin_identity in file.origins for file in result.artifacts.files)
    assert_persona_carriers(tool, plan, {good.content.name})


@pytest.mark.parametrize("tool,class_name", TOOLS)
def test_new_session_unsafe_inventory_remains_a_hard_failure(tmp_path, monkeypatch, tool, class_name):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    runner, home, workspace, native, environment = guest(tmp_path, tool)
    outside = tmp_path / "outside"
    outside.mkdir()
    (native / "agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError):
        launch(
            getattr(module, class_name),
            adapter,
            runner,
            home,
            workspace,
            environment,
            received(artifact(ArtifactType.AGENT)),
            monkeypatch,
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("identities", [(), ("rule:review",), ("agent:missing",)])
def test_empty_or_unmatched_name_conflict_cannot_claim_terminal_handling(
    tmp_path, monkeypatch, tool, class_name, identities
):
    from agentworks.artifacts.native.probe import NativeArtifactConflictError

    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    runner, home, workspace, native, environment = guest(tmp_path, tool)

    def malformed(*args, **kwargs):
        raise NativeArtifactConflictError("fixture invalid native identity", identities, hint="fixture repair")

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", malformed)
    with pytest.raises(StateError) as error:
        launch(
            getattr(module, class_name),
            adapter,
            runner,
            home,
            workspace,
            environment,
            received(artifact(ArtifactType.AGENT)),
            monkeypatch,
        )
    assert type(error.value) is StateError


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("missing_target", [False, True])
def test_ancestor_only_missing_target_or_changed_native_root_warns_and_launches(
    tmp_path, monkeypatch, tool, class_name, missing_target
):
    adapter = import_module(f"agentworks.plugins.{tool}.artifacts")
    implementation = getattr(import_module(f"agentworks.plugins.{tool}.harness_integration"), class_name)
    runner, home, workspace, native, environment = guest(tmp_path, tool)
    item = artifact(ArtifactType.AGENT)
    from agentworks.plugins.claude.artifacts import outer_artifacts

    ancestors = tuple(owned(file) for file in outer_artifacts(received(item), str(home / "old-native")).files)

    def ancestor_probe(*args, **kwargs):
        if missing_target:
            pytest.fail("Ancestor probe must not run without a launch target")
        return str(native)

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", ancestor_probe)
    warnings = Mock()
    monkeypatch.setattr("agentworks.output.warn", warnings)
    result, plan = launch(
        implementation,
        adapter,
        None if missing_target else runner,
        home,
        workspace,
        environment,
        received(),
        monkeypatch,
        ancestors=ancestors,
    )
    assert result.command == "fixture-launch" and warnings.called
    assert not result.artifacts.files and not result.artifacts.deferred and not plan.argv
