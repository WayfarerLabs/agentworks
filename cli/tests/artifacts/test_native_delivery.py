"""Native carriers retain artifact semantics and refuse ambiguous delivery."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.artifacts.native.common import NativeSessionArtifacts, native_home, validate_ancestor_names
from agentworks.artifacts.native.probe import NativeArtifactConflictError, probe_native
from agentworks.artifacts.native.shell import filesystem_artifacts
from agentworks.artifacts.publication import validate_application
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessStart
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok import artifacts as grok
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import received


def enabled(render: Callable[..., NativeSessionArtifacts]) -> list[str]:
    return {
        claude.session_artifacts: ["session-skill-plugin", "session-agent-definitions"],
        codex.session_artifacts: ["session-home-skills", "session-agent-config"],
        grok.session_artifacts: ["session-home-skills"],
    }[render]


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
            text="before ${agw:session_name} after\n",
            members=members,
            native_options_json=json.dumps(options or {}),
        ),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", owner, bundle="team", entry=name),
    )


def context(*inputs: ArtifactInput, ancestors: tuple[OwnedArtifactFile, ...] = ()) -> SessionArtifactContext:
    return SessionArtifactContext(
        inputs=received(*inputs),
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
        package_root=file.package_root,
    )


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_outer_skills_preserve_complete_package_bytes_and_executable_intent(render):
    item = artifact(ArtifactType.SKILL)
    result = render(received(item), "/home/alice/.native")
    assert [(file.path.rsplit("/review/", 1)[1], file.data, file.executable) for file in result.files] == [
        (member.path, member.data, member.executable) for member in item.content.members
    ]
    assert {file.native_identity for file in result.files} == {"skill:review"}
    assert {file.package_root for file in result.files} == {"/home/alice/.native/skills/review"}
    assert not result.deferred


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_rules_are_unconditional_and_hints_do_not_collide_with_named_rule(render):
    hint = artifact(ArtifactType.HINT)
    rule = artifact(ArtifactType.RULE, name="hints")
    result = render(received(hint, rule), "/native")
    assert len({file.path for file in result.files}) == 2
    assert all(file.data.endswith(hint.content.text.encode()) for file in result.files)
    assert all(file.path.count("/") == 3 for file in result.files)


def test_shell_index_has_only_this_run_inputs_and_private_relative_paths():
    item = artifact(ArtifactType.SKILL)
    result = filesystem_artifacts(received(item), "/private/run", session=True)
    index = json.loads(next(file.data for file in result.files if file.path.endswith("/index.json")))
    assert index["groups"][0]["skills"]["review"]["files"] == [
        f"scopes/session/s1/skills/review/{member.path}" for member in item.content.members
    ]
    assert index["groups"][0]["skills"]["review"]["origin"]["resource_name"] == "s1"
    assert result.artifacts_dir == "/private/run"
    assert filesystem_artifacts(received(item), "/outer").artifacts_dir is None
    assert filesystem_artifacts(received(), "/private/run", session=True) == ArtifactApplication()


@pytest.mark.parametrize(
    "directory", [42, (), "", "relative/run", "/private/../run", "/private//run", "/private/\x00run"]
)
def test_plugin_artifact_directory_requires_an_absolute_normalized_path(directory):
    with pytest.raises(StateError):
        validate_application(ArtifactApplication(artifacts_dir=directory), received(), "session", integration="fixture")


@pytest.mark.parametrize("facet", ["vm", "user", "workspace"])
def test_outer_facet_cannot_supply_a_session_artifact_directory(facet):
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(artifacts_dir="/private/run"), received(), facet, integration="fixture"
        )


def test_session_artifact_directory_is_bound_to_the_prepared_run():
    prepared = context(artifact(ArtifactType.HINT))
    application = filesystem_artifacts(prepared.inputs, prepared.directory, session=True)
    assert validate_session_application(application, prepared, integration="shell") is application
    for directory in ("/outside", prepared.directory + "/nested", prepared.directory + "-other"):
        with pytest.raises(StateError):
            validate_session_application(replace(application, artifacts_dir=directory), prepared, integration="shell")


def test_codex_outer_installs_guidance_skills_and_standard_skills_and_personas():
    items = tuple(artifact(type, name=type.value) for type in ArtifactType)
    result = codex.outer_artifacts(
        received(*items), skills_root="/home/a/.agents/skills", agents_root="/home/a/.codex/agents"
    )
    assert not result.deferred
    assert {origin for file in result.files for origin in file.origins} == {item.origin_identity for item in items}
    assert all(not file.generated_section for file in result.files)
    assert len([file for file in result.files if file.path.endswith("/SKILL.md")]) == 3
    role = tomllib.loads(next(file.data.decode() for file in result.files if file.path.endswith(".toml")))
    assert role["developer_instructions"] == items[-1].content.text
    assert role["name"] == "agent"


@pytest.mark.parametrize("render", [claude.session_artifacts, grok.session_artifacts, codex.session_artifacts])
def test_session_guidance_uses_skill_files_instead_of_prompt_arguments(render):
    item = artifact(ArtifactType.RULE)
    result = render(enabled_workarounds=enabled(render), context=context(item), extra_args=[])
    assert any(item.content.text.encode() in file.data for file in result.application.files)
    assert all(item.content.text not in value for value in result.argv)
    assert any(file.native_identity and file.native_identity.startswith("skill:") for file in result.application.files)


def test_claude_session_plugin_uses_native_namespace_and_private_directory():
    item = artifact(ArtifactType.SKILL)
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts), context=context(item), extra_args=[]
    )
    plugin_path = result.argv[result.argv.index("--plugin-dir") + 1]
    manifest = json.loads(next(file.data for file in result.application.files if file.path.endswith("plugin.json")))
    assert manifest["name"] and len(manifest["name"]) <= 64
    assert all(file.path.startswith(plugin_path + "/") for file in result.application.files)
    assert {file.native_identity for file in result.application.files if file.native_identity} == {
        f"skill:{manifest['name']}:review"
    }


@pytest.mark.parametrize("render", [codex.session_artifacts, grok.session_artifacts])
def test_home_skill_workaround_preserves_supporting_files(render):
    item = artifact(ArtifactType.SKILL)
    result = render(context(item), extra_args=[], enabled_workarounds=enabled(render))
    entry = next(file for file in result.application.files if file.native_identity)
    assert not result.application.deferred and result.argv == ()
    assert not entry.path.startswith(context(item).directory + "/")
    for member in item.content.members:
        file = next(file for file in result.application.files if file.path.endswith("/" + member.path))
        if member.path != "SKILL.md":
            assert (file.data, file.executable) == (member.data, member.executable)
    validate_session_application(result.application, context(item), integration="fixture")


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts])
def test_native_persona_options_do_not_enable_hooks_or_unknown_native_fields(render):
    name = {
        claude.session_artifacts: "claude-code",
        codex.session_artifacts: "codex",
    }[render]
    item = artifact(ArtifactType.AGENT, options={name: {"hooks": {}}})
    with pytest.raises(ConfigError):
        render(
            enabled_workarounds=enabled(render),
            context=context(item),
            extra_args=[],
        )


@pytest.mark.parametrize(
    "render,args",
    [
        (claude.session_artifacts, ["--plugin-dir=override"]),
        (codex.session_artifacts, ["-cskills.include_instructions=false"]),
        (grok.session_artifacts, ["--disable-skills"]),
    ],
)
def test_raw_native_overrides_cannot_replace_artifact_carrier(render, args):
    with pytest.raises(ConfigError):
        render(
            enabled_workarounds=enabled(render),
            context=context(artifact(ArtifactType.RULE)),
            extra_args=args,
        )


def test_private_plugin_persona_coexists_with_same_named_user_persona():
    item = artifact(ArtifactType.AGENT)
    other = replace(item, origin=ArtifactOrigin("agent", "agent", "alice", bundle="another", entry="review"))
    earlier = claude.outer_artifacts(received(other), "/home/alice/.claude")
    result = claude.session_artifacts(
        context(item, ancestors=tuple(owned(file) for file in earlier.files)),
        extra_args=[],
        enabled_workarounds=enabled(claude.session_artifacts),
    )
    assert {file.native_identity for file in result.application.files if file.native_identity}.isdisjoint(
        {file.native_identity for file in earlier.files}
    )
    assert not result.application.deferred


def test_ancestor_guidance_does_not_change_claude_saved_prompt_on_resume():
    files = claude.outer_artifacts(received(artifact(ArtifactType.RULE)), "/home/alice/.claude").files
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts),
        context=context(ancestors=tuple(owned(file) for file in files)),
        extra_args=[],
    )
    assert result.application.files == ()
    assert result.argv == ()


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
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, ok=True, stdout="AGW_PYTHON3=available\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            ok=True,
            stderr="",
            stdout="AGW_ARTIFACT_PROBE="
            + json.dumps(
                {"native_home": "/home/a/.claude", "problems": ["native-discovery-exclusions"], "inventory": []}
            ),
        )

    with pytest.raises(StateError):
        probe_native(
            SimpleNamespace(run=run, logger=None),
            tool="claude",
            home="/home/a",
            environment={"API_TOKEN": "a-secret-value"},
        )
    assert "a-secret-value" not in calls[-1][0]
    assert calls[-1][1]["env"]["API_TOKEN"] == "a-secret-value"


@pytest.mark.parametrize("implementation", [CodexIntegration, GrokBuildIntegration])
def test_vm_deferral_preserves_input_identity_without_descendant_knowledge(implementation):
    integration = implementation.for_setup(owner_name="box", owner_kind="vm", facet="vm", config={})
    item = artifact(ArtifactType.AGENT)
    result = integration.vm_init(SimpleNamespace(artifacts=received(item)))
    assert result.files == ()
    assert result.deferred[0].input_id == item.identity
    assert result.deferred[0].destination == "user"


@pytest.mark.parametrize(
    "implementation,configured",
    [
        (ClaudeCodeIntegration, {"append_system_prompt": "configured guidance"}),
        (GrokBuildIntegration, {"rules": "configured guidance"}),
    ],
)
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("opted_in", [False, True])
def test_native_launch_carries_literal_artifact_guidance_on_new_and_resumed_threads(
    monkeypatch, implementation, configured, resume, opted_in
):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from tests.conftest import _FakeTarget

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *args, **kwargs: "/home/alice/.native")
    if implementation is GrokBuildIntegration:
        monkeypatch.setattr(
            "agentworks.plugins.grok.harness_integration.probe_native", lambda *args, **kwargs: "/home/alice/.native"
        )
    item = artifact(ArtifactType.RULE)
    integration = implementation(
        "native",
        {
            **configured,
            "enabled_workarounds": (
                ["session-skill-plugin" if implementation is ClaudeCodeIntegration else "session-home-skills"]
                if opted_in
                else []
            ),
        },
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
        artifact_context=context(item),
    )
    method = "_transcript_exists" if implementation is ClaudeCodeIntegration else "_session_exists"
    monkeypatch.setattr(integration, method, lambda *_args: resume)
    result = integration.start(
        RunContext(admin_target=_FakeTarget()),
        intent=HarnessLaunchIntent.RESUME_OR_NEW if resume else HarnessLaunchIntent.CREATE,
    )
    assert "{{" not in result.command
    argv = shlex.split(shlex.split(result.command)[2])
    assert ("--resume" in argv) is resume
    if not opted_in:
        flag = "--rules" if implementation is GrokBuildIntegration else "--append-system-prompt"
        assert argv[argv.index(flag) + 1] == "configured guidance"
        assert result.artifacts.files == ()
        assert len(result.artifacts.deferred) == 1
    else:
        flag = "--rules" if implementation is GrokBuildIntegration else "--append-system-prompt"
        assert argv[argv.index(flag) + 1] == "configured guidance"
        assert any(item.content.text.encode() in file.data for file in result.artifacts.files)
        assert all(item.content.text not in value for value in argv)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("name", ["reviewer", "review-2", "123", "r" * 64])
@pytest.mark.parametrize("include_rule", [False, True])
def test_codex_literal_role_and_guidance_overrides_apply_on_resume_and_new(fresh, name, include_rule):
    item = artifact(ArtifactType.RULE)
    persona = artifact(ArtifactType.AGENT, name=name)
    persona = replace(
        persona,
        content=replace(persona.content, description='Review "developer_instructions=x".\n${agw:session_name} café'),
    )
    artifacts = context(item, persona) if include_rule else context(persona)
    integration = CodexIntegration(
        "codex",
        {"developer_instructions": "configured guidance"},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=artifacts,
    )
    integration._artifact_plan = codex.session_artifacts(
        enabled_workarounds=enabled(codex.session_artifacts) if include_rule else ["session-agent-config"],
        context=artifacts,
        extra_args=[],
    )
    argv_text = integration._codex_argv(() if fresh else ("resume", "thread-id"), fresh=fresh)
    assert "{{" not in argv_text
    argv = shlex.split(argv_text)
    values = [argv[index + 1] for index, token in enumerate(argv) if token == "-c"]
    # Codex splits the assignment at the first '=' and parses only its value as TOML:
    # https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/utils/cli/src/config_override.rs
    combined = {}
    for value in values:
        key, raw_value = value.split("=", 1)
        combined[key.strip()] = tomllib.loads("value=" + raw_value.strip())["value"]
    assert combined["developer_instructions"] == "configured guidance"
    if include_rule:
        assert any(item.content.text.encode() in file.data for file in integration._artifact_plan.application.files)
    assert {key for key in combined if key.startswith("agents.")} == {
        f"agents.{name}.config_file",
        f"agents.{name}.description",
    }
    assert combined[f"agents.{name}.description"] == persona.content.description
    path = combined[f"agents.{name}.config_file"]
    role = tomllib.loads(
        next(file.data.decode() for file in integration._artifact_plan.application.files if file.path == path)
    )
    assert role["developer_instructions"] == persona.content.text
    assert "name" not in role and "description" not in role


@pytest.mark.parametrize(
    "name",
    [
        "review.part",
        "/",
        "",
        'review"er',
        "review=er",
        "review er",
        "Review",
        "review_er",
        "review--er",
        "-review",
        "review-",
        "r" * 65,
    ],
)
@pytest.mark.parametrize("kind", list(ArtifactType))
def test_normalized_artifact_names_reject_noncanonical_names_before_native_delivery(name, kind):
    with pytest.raises(ValueError):
        artifact(kind, name=name)


@pytest.mark.parametrize(
    "render,kind",
    [
        (codex.session_artifacts, ArtifactType.RULE),
        (grok.session_artifacts, ArtifactType.RULE),
        (claude.session_artifacts, ArtifactType.AGENT),
    ],
)
def test_large_artifact_bodies_remain_in_files_instead_of_launch_arguments(render, kind):
    item = artifact(kind)
    item = replace(item, content=replace(item.content, text="x" * (128 * 1024)))
    result = render(context(item), extra_args=[], enabled_workarounds=enabled(render))
    assert any(item.content.text.encode() in file.data for file in result.application.files)
    assert sum(map(len, result.argv)) < 1024


def test_large_claude_context_uses_private_file_without_an_argument_size_penalty():
    item = artifact(ArtifactType.RULE)
    item = replace(item, content=replace(item.content, text="x" * (128 * 1024)))
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts), context=context(item), extra_args=[]
    )
    assert any(item.content.text.encode() in file.data for file in result.application.files)
    assert sum(len(token) for token in result.argv) < 1024


def test_quoted_native_command_limit_accounts_for_shell_expansion():
    from agentworks.artifacts.native.common import validate_native_command

    text = "'" * (16 * 1024)
    with pytest.raises(ConfigError):
        validate_native_command(shlex.quote(text))


@pytest.mark.parametrize(
    "implementation,module",
    [
        (ClaudeCodeIntegration, "agentworks.plugins.claude.harness_integration"),
        (CodexIntegration, "agentworks.plugins.codex.harness_integration"),
        (GrokBuildIntegration, "agentworks.plugins.grok.harness_integration"),
    ],
)
@pytest.mark.parametrize("outside_home", [False, True])
def test_user_native_home_boundary_precedes_other_setup(db, monkeypatch, implementation, module, outside_home):
    from importlib import import_module

    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation

    plugin = import_module(module)
    setup_calls = []
    if hasattr(plugin, "setup_user"):
        monkeypatch.setattr(plugin, "setup_user", lambda *args: setup_calls.append(args))
    root = "/mnt/native" if outside_home else "/home/alice/custom-native"
    monkeypatch.setattr(plugin, "probe_native", lambda *args, **kwargs: root)
    integration = implementation.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "lima", "vm"),
        runner=SimpleNamespace(),
        prior=None,
        checkpoint=lambda claims: None,
        username="alice",
        home="/home/alice",
        artifacts=received(artifact(ArtifactType.AGENT)),
    )
    if outside_home:
        with pytest.raises(ConfigError):
            integration.user_init(invocation)
        assert setup_calls == []
    else:
        result = integration.user_init(invocation)
        assert result.files and all(file.path.startswith(root + "/") for file in result.files)
        assert len(setup_calls) == (0 if implementation is GrokBuildIntegration else 1)


def test_codex_external_home_does_not_block_home_owned_skills(db, monkeypatch):
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from agentworks.plugins.codex import harness_integration as plugin

    monkeypatch.setattr(plugin, "setup_user", lambda *args: None)
    monkeypatch.setattr(plugin, "probe_native", lambda *args, **kwargs: "/mnt/native")
    integration = CodexIntegration.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
    result = integration.user_init(
        UserSetupInvocation(
            vm=db.insert_vm("vm", "lima", "vm"),
            runner=SimpleNamespace(),
            prior=None,
            checkpoint=lambda claims: None,
            username="alice",
            home="/home/alice",
            artifacts=received(artifact(ArtifactType.SKILL)),
        )
    )
    assert all(file.path.startswith("/home/alice/.agents/skills/") for file in result.files)
    assert result.deferred == ()


def test_codex_persona_serialization_rejects_output_beyond_inventory_bound(monkeypatch):
    item = artifact(ArtifactType.AGENT)
    rendered = codex.outer_artifacts(received(item), skills_root="/skills", agents_root="/agents")
    limit = len(rendered.files[0].data)
    monkeypatch.setattr(codex, "MAX_CODEX_PERSONA_BYTES", limit)
    assert codex.outer_artifacts(received(item), skills_root="/skills", agents_root="/agents") == rendered
    monkeypatch.setattr(codex, "MAX_CODEX_PERSONA_BYTES", limit - 1)
    with pytest.raises(ConfigError):
        codex.outer_artifacts(received(item), skills_root="/skills", agents_root="/agents")


def test_shell_index_preserves_declared_key_order_with_fixed_type_order():
    inputs = received(
        artifact(ArtifactType.RULE, name="zebra"),
        artifact(ArtifactType.RULE, name="alpha"),
        artifact(ArtifactType.HINT, name="zebra"),
        artifact(ArtifactType.HINT, name="alpha"),
    )
    application = filesystem_artifacts(inputs, "/private", session=True)
    index = json.loads(next(file.data for file in application.files if file.path.endswith("/index.json")))
    owner = index["groups"][0]
    assert list(owner) == ["owner", "hints", "rules", "skills", "agents"]
    assert list(owner["hints"]) == list(owner["rules"]) == ["zebra", "alpha"]


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts, grok.session_artifacts])
def test_session_delivery_defaults_to_unhandled_before_native_validation(render):
    items = tuple(
        artifact(type, options={name: {"hooks": {}} for name in ("claude-code", "codex", "grok-build")})
        for type in ArtifactType
    )
    duplicate = replace(items[-1], origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
    result = render(context(duplicate, *items), extra_args=["--"])
    assert result.argv == ()
    assert result.application.files == ()
    assert {item.input_id for item in result.application.deferred} == {item.identity for item in (*items, duplicate)}
    assert {item.destination for item in result.application.deferred} == {"session"}


@pytest.mark.parametrize(
    "integration,workarounds",
    [
        (ClaudeCodeIntegration, claude._SESSION_WORKAROUNDS),
        (CodexIntegration, codex._SESSION_WORKAROUNDS),
        (GrokBuildIntegration, grok._SESSION_WORKAROUNDS),
    ],
)
def test_config_workaround_names_match_native_delivery(integration, workarounds):
    annotation = integration.config_model.model_fields["enabled_workarounds"].annotation
    names = get_args(get_args(annotation)[0])
    assert set(names) == set(workarounds.values())


@pytest.mark.parametrize("type", list(ArtifactType))
def test_shell_delivers_session_artifacts_without_workarounds(type):
    item = artifact(type)
    prepared = context(item)
    integration = ShellIntegration(
        "shell",
        {},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=prepared,
    )
    result = integration.start(RunContext())
    assert isinstance(result, HarnessStart)
    assert result.artifacts.files
    assert result.artifacts.deferred == ()
    assert result.artifacts.artifacts_dir == prepared.directory
    assert any(
        file.origins == (item.origin_identity,) and file.path != f"{prepared.directory}/index.json"
        for file in result.artifacts.files
    )


@pytest.mark.parametrize(
    "render,workaround,selected,flag",
    [
        (
            claude.session_artifacts,
            "session-skill-plugin",
            {ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL},
            "--plugin-dir",
        ),
        (claude.session_artifacts, "session-agent-definitions", {ArtifactType.AGENT}, "--plugin-dir"),
        (
            codex.session_artifacts,
            "session-home-skills",
            {ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL},
            None,
        ),
        (codex.session_artifacts, "session-agent-config", {ArtifactType.AGENT}, "-c"),
        (
            grok.session_artifacts,
            "session-home-skills",
            {ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL},
            None,
        ),
    ],
)
def test_workaround_selects_only_its_types_including_deferred_ancestors(render, workaround, selected, flag):
    items = tuple(artifact(type) for type in ArtifactType)
    items = tuple(
        replace(item, origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
        if item.content.type is ArtifactType.RULE
        else item
        for item in items
    )
    items = tuple(sorted(items, key=lambda item: item.origin.component == "session"))
    received_context = context(*items)
    assert received_context.inputs.deferred
    result = render(
        received_context,
        extra_args=[],
        enabled_workarounds=[workaround],
    )
    assert (flag in result.argv) if flag is not None else result.argv == ()
    assert {item.input_id for item in result.application.deferred} == {
        item.identity for item in items if item.content.type not in selected
    }


@pytest.mark.parametrize(
    "render,workaround,args",
    [
        (claude.session_artifacts, "session-agent-definitions", ["--append-system-prompt", "configured"]),
        (codex.session_artifacts, "session-agent-config", ["-c", 'developer_instructions="configured"']),
        (grok.session_artifacts, "session-agent-definitions", ["--rules", "configured"]),
    ],
)
def test_unselected_guidance_does_not_restrict_native_guidance_flags(render, workaround, args):
    if render is grok.session_artifacts:
        args = [*args, "--single", "task"]
    rule, agent = artifact(ArtifactType.RULE), artifact(ArtifactType.AGENT)
    result = render(
        context(rule, agent),
        extra_args=args,
        enabled_workarounds=[workaround],
    )
    if render is grok.session_artifacts:
        assert result.argv == () and result.application.files == ()
        assert {item.input_id for item in result.application.deferred} == {rule.identity, agent.identity}
        assert {item.destination for item in result.application.deferred} == {"session"}
    else:
        assert result.argv
        assert {item.input_id for item in result.application.deferred} == {rule.identity}


@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
def test_skipped_inputs_do_not_probe_or_require_launch_target(monkeypatch, implementation):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent

    monkeypatch.setattr(
        "agentworks.artifacts.native.probe.probe_native",
        lambda *args, **kwargs: pytest.fail("unexpected artifact probe"),
    )
    integration = implementation(
        "native",
        {},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=context(artifact(ArtifactType.RULE)),
    )
    # Isolate launch selection from independent transcript / identity discovery.
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "native-command")
    if implementation is CodexIntegration:
        monkeypatch.setattr(integration, "_start_fresh", lambda *args, **kwargs: "native-command")
    result = integration.start(RunContext(), intent=HarnessLaunchIntent.CREATE)
    assert result.artifacts.files == ()
    assert len(result.artifacts.deferred) == 1


@pytest.mark.parametrize(
    "config,workaround",
    [
        (ClaudeCodeIntegration.config_model, "session-skill-plugin"),
        (CodexIntegration.config_model, "session-agent-config"),
        (GrokBuildIntegration.config_model, "session-home-skills"),
    ],
)
def test_workaround_config_replaces_inherited_optins_and_refuses_unknown_names(config, workaround):
    from pydantic import ValidationError

    from agentworks.schema import merge_model

    assert config().enabled_workarounds == []
    value, _ = merge_model(config, {"enabled_workarounds": [workaround]}, {"enabled_workarounds": []})
    assert config.model_validate(value).enabled_workarounds == []
    with pytest.raises(ValidationError):
        config.model_validate({"enabled_workarounds": ["unknown-workaround"]})


@pytest.mark.parametrize(
    "render,args",
    [
        (claude.session_artifacts, ["--bare"]),
        (codex.session_artifacts, ["--profile"]),
        (grok.session_artifacts, ["--no-subagents"]),
        (claude.session_artifacts, ["--", "prompt"]),
        (codex.session_artifacts, ["--", "prompt"]),
        (grok.session_artifacts, ["--", "prompt"]),
    ],
)
def test_ancestor_only_override_is_reported_without_blocking(render, args, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    existing = claude.outer_artifacts(received(artifact(ArtifactType.AGENT)), "/home/alice/.claude")
    result = render(
        context=context(ancestors=tuple(owned(file) for file in existing.files)),
        extra_args=args,
    )
    assert warnings
    assert not result.application.files and not result.argv


@pytest.mark.parametrize(
    "integration_class,module,workaround",
    [
        (ClaudeCodeIntegration, "claude", "session-skill-plugin"),
        (CodexIntegration, "codex", "session-home-skills"),
        (GrokBuildIntegration, "grok", "session-home-skills"),
    ],
)
@pytest.mark.parametrize("own_delivery", [False, True])
def test_failed_native_probe_is_advisory_only_without_new_carriers(
    integration_class, module, workaround, own_delivery, monkeypatch
):
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.errors import ExternalError
    from tests.conftest import _FakeTarget

    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)

    def fail_probe(*args, **kwargs):
        raise ExternalError("fixture native discovery unavailable")

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", fail_probe)
    ancestor = claude.outer_artifacts(received(artifact(ArtifactType.RULE)), "/home/alice/.claude")
    prepared = context(
        *([artifact(ArtifactType.RULE)] if own_delivery else []),
        ancestors=tuple(owned(file) for file in ancestor.files),
    )
    integration = integration_class(
        integration_class.name,
        {"enabled_workarounds": [workaround] if own_delivery else []},
        session_name="s1",
        vm_name="vm",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=prepared,
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "fixture-launch")
    if own_delivery:
        with pytest.raises(ExternalError):
            integration.start(RunContext(admin_target=_FakeTarget()), intent=HarnessLaunchIntent.RESUME_OR_NEW)
    else:
        result = integration.start(RunContext(admin_target=_FakeTarget()), intent=HarnessLaunchIntent.RESUME_OR_NEW)
        assert result.command == "fixture-launch" and warnings


@pytest.mark.parametrize(
    "implementation,facet",
    [
        (ClaudeCodeIntegration, "vm"),
        (ClaudeCodeIntegration, "user"),
        (ClaudeCodeIntegration, "workspace"),
        (CodexIntegration, "vm"),
        (CodexIntegration, "user"),
        (CodexIntegration, "workspace"),
        (GrokBuildIntegration, "user"),
        (GrokBuildIntegration, "workspace"),
    ],
)
def test_setup_native_name_conflict_defers_only_affected_artifact(implementation, facet, db, monkeypatch):
    from importlib import import_module

    from agentworks.artifacts.native.probe import NativeArtifactConflictError
    from agentworks.capabilities.harness_integration.setup import (
        SetupInvocation,
        UserSetupInvocation,
        VMSetupInvocation,
        WorkspaceSetupInvocation,
    )
    from tests.conftest import _FakeTarget

    module = import_module(implementation.__module__)
    module_root = "/home/alice/.native"
    calls = []

    def check(*args, files=(), **kwargs):
        calls.append(tuple(files))
        if any(file.native_identity == "skill:review" for file in files):
            raise NativeArtifactConflictError("fixture competing skill", ("skill:review",), hint="fixture repair")
        return module_root

    monkeypatch.setattr(module, "probe_native", check)
    bad = artifact(ArtifactType.SKILL)
    good = artifact(ArtifactType.RULE)
    incomplete: list[str] = []
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "runner": _FakeTarget(),
        "prior": None,
        "checkpoint": lambda claims: None,
        "report_incomplete": incomplete.append,
        "artifacts": received(bad, good),
    }
    invocation: SetupInvocation
    if facet == "vm":
        invocation = VMSetupInvocation(**common)
    elif facet == "user":
        invocation = UserSetupInvocation(**common, username="alice", home="/home/alice")
    else:
        invocation = WorkspaceSetupInvocation(**common, workspace_name="ws", root="/workspace", linux_group="ws")
    integration = implementation.for_setup(owner_kind="vm", owner_name="vm", facet=facet, config={})
    result = getattr(integration, {"vm": "vm_init", "user": "user_init", "workspace": "workspace_init"}[facet])(
        invocation
    )
    assert incomplete and calls
    assert not any(bad.origin_identity in file.origins for file in result.files)
    route = next(item for item in result.deferred if item.input_id == bad.identity)
    assert route.destination == ("user" if facet == "vm" else "session")
    assert any(good.origin_identity in file.origins for file in result.files) or any(
        item.input_id == good.identity for item in result.deferred
    )


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest filesystem helpers locally")
@pytest.mark.parametrize("modified", [False, True])
def test_setup_name_conflict_deferral_uses_safe_owned_retirement(tmp_path, db, modified):
    from agentworks.artifacts.native.common import defer_setup_conflicts
    from agentworks.artifacts.native.probe import NativeArtifactConflictError
    from agentworks.artifacts.publication import publish_artifacts
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from tests.native_setup_fixtures import LocalFixtureTransport

    target = LocalFixtureTransport(tmp_path)
    bad, good = artifact(ArtifactType.SKILL), artifact(ArtifactType.RULE)
    inputs = received(bad, good)
    plan = claude.outer_artifacts(inputs, str(target.home / ".claude"))
    current = publish_artifacts(target, plan.files, (), lambda files: None, roots=(str(target.home),)).files
    entrypoint = target.home / ".claude/skills/review/SKILL.md"
    if modified:
        entrypoint.write_text("operator changed entrypoint")
    competing = target.home / ".claude/skills/competing/SKILL.md"
    competing.parent.mkdir(parents=True)
    competing.write_text("operator content")
    incomplete: list[str] = []
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete.append,
        artifacts=inputs,
        username="alice",
        home=str(target.home),
    )

    def check(files):
        if any(file.native_identity == "skill:review" for file in files):
            raise NativeArtifactConflictError("fixture competing skill", ("skill:review",), hint="fixture repair")

    filtered = defer_setup_conflicts(plan, invocation, check)
    result = publish_artifacts(target, filtered.files, current, lambda files: None, roots=(str(target.home),))
    assert incomplete and filtered.deferred[0].input_id == bad.identity
    assert competing.read_text() == "operator content"
    assert entrypoint.exists() is modified
    assert any(file.path == str(entrypoint) for file in result.files) is modified
    assert all(Path(file.path).exists() for file in result.files)
    assert any(good.origin_identity in file.origins for file in result.files)


@pytest.mark.parametrize("render", [codex.session_artifacts])
@pytest.mark.parametrize("matching_owner", ["user", "workspace"])
def test_new_carrier_cannot_choose_one_of_competing_ancestor_origins(render, matching_owner):
    first = artifact(ArtifactType.AGENT, owner="user")
    second = artifact(ArtifactType.AGENT, owner="workspace")
    earlier = (
        *claude.outer_artifacts(received(first), "/home/alice/.claude").files,
        *claude.outer_artifacts(received(second), "/workspace/.claude").files,
    )
    selected = first if matching_owner == "user" else second
    with pytest.raises(NativeArtifactConflictError):
        prepared = context(selected, ancestors=tuple(owned(file) for file in earlier))
        plan = render(prepared, extra_args=[], enabled_workarounds=enabled(render))
        validate_ancestor_names(prepared, plan.application)
