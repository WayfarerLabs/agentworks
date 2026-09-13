"""Native carriers retain artifact semantics and refuse ambiguous delivery."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.artifacts.application import ArtifactApplication, OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.artifacts.native.common import native_home, validate_ancestor_names
from agentworks.artifacts.native.probe import probe_native
from agentworks.artifacts.native.shell import shell_artifacts
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok import artifacts as grok
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration


def artifact(
    type: ArtifactType, *, name: str = "review", owner: str = "s1", options: dict | None = None
) -> ArtifactInput:
    members: tuple[ArtifactMember, ...] = ()
    if type is ArtifactType.SKILL:
        members = (
            ArtifactMember("SKILL.md", b"---\nname: review\ndescription: Review changes\n---\nUse this skill.\n"),
            ArtifactMember("scripts/check.sh", b"#!/bin/sh\nexit 0\n", executable=True),
            ArtifactMember("data/logo.bin", bytes(range(256))),
        )
    return ArtifactInput(
        ArtifactContent(
            type,
            name,
            description="Review changes",
            text="before {{session_name}} after\n",
            members=members,
            native_options_json=json.dumps(options or {}),
        ),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", owner, bundle="team", entry=name),
    )


def context(*inputs: ArtifactInput, ancestors: tuple[OwnedArtifactFile, ...] = ()) -> SessionArtifactContext:
    return SessionArtifactContext(
        inputs=inputs,
        home="/home/alice",
        directory="/home/alice/.agentworks-artifacts/session/u/r",
        session_uuid="u",
        run_id="r",
        ancestor_files=ancestors,
    )


def owned(file, *, origins: tuple[str, ...] | None = None) -> OwnedArtifactFile:
    return OwnedArtifactFile(
        path=file.path,
        sha256=hashlib.sha256(file.data).hexdigest(),
        origins=origins or file.origins,
        native_identity=file.native_identity,
    )


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_outer_skills_preserve_complete_package_bytes_and_executable_intent(render):
    item = artifact(ArtifactType.SKILL)
    result = render((item,), "/home/alice/.native")
    assert [(file.path.rsplit("/review/", 1)[1], file.data, file.executable) for file in result.files] == [
        (member.path, member.data, member.executable) for member in item.content.members
    ]
    assert {file.native_identity for file in result.files} == {"skill:review"}
    assert not result.deferred


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_rules_are_unconditional_and_hints_do_not_collide_with_named_rule(render):
    hint = artifact(ArtifactType.HINT)
    rule = artifact(ArtifactType.RULE, name="hints")
    result = render((hint, rule), "/native")
    assert len({file.path for file in result.files}) == 2
    assert all(file.data == hint.content.text.encode() for file in result.files)
    assert all(file.path.count("/") == 3 for file in result.files)


def test_shell_index_has_only_this_run_inputs_and_private_relative_paths():
    item = artifact(ArtifactType.SKILL)
    result = shell_artifacts((item,), "/private/run", session=True)
    index = json.loads(next(file.data for file in result.files if file.path.endswith("/index.json")))
    assert index["artifacts"][0]["files"] == [f"skills/review/{member.path}" for member in item.content.members]
    assert index["artifacts"][0]["origin"]["resource_name"] == "s1"
    assert result.environment == (("AGENTWORKS_ARTIFACTS_DIR", "/private/run"),)
    assert shell_artifacts((), "/private/run", session=True) == ArtifactApplication()


def test_codex_outer_routes_context_and_installs_standard_skills_and_personas():
    items = tuple(artifact(type, name=type.value) for type in ArtifactType)
    result = codex.outer_artifacts(items, skills_root="/home/a/.agents/skills", agents_root="/home/a/.codex/agents")
    assert {entry.input_id for entry in result.deferred} == {item.identity for item in items[:2]}
    assert {entry.destination for entry in result.deferred} == {"session"}
    role = tomllib.loads(next(file.data.decode() for file in result.files if file.path.endswith(".toml")))
    assert role["developer_instructions"] == items[-1].content.text
    assert role["name"] == "agent"


@pytest.mark.parametrize(
    "render,option",
    [
        (claude.session_artifacts, "--append-system-prompt-file"),
        (grok.session_artifacts, "--rules"),
        (codex.session_artifacts, "-c"),
    ],
)
def test_session_guidance_adds_to_configured_text_without_making_it_an_initial_prompt(render, option):
    item = artifact(ArtifactType.RULE)
    result = render(context(item), configured="existing setup", extra_args=[])
    assert option in result.argv
    assert result.application.files[0].data.decode() == "existing setup\n\n" + item.content.text
    if render is codex.session_artifacts:
        value = result.argv[result.argv.index("-c") + 1]
        assert tomllib.loads(value)["developer_instructions"] == result.application.files[0].data.decode()
    elif render is grok.session_artifacts:
        assert result.argv[result.argv.index("--rules") + 1] == result.application.files[0].data.decode()
    else:
        assert result.argv[result.argv.index("--append-system-prompt-file") + 1] == result.application.files[0].path
        assert result.argv[-2:] == ("--system-prompt-snapshot", "off")


def test_claude_session_plugin_uses_native_namespace_and_private_directory():
    item = artifact(ArtifactType.SKILL)
    result = claude.session_artifacts(context(item), configured=None, extra_args=[])
    plugin_path = result.argv[result.argv.index("--plugin-dir") + 1]
    manifest = json.loads(next(file.data for file in result.application.files if file.path.endswith("plugin.json")))
    assert manifest["name"] == "agentworks-artifacts"
    assert all(file.path.startswith(plugin_path + "/") for file in result.application.files)
    assert {file.native_identity for file in result.application.files if file.native_identity} == {
        "skill:agentworks-artifacts:review"
    }


@pytest.mark.parametrize("render", [codex.session_artifacts, grok.session_artifacts])
def test_private_skills_stay_unhandled_when_native_cli_cannot_discover_them(render):
    item = artifact(ArtifactType.SKILL)
    result = render(context(item), configured=None, extra_args=[])
    assert result.application.files == ()
    assert result.application.deferred[0].input_id == item.identity
    assert result.application.deferred[0].destination == "session"
    assert result.argv == ()


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts, grok.session_artifacts])
def test_native_persona_options_do_not_enable_hooks_or_unknown_native_fields(render):
    name = {
        claude.session_artifacts: "claude-code",
        codex.session_artifacts: "codex",
        grok.session_artifacts: "grok-build",
    }[render]
    item = artifact(ArtifactType.AGENT, options={name: {"hooks": {}}})
    with pytest.raises(ConfigError):
        render(context(item), configured=None, extra_args=[])


@pytest.mark.parametrize(
    "render,args",
    [
        (claude.session_artifacts, ["--append-system-prompt=override"]),
        (claude.session_artifacts, ["--disable-slash-commands"]),
        (codex.session_artifacts, ['-cdeveloper_instructions="override"']),
        (codex.session_artifacts, ["--config", 'agents.review.config_file="/other"']),
        (grok.session_artifacts, ["--append-system-prompt", "override"]),
        (grok.session_artifacts, ["--no-subagents"]),
    ],
)
def test_raw_native_overrides_cannot_replace_artifact_carrier(render, args):
    with pytest.raises(ConfigError):
        render(context(artifact(ArtifactType.RULE)), configured=None, extra_args=args)


def test_native_name_collision_does_not_silently_shadow_another_scope():
    item = artifact(ArtifactType.AGENT)
    other = replace(item, origin=ArtifactOrigin("agent", "agent", "alice", bundle="another", entry="review"))
    earlier = claude.outer_artifacts((other,), "/home/alice/.claude")
    with pytest.raises(ConfigError):
        claude.session_artifacts(
            context(item, ancestors=tuple(owned(file) for file in earlier.files)), configured=None, extra_args=[]
        )
    # Multiple members of the same skill are a single native claim.
    skill = claude.outer_artifacts((artifact(ArtifactType.SKILL),), "/home/alice/.claude")
    validate_ancestor_names(context(ancestors=tuple(owned(file) for file in skill.files)), ArtifactApplication())


def test_ancestor_guidance_also_disables_claude_saved_prompt_on_resume():
    files = claude.outer_artifacts((artifact(ArtifactType.RULE),), "/home/alice/.claude").files
    result = claude.session_artifacts(
        context(ancestors=tuple(owned(file) for file in files)), configured=None, extra_args=[]
    )
    assert result.application.files == ()
    assert result.argv == ("--system-prompt-snapshot", "off")


@pytest.mark.parametrize("root", ["relative", "/home/a/../b", "/home//a", "/home/a/"])
def test_native_home_override_must_be_normalized(root):
    with pytest.raises(ConfigError):
        native_home("/home/a", {"NATIVE_HOME": root}, "NATIVE_HOME", ".native")


def test_native_home_override_is_used_instead_of_default():
    assert native_home("/home/a", {"NATIVE_HOME": "/home/a/custom"}, "NATIVE_HOME", ".native") == "/home/a/custom"


def test_native_probe_passes_environment_separately_and_rejects_policy_failures():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="AGW_ARTIFACT_PROBE="
            + json.dumps({"native_home": "/home/a/.claude", "problems": ["native-discovery-exclusions"]}),
        )

    with pytest.raises(StateError):
        probe_native(
            SimpleNamespace(run=run), tool="claude", home="/home/a", environment={"API_TOKEN": "a-secret-value"}
        )
    assert "a-secret-value" not in calls[0][0]
    assert calls[0][1]["env"]["API_TOKEN"] == "a-secret-value"


@pytest.mark.parametrize(
    "implementation", [ShellIntegration, ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration]
)
def test_vm_deferral_preserves_input_identity_without_descendant_knowledge(implementation):
    integration = implementation.for_setup(owner_name="box", owner_kind="vm", facet="vm", config={})
    item = artifact(ArtifactType.RULE)
    result = integration.vm_init(SimpleNamespace(artifacts=(item,)))
    assert result.files == ()
    assert result.deferred[0].input_id == item.identity
    assert result.deferred[0].destination == ("session" if implementation is ShellIntegration else "user")
